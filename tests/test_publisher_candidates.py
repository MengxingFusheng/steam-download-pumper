import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PublisherCandidateTests(unittest.TestCase):
    def _write(self, value):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "candidates.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return path

    def test_loads_enabled_unique_https_and_http_urls(self):
        from source_publisher.candidates import load_candidates

        path = self._write({"schema": 1, "sources": [
            {"url": "https://mirror.example/file.iso", "enabled": True},
            {"url": "http://speed.example/file.bin", "enabled": False},
        ]})
        self.assertEqual(load_candidates(path), ["https://mirror.example/file.iso"])

    def test_rejects_invalid_schema_or_unsafe_urls(self):
        from source_publisher.candidates import load_candidates

        invalid_documents = [
            {"schema": 2, "sources": []},
            {"schema": 1, "sources": "not-a-list"},
            {"schema": 1, "sources": [{"url": "ftp://example.test/file", "enabled": True}]},
            {"schema": 1, "sources": [{"url": "https://user:pass@example.test/file", "enabled": True}]},
            {"schema": 1, "sources": [{"url": "https://example.test/file#fragment", "enabled": True}]},
            {"schema": 1, "sources": [{"url": "https://example.test/file?token=secret", "enabled": True}]},
            {"schema": 1, "sources": [{"url": " https://example.test/file", "enabled": True}]},
            {"schema": 1, "sources": [{"url": "https://example.test:bad/file", "enabled": True}]},
            {"schema": 1, "sources": [{"url": "https://example.test/file", "enabled": 1}]},
            {"schema": 1, "sources": [
                {"url": "https://example.test/file", "enabled": True},
                {"url": "https://example.test/file", "enabled": True},
            ]},
        ]
        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(ValueError):
                load_candidates(self._write(document))

    def test_rejects_more_than_two_hundred_candidates(self):
        from source_publisher.candidates import load_candidates

        sources = [{"url": f"https://mirror{i}.example/file", "enabled": True} for i in range(201)]
        with self.assertRaises(ValueError):
            load_candidates(self._write({"schema": 1, "sources": sources}))

    def test_resolve_public_ipv4_rejects_non_public_and_ipv6_only_hosts(self):
        from source_publisher.candidates import resolve_public_ipv4

        rejected = [
            "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
            "169.254.169.254", "0.0.0.0", "224.0.0.1", "192.0.2.1",
        ]
        for address in rejected:
            with self.subTest(address=address), patch("socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
            ]), self.assertRaises(ValueError):
                resolve_public_ipv4("example.test", 443)
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
        ]), self.assertRaises(ValueError):
            resolve_public_ipv4("example.test", 443)


if __name__ == "__main__":
    unittest.main()
