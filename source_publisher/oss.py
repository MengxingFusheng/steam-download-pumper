from __future__ import annotations

import http.client
import os
import re
import subprocess
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .candidates import resolve_public_ipv4
from .config import PublisherConfig, PublisherSecrets
from .manifest import MAX_MANIFEST_BYTES
from .probe import PinnedResponse, Resolver, pinned_request


RELEASE_KEY_RE = re.compile(r"^releases/\d{14}\.json$")


class OSSFailure(RuntimeError):
    pass


class OSSClient:
    def __init__(
        self,
        config: PublisherConfig,
        secrets: PublisherSecrets,
        *,
        resolver: Resolver = resolve_public_ipv4,
        request_fn: Callable[..., PinnedResponse] = pinned_request,
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.resolver = resolver
        self.request_fn = request_fn

    def _environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "OSS_ACCESS_KEY_ID": self.secrets.oss_access_key_id,
            "OSS_ACCESS_KEY_SECRET": self.secrets.oss_access_key_secret,
            "OSS_REGION": self.config.region,
            "OSS_ENDPOINT": self.config.endpoint,
        }

    def upload(self, source: Path, object_key: str) -> None:
        if not object_key.startswith("pumper/v1/") or ".." in object_key:
            raise OSSFailure("OSS object key is invalid")
        command = [
            self.config.ossutil_path,
            "cp",
            str(source),
            f"oss://{self.config.bucket}/{object_key}",
            "--force",
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._environment(),
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSSFailure("OSS upload failed") from exc
        if completed.returncode != 0:
            raise OSSFailure("OSS upload failed")

    def read_public(self, relative_key: str) -> bytes:
        if relative_key != "latest.json" and RELEASE_KEY_RE.fullmatch(relative_key) is None:
            raise OSSFailure("public object key is invalid")
        return self.read_url(f"{self.config.public_base_url}/{relative_key}")

    @staticmethod
    def _origin(parsed) -> tuple[str, str, int]:  # type: ignore[no-untyped-def]
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 443

    def _validate_public_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            base = urlsplit(self.config.public_base_url)
            _ = parsed.port
        except ValueError as exc:
            raise OSSFailure("public verification URL is invalid") from exc
        if (
            self._origin(parsed) != self._origin(base)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise OSSFailure("public verification URL is outside the configured origin")
        expected_prefix = base.path.rstrip("/")
        relative = parsed.path.removeprefix(expected_prefix + "/")
        if parsed.path == expected_prefix or (
            relative != "latest.json" and RELEASE_KEY_RE.fullmatch(relative) is None
        ):
            raise OSSFailure("public verification path is invalid")

    def read_url(self, url: str) -> bytes:
        self._validate_public_url(url)
        try:
            response = self.request_fn(
                url,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "pumper-source-publisher/1",
                },
                max_bytes=MAX_MANIFEST_BYTES + 1,
                timeout=20,
                resolver=self.resolver,
            )
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise OSSFailure("public verification failed") from exc
        if response.status != 200:
            raise OSSFailure("public verification rejected redirect or non-200 response")
        if not response.body or len(response.body) > MAX_MANIFEST_BYTES:
            raise OSSFailure("public verification response is invalid")
        return response.body
