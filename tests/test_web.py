import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from steam_pumper.web import Handler


class DummyController:
    cfg = type("Cfg", (), {"to_dict": lambda self: {}})()

    def status(self):
        return {"ok": True}

    def metrics(self):
        return {"target_mbps": 800, "avg60_mbps": 760}

    def source_snapshot(self):
        return [{"url": "https://example.test/file", "ip": "203.0.113.1", "healthy": True}]


class WebTests(unittest.TestCase):
    def test_metrics_and_sources_api_endpoints(self):
        Handler.controller = DummyController()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            metrics = json.loads(urllib.request.urlopen(f"{base}/api/metrics", timeout=2).read().decode("utf-8"))
            sources = json.loads(urllib.request.urlopen(f"{base}/api/sources", timeout=2).read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(metrics["target_mbps"], 800)
        self.assertEqual(sources[0]["ip"], "203.0.113.1")


if __name__ == "__main__":
    unittest.main()
