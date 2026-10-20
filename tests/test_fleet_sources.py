import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from steam_pumper.config import FleetConfig
from steam_pumper.engine import EngineProcess
from steam_pumper.fleet import ActiveLine, FleetAccount, FleetController
from steam_pumper.remote_sources import SourceListSnapshot
from steam_pumper.topology import LogicalLine


class FleetSourcesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"APPLY_LAN_IPS": "0", "DASHBOARD_URL": "", "FLEET_ACCOUNTS_CSV": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.controller = FleetController(Path(self.directory.name) / "config.json", FleetConfig())
        self.controller.sources_runtime_dir = Path(self.directory.name) / "sources"
        self.addCleanup(self.controller.shutdown)
        self.now = datetime.now(ZoneInfo("Asia/Shanghai"))

    def engine(self, account_id="a1"):
        account = FleetAccount(account_id, "127.0.0.1", "pppoe-test", weight=1)
        with patch.object(EngineProcess, "start"):
            self.controller._start(account, self.now)
        return self.controller.active[account_id].engine

    def test_start_passes_source_file_generation_and_retained_health(self):
        self.controller.account_source_health["a1"] = {"http://example.test/file": {"consecutive_failures": 3}}
        engine = self.engine()
        self.assertIsNotNone(engine.sources_file)
        self.assertTrue(engine.state.confirmed_source_generation)
        self.assertEqual(engine.source_health["http://example.test/file"]["consecutive_failures"], 3)

    def test_failed_signal_is_not_reported_as_applied(self):
        engine = self.engine()
        engine.process = SimpleNamespace(pid=12345, poll=lambda: None)
        with patch("steam_pumper.engine.os.kill", side_effect=OSError("signal failed")):
            self.controller.update_config({"source_pool": ["https://new.example/test"]})
        state = self.controller.source_list_status()
        self.assertEqual(state["apply_state"], "failed")
        self.assertIn("signal failed", state["apply_error"])
        self.assertNotEqual(state["active_generation"], state["pending_generation"])
        engine.process = None

    def test_only_unconfirmed_lines_are_retried_and_latest_generation_wins(self):
        first, second = self.engine("a1"), self.engine("a2")
        for index, engine in enumerate((first, second)):
            engine.process = SimpleNamespace(pid=12345 + index, poll=lambda: None)
        start = time.monotonic()
        with patch("steam_pumper.engine.os.kill") as kill:
            self.controller._begin_source_update(["https://new.example/a"], 10, "remote", start)
            old = self.controller.pending_source_generation
            first._consume_line(json.dumps({"type": "source-list", "line_id": "a1", "state": "reloaded", "generation": old}))
            self.controller._advance_source_update(start + 31)
            self.assertEqual(self.controller.source_list_status()["apply_state"], "partial")
            self.assertEqual(kill.call_count, 3)
            self.controller._begin_source_update(["https://new.example/b"], 11, "remote", start + 32)
            latest = self.controller.pending_source_generation
            second._consume_line(json.dumps({"type": "source-list", "line_id": "a2", "state": "reloaded", "generation": old}))
            self.assertFalse(second.source_generation_confirmed(latest))
            for account_id, engine in (("a1", first), ("a2", second)):
                engine._consume_line(json.dumps({"type": "source-list", "line_id": account_id, "state": "reloaded", "generation": latest}))
            self.controller._advance_source_update(start + 33)
            self.assertEqual(self.controller.source_list_status()["active_revision"], 11)
            self.assertEqual(self.controller.source_list_status()["apply_state"], "applied")
        first.process = second.process = None

    def test_unknown_health_is_not_healthy_and_measurements_are_real(self):
        engine = self.engine()
        url = engine.sources[0]
        self.assertIsNone(self.controller.source_snapshot()[0]["healthy"])
        engine._consume_line(json.dumps({"type": "source", "line_id": "a1", "url": url,
            "generation": engine.state.confirmed_source_generation, "source_epoch": 1,
            "state": "healthy", "window_bytes": 1000, "active_seconds": 2,
            "sample_seconds": 15, "unit_connection_mbps": 0.004,
            "health": {"total_bytes": 1000}}))
        row = self.controller.source_snapshot()[0]
        self.assertTrue(row["healthy"])
        self.assertEqual(row["lines"][0]["window_bytes"], 1000)
        self.controller._stop("a1")
        self.assertEqual(self.controller.account_source_health["a1"][url]["total_bytes"], 1000)

    def test_failed_start_requires_real_loaded_confirmation(self):
        engine = self.engine()
        with patch("steam_pumper.engine.subprocess.Popen", side_effect=OSError("binary unavailable")):
            engine.start()
        self.assertFalse(engine.source_generation_confirmed(self.controller.source_generation))
        self.assertEqual(self.controller.source_list_status()["apply_state"], "failed")
        engine.stage_sources(engine.sources, self.controller.source_generation)
        self.assertFalse(engine.source_generation_confirmed(self.controller.source_generation))

    def test_restart_resets_epochs_and_accepts_new_process_measurements(self):
        engine = self.engine()
        url = engine.sources[0]
        generation = engine.state.confirmed_source_generation
        engine._source_epochs[url] = 99
        with patch("steam_pumper.engine.subprocess.Popen", side_effect=OSError("test")):
            engine.start()
        engine._consume_line(json.dumps({"type": "source-list", "line_id": "a1", "state": "loaded", "generation": generation}))
        engine._consume_line(json.dumps({"type": "source", "line_id": "a1", "url": url,
            "generation": generation, "source_epoch": 1, "state": "healthy", "window_bytes": 1234}))
        self.assertEqual(engine.state.source_states[url].window_bytes, 1234)

    def test_local_fallback_changes_are_applied_with_remote_enabled(self):
        self.controller.remote_source_manager = SimpleNamespace(current_urls=["https://remote.example/a"])
        self.controller.effective_source_pool = ["https://remote.example/a"]
        self.controller.active_source_revision = 123
        engine = self.engine()
        self.controller.update_config({"source_pool": ["https://fallback.example/a"]})
        self.controller._advance_source_update(time.monotonic())
        self.assertEqual(engine.fallback_sources, ["https://fallback.example/a"])
        self.assertEqual(self.controller.effective_source_pool, ["https://remote.example/a"])
        self.controller.remote_source_manager = None

    def test_connections_increase_within_existing_ceiling(self):
        from collections import deque
        engine = self.engine()
        active = self.controller.active["a1"]
        active.started_at = self.now - timedelta(seconds=65)
        active.last_progress_at = self.now
        active.rate_samples = deque([(self.now - timedelta(seconds=60), 0), (self.now, 20_000_000)])
        engine.state.total_bytes = 20_000_000
        active.last_bytes = 20_000_000
        with patch.object(engine, "poll"), patch.object(engine, "set_connections") as resize:
            self.controller._poll_engines(self.now)
        resize.assert_called_once_with(min(self.controller.cfg.connections_per_line + 1, self.controller.cfg.max_connections_per_line))


@unittest.skipUnless(os.environ.get("DISCARDER_TEST_BINARY"), "set DISCARDER_TEST_BINARY for real Go integration")
class FleetRealReloadTests(unittest.TestCase):
    def test_fleet_switches_requests_without_changing_go_pid(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(16 * 1024 * 1024))
                self.end_headers()
                try:
                    for _ in range(256):
                        self.wfile.write(b"x" * 65536)
                        self.wfile.flush()
                        time.sleep(0.003)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "discarder"
                binary.symlink_to(Path(os.environ["DISCARDER_TEST_BINARY"]).resolve())
                base = f"http://127.0.0.1:{server.server_port}"
                cfg = FleetConfig(source_pool=[base + "/old"], connections_per_line=1,
                                  startup_stagger_seconds=0, worker_restart_jitter_seconds=0, engine_status_seconds=1)
                with patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ["PATH"],
                                            "APPLY_LAN_IPS": "0", "DASHBOARD_URL": "", "FLEET_ACCOUNTS_CSV": ""}):
                    controller = FleetController(Path(directory) / "config.json", cfg)
                    controller.sources_runtime_dir = Path(directory) / "sources"
                    account = FleetAccount("a1", "127.0.0.1", "pppoe-test", weight=1)
                    original = EngineProcess

                    def local_engine(*args, **kwargs):
                        kwargs["reject_private_destinations"] = False
                        return original(*args, **kwargs)

                    try:
                        with patch("steam_pumper.fleet.EngineProcess", side_effect=local_engine):
                            controller._start(account, datetime.now(ZoneInfo("Asia/Shanghai")))
                        engine = controller.active["a1"].engine
                        pid = engine.process.pid
                        self.wait_for(lambda: "/old" in requests, engine)
                        self.wait_for(lambda: engine.state.total_bytes > 0, engine)
                        # A slow but live real subprocess must survive the
                        # supervisor's slow window and still accept hot reloads.
                        now = datetime.now(ZoneInfo("Asia/Shanghai"))
                        active = controller.active["a1"]
                        active.started_at = now - timedelta(seconds=181)
                        active.rate_samples.clear()
                        active.rate_samples.append((active.started_at, 0))
                        self.assertFalse(controller._poll_engines(now))
                        self.assertIs(controller.active["a1"].engine, engine)
                        self.assertNotIn("a1", controller.account_cooldowns)
                        self.assertIsNotNone(active.last_slow_notice_at)
                        self.assertEqual(engine.process.pid, pid)
                        for path in ("/new", "/newer"):
                            controller.update_config({"source_pool": [base + path]})
                            self.wait_for(lambda: path in requests and engine.source_generation_confirmed(controller.pending_source_generation), engine)
                            controller._advance_source_update(time.monotonic())
                            self.assertEqual(controller.source_list_status()["apply_state"], "applied")
                            self.assertEqual(engine.process.pid, pid)
                            self.assertIsNone(engine.process.poll())
                    finally:
                        controller.shutdown()
        finally:
            server.shutdown()
            server.server_close()

    def wait_for(self, condition, engine):
        finish = time.monotonic() + 8
        while time.monotonic() < finish:
            engine.poll()
            if condition():
                return
            time.sleep(0.02)
        self.fail(f"Go reload did not complete: {engine.state}")
