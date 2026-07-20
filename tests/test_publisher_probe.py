import socket
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


class _ProbeHandler(BaseHTTPRequestHandler):
    ranges = []
    hosts = []
    statuses = []
    bytes_to_send = 2 * 1024 * 1024

    def do_GET(self):
        type(self).ranges.append(self.headers.get("Range"))
        type(self).hosts.append(self.headers.get("Host"))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://redirected.example:{self.server.server_port}/file")
            self.end_headers()
            return
        status = type(self).statuses.pop(0) if type(self).statuses else 206
        self.send_response(status)
        if status in (200, 206):
            self.send_header("Content-Length", str(type(self).bytes_to_send))
        self.end_headers()
        if status in (200, 206):
            chunk = b"x" * 65536
            remaining = type(self).bytes_to_send
            while remaining:
                data = chunk[:remaining]
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    break
                remaining -= len(data)

    def log_message(self, _format, *_args):
        pass


class PublisherProbeTests(unittest.TestCase):
    def setUp(self):
        _ProbeHandler.ranges = []
        _ProbeHandler.hosts = []
        _ProbeHandler.statuses = []
        _ProbeHandler.bytes_to_send = 2 * 1024 * 1024
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://probe.example:{self.server.server_port}/file"

    def _dial_validated_ip_to_fixture(self, dialed):
        def dial(address, timeout=None, source_address=None, *args, **kwargs):
            dialed.append(address)
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(("127.0.0.1", self.server.server_port))
            return connection

        return dial

    def test_source_requires_two_bounded_range_probes(self):
        from source_publisher.probe import probe_source

        dialed = []
        with patch("socket.create_connection", side_effect=self._dial_validated_ip_to_fixture(dialed)):
            result = probe_source(
                self.url,
                timeout=5,
                resolver=lambda _host, _port: ("93.184.216.34",),
            )
        self.assertTrue(result.success, result.error)
        self.assertEqual(_ProbeHandler.ranges, ["bytes=0-8388607", "bytes=0-8388607"])
        self.assertEqual(_ProbeHandler.hosts, [
            f"probe.example:{self.server.server_port}",
            f"probe.example:{self.server.server_port}",
        ])
        self.assertEqual(dialed, [
            ("93.184.216.34", self.server.server_port),
            ("93.184.216.34", self.server.server_port),
        ])
        self.assertGreaterEqual(result.probe_mbps, 0)
        self.assertEqual(result.bytes_read, 4 * 1024 * 1024)

    def test_rejects_failed_or_short_probe(self):
        from source_publisher.probe import probe_source

        dialed = []
        _ProbeHandler.statuses = [206, 500]
        with patch("socket.create_connection", side_effect=self._dial_validated_ip_to_fixture(dialed)):
            failed = probe_source(
                self.url, timeout=5, resolver=lambda *_args: ("93.184.216.34",)
            )
        self.assertFalse(failed.success)
        _ProbeHandler.statuses = [206]
        _ProbeHandler.bytes_to_send = 2 * 1024 * 1024 - 1
        with patch("socket.create_connection", side_effect=self._dial_validated_ip_to_fixture(dialed)):
            short = probe_source(
                self.url, timeout=5, resolver=lambda *_args: ("93.184.216.34",)
            )
        self.assertFalse(short.success)

    def test_dns_rebinding_cannot_change_validated_dial_address(self):
        from source_publisher.probe import probe_source

        dialed = []
        public_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", self.server.server_port))
        ]
        with (
            patch("socket.getaddrinfo", return_value=public_answer) as resolver,
            patch("socket.create_connection", side_effect=self._dial_validated_ip_to_fixture(dialed)),
        ):
            result = probe_source(self.url, timeout=5)
        self.assertTrue(result.success, result.error)
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual([address[0] for address in dialed], ["93.184.216.34", "93.184.216.34"])

    def test_mixed_public_and_private_answers_are_rejected_before_dial(self):
        from source_publisher.probe import probe_source

        with patch("socket.create_connection") as dial:
            result = probe_source(
                self.url,
                timeout=5,
                resolver=lambda *_args: ("93.184.216.34", "10.0.0.7"),
            )
        self.assertFalse(result.success)
        dial.assert_not_called()

    def test_each_redirect_is_resolved_and_pinned_again(self):
        from source_publisher.probe import probe_source

        dialed = []
        resolved = []

        def resolver(host, _port):
            resolved.append(host)
            return ("93.184.216.34",)

        redirect_url = f"http://probe.example:{self.server.server_port}/redirect"
        with patch("socket.create_connection", side_effect=self._dial_validated_ip_to_fixture(dialed)):
            result = probe_source(redirect_url, timeout=5, resolver=resolver)
        self.assertTrue(result.success, result.error)
        self.assertEqual(resolved, [
            "probe.example", "redirected.example",
            "probe.example", "redirected.example",
        ])
        self.assertEqual(len(dialed), 4)

    def test_https_dials_validated_ip_but_uses_original_hostname_for_sni(self):
        from source_publisher.probe import _PinnedHTTPSConnection

        connection = _PinnedHTTPSConnection(
            "93.184.216.34", "secure.example", 443, 5
        )
        raw_socket = object()
        wrapped_socket = object()
        connection._create_connection = unittest.mock.Mock(return_value=raw_socket)
        connection._context = unittest.mock.Mock()
        connection._context.wrap_socket.return_value = wrapped_socket
        connection.connect()
        connection._create_connection.assert_called_once_with(
            ("93.184.216.34", 443), 5, None
        )
        connection._context.wrap_socket.assert_called_once_with(
            raw_socket, server_hostname="secure.example"
        )
        self.assertIs(connection.sock, wrapped_socket)

    def test_probe_pool_never_exceeds_four_workers(self):
        from source_publisher.probe import ProbeResult, probe_candidates

        active = 0
        maximum = 0
        lock = threading.Lock()

        def fake_probe(url, **_kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return ProbeResult(url, datetime.now(timezone.utc), 10.0, 4 * 1024 * 1024, True, "")

        with patch("source_publisher.probe.probe_source", side_effect=fake_probe):
            results = probe_candidates([f"https://example{i}.test/file" for i in range(12)], concurrency=8)
        self.assertEqual(len(results), 12)
        self.assertLessEqual(maximum, 4)


if __name__ == "__main__":
    unittest.main()
