import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from steam_pumper.config import PumperConfig, save_config
from steam_pumper.controller import PumperController


class ControllerTests(unittest.TestCase):
    def test_metrics_snapshot_reports_target_and_daily_goal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            save_config(config_path, PumperConfig(target_mbps=800, start_time="00:00", end_time="18:00"))
            controller = PumperController(config_path)
            controller.tracker.record(100.0, 1_000_000)
            controller.tracker.record(101.0, 101_000_000)

            metrics = controller.metrics()

        self.assertEqual(metrics["target_mbps"], 800)
        self.assertEqual(round(metrics["current_mbps"]), 800)
        self.assertEqual(round(metrics["avg10_mbps"]), 800)
        self.assertEqual(metrics["theoretical_window_bytes"], 6_480_000_000_000)
        self.assertEqual(metrics["minimum_accept_bytes"], 5_184_000_000_000)

    def test_public_http_start_does_not_block_on_startup_stagger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            save_config(
                config_path,
                PumperConfig(
                    line_count=2,
                    connections_per_line=1,
                    startup_stagger_seconds=2,
                    source_pool=["http://example.test/file"],
                ),
            )
            controller = PumperController(config_path)

            class FakeWorker:
                def __init__(self, *_args):
                    pass

                def start(self):
                    return None

                def stop(self):
                    return None

                def join(self, timeout=None):
                    return None

            started = time.monotonic()
            with patch("steam_pumper.controller.DownloadWorker", FakeWorker), patch.object(
                controller,
                "resolve_sources",
                return_value=[],
            ):
                controller.start_downloads()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
