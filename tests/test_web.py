import json
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from steam_pumper.web import Handler, PumperHTTPServer, render_html


class DummyConfig:
    def to_dict(self):
        return {"target_mbps": 800}


class DummyController:
    cfg = DummyConfig()

    def status(self):
        return {"running": False, "config": self.cfg.to_dict(), "logs": []}

    def metrics(self):
        return {"target_mbps": 800, "avg60_mbps": 760, "lines": []}

    def source_snapshot(self):
        return [{"url": "https://example.test/file", "ip": "203.0.113.1", "healthy": True}]

    def source_list_status(self):
        return {
            "enabled": True,
            "revision": 20260720031700,
            "source_count": 3,
            "origin": "remote",
            "last_success_at": "2026-07-20T04:00:00+08:00",
            "next_refresh_at": "2026-07-21T04:10:00+08:00",
            "stale": False,
            "last_error": "",
        }

    def request_source_list_refresh(self):
        return {**self.source_list_status(), "refresh_request_state": "queued"}

    def set_manual_enabled(self, _enabled):
        return None

    def start_downloads(self):
        return None

    def update_config(self, _data):
        return self.cfg


class WebTests(unittest.TestCase):
    def test_http_server_drives_controller_tick(self):
        class TickController:
            def __init__(self):
                self.ticks = 0

            def tick(self):
                self.ticks += 1

        controller = TickController()
        server = PumperHTTPServer(("127.0.0.1", 0), Handler, controller)
        try:
            server.service_actions()
        finally:
            server.server_close()

        self.assertEqual(controller.ticks, 1)

    def test_rendered_console_javascript_is_valid(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript syntax validation")
        html = render_html("multi_ip")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]

        result = subprocess.run(
            ["node", "--check"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ikuai_console_has_no_ip_or_line_count_fields(self):
        html = render_html("ikuai_line")

        self.assertIn('name="target_mbps"', html)
        self.assertIn('name="connections_per_line"', html)
        self.assertNotIn('name="line_count"', html)
        self.assertNotIn('name="lan_ips"', html)
        self.assertNotIn("EGRESS_MODE", html)

    def test_multi_ip_console_adds_only_topology_fields(self):
        html = render_html("multi_ip")

        self.assertIn('name="line_count"', html)
        self.assertIn('name="lan_ips"', html)
        self.assertNotIn('name="egress_mode"', html)
        self.assertNotIn("新建连接数", html)

    def test_multi_ip_console_displays_remote_source_list_status_and_refresh(self):
        html = render_html("multi_ip")

        for expected in (
            "远程源清单",
            "sourceListRevision",
            "sourceListCount",
            "sourceListOrigin",
            "sourceListLastSuccess",
            "sourceListNextRefresh",
            "sourceListStale",
            "sourceListError",
            "立即刷新",
            "/api/source-list/refresh",
        ):
            self.assertIn(expected, html)
        self.assertNotIn('name="source_list_url"', html)
        self.assertNotIn('name="source_list_public_key"', html)
        self.assertNotIn("sourceListRefreshState", html)
        self.assertNotIn("sourceListApplyState", html)

    def test_ikuai_console_does_not_show_remote_source_list_controls(self):
        html = render_html("ikuai_line")

        self.assertNotIn("远程源清单", html)
        self.assertNotIn("/api/source-list/refresh", html)

    def test_console_escapes_table_values_and_uses_text_content_for_logs(self):
        html = render_html("multi_ip")

        self.assertIn("function escapeHtml", html)
        self.assertIn("escapeHtml(line.bind_ip", html)
        self.assertIn("escapeHtml(source.url", html)
        self.assertIn("logs').textContent", html)

    def test_console_displays_source_circuit_breaker_state(self):
        html = render_html("multi_ip")

        self.assertIn("source.state", html)
        self.assertIn("source.retry_in_seconds", html)
        self.assertIn("隔离/重试", html)

    def test_metrics_sources_and_config_api_endpoints(self):
        Handler.controller = DummyController()
        Handler.topology_name = "ikuai_line"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            metrics = self._get_json(f"{base}/api/metrics")
            sources = self._get_json(f"{base}/api/sources")
            config = self._get_json(f"{base}/api/config")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(metrics["target_mbps"], 800)
        self.assertEqual(sources[0]["ip"], "203.0.113.1")
        self.assertEqual(config["target_mbps"], 800)

    def test_source_list_get_and_manual_refresh_endpoints(self):
        Handler.controller = DummyController()
        Handler.topology_name = "multi_ip"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            current = self._get_json(f"{base}/api/source-list")
            request = urllib.request.Request(f"{base}/api/source-list/refresh", data=b"", method="POST")
            refreshed = json.loads(urllib.request.urlopen(request, timeout=2).read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(current["origin"], "remote")
        self.assertEqual(refreshed["revision"], 20260720031700)
        self.assertEqual(refreshed["refresh_request_state"], "queued")

    def test_manual_refresh_failure_returns_503_json(self):
        from steam_pumper.controller import SourceListRefreshError

        class OfflineController(DummyController):
            def request_source_list_refresh(self):
                raise SourceListRefreshError("OSS offline")

        Handler.controller = OfflineController()
        Handler.topology_name = "multi_ip"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/source-list/refresh",
            data=b"",
            method="POST",
        )
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(payload, {"error": "OSS offline"})

    def test_api_errors_are_json(self):
        class RejectingController(DummyController):
            def update_config(self, _data):
                raise ValueError("bad config")

        Handler.controller = RejectingController()
        Handler.topology_name = "ikuai_line"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/config",
            data=b"{}",
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(payload, {"error": "bad config"})

    @staticmethod
    def _get_json(url):
        return json.loads(urllib.request.urlopen(url, timeout=2).read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
