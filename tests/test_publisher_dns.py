import json
import threading
import time
import unittest
from unittest.mock import patch

from source_publisher.dns import DoHResolver
from source_publisher.probe import PinnedResponse


class PublisherDoHTests(unittest.TestCase):
    def setUp(self):
        resolver = patch("source_publisher.dns.resolve_public_ipv4", return_value=("223.5.5.5",))
        resolver.start()
        self.addCleanup(resolver.stop)

    def response(self, answers=None, **updates):
        value = {'Status': 0, 'Question': [{'name': 'source.example.', 'type': 1}],
                 'Answer': answers if answers is not None else [
                     {'name': 'source.example.', 'type': 5, 'TTL': 900, 'data': 'cdn.example.'},
                     {'name': 'cdn.example.', 'type': 1, 'TTL': 900, 'data': '93.184.216.34'},
                 ]}
        value.update(updates)
        return PinnedResponse(200, {}, json.dumps(value).encode())

    def test_checks_cname_association_caches_and_shares_cancellation_and_deadline(self):
        calls = []
        cancellation = threading.Event()
        deadline = time.monotonic() + 2
        def request(url, **kwargs):
            calls.append((url, kwargs))
            return self.response()
        resolver = DoHResolver('https://dns.alidns.com/resolve', request_fn=request,
                               cancel_event=cancellation, deadline=deadline)
        self.assertEqual(resolver('source.example', 443), ('93.184.216.34',))
        self.assertEqual(resolver('source.example', 80), ('93.184.216.34',))
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(resolver.cache['source.example'][0], time.monotonic() + 300)
        self.assertEqual(calls[0][1]['max_bytes'], 64 * 1024 + 1)
        self.assertIs(calls[0][1]['cancel_event'], cancellation)
        self.assertLessEqual(calls[0][1]['deadline'], deadline)
        self.assertIn('type=A', calls[0][0])

    def test_rejects_redirect_wrong_question_nonzero_status_and_unrelated_answers(self):
        bad = [PinnedResponse(302, {'location': 'https://elsewhere.example/'}, b''),
               self.response(Status=3), self.response(Question=[{'name': 'other.example', 'type': 1}]),
               self.response(answers=[{'name': 'other.example', 'type': 1, 'data': '93.184.216.34', 'TTL': 1}]),
               self.response(answers=[{'name': 'source.example', 'type': 1, 'data': '127.0.0.1', 'TTL': 1}]),
               self.response(answers=[{'name': 'source.example', 'type': 1, 'data': '198.18.0.1', 'TTL': 1}]),
               self.response(answers=[{'name': 'source.example', 'type': 1, 'data': '::1', 'TTL': 1}]),
               PinnedResponse(200, {}, b'x' * (64 * 1024 + 1)),
               self.response(answers=[{'name': 'source.example', 'type': 5, 'data': 'source.example.', 'TTL': 1}])]
        for response in bad:
            with self.subTest(response=response.status), self.assertRaises(ValueError):
                DoHResolver('https://dns.alidns.com/resolve', request_fn=lambda *a, **k: response)('source.example', 443)

    def test_alidns_single_question_object_is_supported(self):
        response = self.response(Question={"name": "source.example.", "type": 1})
        resolver = DoHResolver("https://dns.alidns.com/resolve", request_fn=lambda *a, **k: response)
        self.assertEqual(resolver("source.example", 443), ("93.184.216.34",))

    def test_literal_public_ip_does_not_query_dns_and_private_ip_is_rejected(self):
        def request(*args, **kwargs):
            self.fail('literal address should not need DoH')
        resolver = DoHResolver('https://dns.alidns.com/resolve', request_fn=request)
        self.assertEqual(resolver('93.184.216.34', 443), ('93.184.216.34',))
        with self.assertRaises(ValueError):
            resolver('10.0.0.1', 443)
