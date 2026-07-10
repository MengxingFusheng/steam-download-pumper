import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from steam_pumper.controller import PumperController


class ControllerTests(unittest.TestCase):
    def test_controller_builds_one_runtime_per_logical_line(self):
        cases = (
            ("ikuai_line", {}, 1),
            (
                "multi_ip",
                {"LINE_COUNT": "2", "LAN_IPS": "192.168.1.233,192.168.1.234"},
                2,
            ),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            for topology_name, env, expected_lines in cases:
                with self.subTest(topology=topology_name):
                    controller = PumperController(topology_name, Path(tmpdir) / f"{topology_name}.json", env=env)
                    self.assertEqual(len(controller.lines), expected_lines)
                    self.assertEqual(len(controller.line_runtimes), expected_lines)

    def test_start_applies_topology_before_starting_engines(self):
        events = []
        with tempfile.TemporaryDirectory() as tmpdir:
            controller = PumperController("ikuai_line", Path(tmpdir) / "config.json", env={})
            controller.topology.apply = lambda _cfg, _log: events.append("apply")
            for runtime in controller.line_runtimes.values():
                runtime.engine.start = lambda: events.append("start")

            controller.start_downloads()

        self.assertEqual(events, ["apply", "start"])

    def test_multi_ip_scales_slow_line_without_scaling_fast_line(self):
        env = {"LINE_COUNT": "2", "LAN_IPS": "192.168.1.233,192.168.1.234"}
        with tempfile.TemporaryDirectory() as tmpdir:
            controller = PumperController("multi_ip", Path(tmpdir) / "config.json", env=env)
            slow = controller.line_runtimes["line-1"]
            fast = controller.line_runtimes["line-2"]
            for runtime in (slow, fast):
                runtime.engine.state.has_metrics = True
                runtime.engine.state.status = "downloading"
            slow.tracker.record(0, 0)
            slow.tracker.record(10, 10_000_000)
            fast.tracker.record(0, 0)
            fast.tracker.record(10, 500_000_000)

            controller._scale_lines(now=20)

        self.assertEqual(slow.desired_connections, 9)
        self.assertEqual(fast.desired_connections, 8)

    def test_interface_metrics_are_fallback_before_helper_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            controller = PumperController("ikuai_line", Path(tmpdir) / "config.json", env={})
            controller.interface_tracker.record(0, 0)
            controller.interface_tracker.record(1, 10_000_000)

            metrics = controller.metrics()

        self.assertEqual(metrics["current_mbps"], 80.0)
        self.assertFalse(metrics["lines"][0]["metrics_available"])
        self.assertEqual(metrics["lines"][0]["current_mbps"], 0.0)

    def test_metrics_snapshot_reports_target_and_daily_goal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            controller = PumperController(
                "ikuai_line",
                Path(tmpdir) / "config.json",
                env={"TARGET_MBPS": "800", "START_TIME": "00:00", "END_TIME": "18:00"},
            )
            runtime = controller.line_runtimes["line-1"]
            runtime.engine.state.has_metrics = True
            runtime.tracker.today_bytes = 5_184_000_000_000

            metrics = controller.metrics()

        self.assertEqual(metrics["theoretical_window_bytes"], 6_480_000_000_000)
        self.assertEqual(metrics["minimum_accept_bytes"], 5_184_000_000_000)
        self.assertEqual(metrics["daily_target_percent"], 80.0)

    def test_config_update_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            controller = PumperController("ikuai_line", Path(tmpdir) / "config.json", env={})

            with self.assertRaisesRegex(ValueError, "unsupported configuration fields"):
                controller.update_config({"line_count": 2})

    def test_common_config_update_hot_scales_existing_engine(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            controller = PumperController("ikuai_line", Path(tmpdir) / "config.json", env={})
            runtime = controller.line_runtimes["line-1"]
            with patch.object(runtime.engine, "set_connections") as resize:
                controller.update_config({"connections_per_line": 6})

        resize.assert_called_once_with(6)
        self.assertIs(controller.line_runtimes["line-1"], runtime)


if __name__ == "__main__":
    unittest.main()
