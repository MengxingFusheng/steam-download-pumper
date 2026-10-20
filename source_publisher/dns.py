from __future__ import annotations

import ipaddress
import json
import threading
import time
from typing import Callable
from urllib.parse import urlencode, urlsplit

from .candidates import resolve_public_ipv4
from .probe import PinnedResponse, pinned_request


MAX_DOH_BYTES = 64 * 1024


def _name(value: str) -> str:
    return value.rstrip('.').encode('idna').decode('ascii').lower()


def _public_address(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError('DNS returned a non-public IPv4 address')
    return str(address)


class DoHResolver:
    """An explicit trusted HTTPS DNS endpoint; candidate connections remain pinned."""

    def __init__(self, endpoint: str, *, request_fn: Callable[..., PinnedResponse] = pinned_request,
                 cancel_event: threading.Event | None = None, deadline: float | None = None) -> None:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError('DoH endpoint must be an HTTPS URL without credentials or query')
        self.endpoint = endpoint
        self.provider_host = parsed.hostname
        self.provider_port = parsed.port or 443
        self.provider_addresses: tuple[str, ...] | None = None
        self.request_fn = request_fn
        self.cancel_event = cancel_event or threading.Event()
        self.deadline = deadline
        self.cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self.lock = threading.Lock()

    def __call__(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            return (_public_address(hostname),)
        name = _name(hostname)
        with self.lock:
            cached = self.cache.get(name)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        deadline = min(self.deadline or float('inf'), time.monotonic() + 5)
        # This resolver already runs in a bounded DNS worker. Resolve the trusted
        # provider directly there, then skip nested pool work to avoid starving
        # the same worker pool when four candidates query DoH concurrently.
        if self.provider_addresses is None:
            self.provider_addresses = resolve_public_ipv4(self.provider_host, self.provider_port)
        response = self.request_fn(
            self.endpoint + '?' + urlencode({'name': name, 'type': 'A'}),
            headers={'Accept': 'application/dns-json', 'Accept-Encoding': 'identity'},
            max_bytes=MAX_DOH_BYTES + 1, timeout=5, deadline=deadline,
            cancel_event=self.cancel_event,
            # Resolving the trusted DoH endpoint uses only the system resolver;
            # this cannot recurse back into the candidate resolver.
            resolver=resolve_public_ipv4,
            resolved_addresses=self.provider_addresses,
        )
        if response.status != 200 or len(response.body) > MAX_DOH_BYTES:
            raise ValueError('DoH response was rejected')
        try:
            document = json.loads(response.body)
            question = document['Question']
            # AliDNS returns one Question object; Google uses a one-item array.
            if isinstance(question, dict):
                question = [question]
            answers = document.get('Answer', [])
            if (document.get('Status') != 0 or document.get('TC') is True or not isinstance(question, list)
                    or len(question) != 1 or question[0].get('type') != 1
                    or _name(question[0]['name']) != name or not isinstance(answers, list)
                    or len(answers) > 1000):
                raise ValueError
            chain: set[str] = {name}
            ttl_values: list[int] = []
            current = name
            for _ in range(10):
                cnames = [a for a in answers if a.get('type') == 5 and _name(a['name']) == current]
                if not cnames:
                    break
                targets = {_name(a['data']) for a in cnames}
                if len(targets) != 1:
                    raise ValueError
                target = targets.pop()
                if target in chain:
                    raise ValueError
                ttl_values.extend(max(0, int(a.get('TTL', 0))) for a in cnames)
                chain.add(target)
                current = target
            else:
                raise ValueError
            associated = [a for a in answers if a.get('type') == 1 and _name(a['name']) in chain]
            addresses = tuple(dict.fromkeys(_public_address(a['data']) for a in associated))
            if not addresses:
                raise ValueError
            ttl_values.extend(max(0, int(a.get('TTL', 0))) for a in associated)
        except (TypeError, KeyError, AttributeError, ValueError, UnicodeError) as exc:
            raise ValueError('DoH returned invalid or unrelated IPv4 answers') from exc
        ttl = min([300, *ttl_values])
        with self.lock:
            # Candidate count is bounded but redirects can create extra hostnames.
            if len(self.cache) >= 1024:
                self.cache = {key: item for key, item in self.cache.items() if item[0] > time.monotonic()}
                if len(self.cache) >= 1024:
                    self.cache.pop(next(iter(self.cache)))
            self.cache[name] = (time.monotonic() + ttl, addresses)
        return addresses
