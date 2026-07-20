import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


class _ProbeHandler(BaseHTTPRequestHandler):
    ranges = []
    statuses = []
    bytes_to_send = 2 * 1024 * 1024

    def do_GET(self):
        type(self).ranges.append(self.headers.get("Range"))
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
        _ProbeHandler.statuses = []
        _ProbeHandler.bytes_to_send = 2 * 1024 * 1024
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/file"

    def test_source_requires_two_bounded_range_probes(self):
        from source_publisher.probe import probe_source

        result = probe_source(
            self.url,
            probe_bytes=8 * 1024 * 1024,
            timeout=5,
            resolver=lambda _host, _port: ("127.0.0.1",),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(_ProbeHandler.ranges, ["bytes=0-8388607", "bytes=0-8388607"])
        self.assertGreaterEqual(result.probe_mbps, 0)
        self.assertEqual(result.bytes_read, 4 * 1024 * 1024)

    def test_rejects_failed_or_short_probe(self):
        from source_publisher.probe import probe_source

        _ProbeHandler.statuses = [206, 500]
        failed = probe_source(self.url, timeout=5, resolver=lambda *_args: ("127.0.0.1",))
        self.assertFalse(failed.success)
        _ProbeHandler.statuses = [206]
        _ProbeHandler.bytes_to_send = 2 * 1024 * 1024 - 1
        short = probe_source(self.url, timeout=5, resolver=lambda *_args: ("127.0.0.1",))
        self.assertFalse(short.success)

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
