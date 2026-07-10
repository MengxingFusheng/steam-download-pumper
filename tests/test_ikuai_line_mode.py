import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class IkuaiLineModeTests(unittest.TestCase):
    def test_line_config_defaults_to_single_container_single_line(self):
        from steam_pumper.line_config import LineConfig

        cfg = LineConfig().validate()

        self.assertEqual(cfg.target_mbps, 400)
        self.assertEqual(cfg.connections, 8)
        self.assertEqual(cfg.max_connections, 12)
        self.assertEqual(cfg.start_time, "00:00")
        self.assertEqual(cfg.end_time, "18:00")
        self.assertGreater(len(cfg.source_pool), 0)

    def test_line_config_rejects_non_http_and_credentialed_sources(self):
        from steam_pumper.line_config import LineConfig

        for url in ("file:///etc/passwd", "ftp://example.test/file", "http://user:pass@example.test/file"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LineConfig(source_pool=[url]).validate()

    def test_line_config_rejects_removed_multiline_and_steam_fields(self):
        from steam_pumper.line_config import load_line_config

        rejected = {
            "LINE_COUNT": "2",
            "EGRESS_MODE": "multi_ip",
            "LAN_IPS": "192.168.1.233,192.168.1.234",
        }
        for key, value in rejected.items():
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    load_line_config("/path/does/not/exist.json", {key: value})

    def test_saved_line_config_wins_over_image_default_environment(self):
        from steam_pumper.line_config import LineConfig, load_line_config, save_line_config

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            save_line_config(config_path, LineConfig(target_mbps=720, connections=4))

            cfg = load_line_config(
                config_path,
                {"TARGET_MBPS": "400", "CONNECTIONS": "8", "MAX_CONNECTIONS": "12"},
            )

        self.assertEqual(cfg.target_mbps, 720)
        self.assertEqual(cfg.connections, 4)

    def test_line_worker_autoscale_is_capped_at_twelve(self):
        from steam_pumper.line_config import LineConfig
        from steam_pumper.line_worker import next_line_worker_count

        cfg = LineConfig(connections=8, max_connections=99, target_mbps=400).validate()

        self.assertEqual(cfg.max_connections, 12)
        self.assertEqual(next_line_worker_count(cfg, 12, 100), 12)
        self.assertEqual(next_line_worker_count(cfg, 8, 100), 9)

    def test_line_download_worker_does_not_allocate_a_python_thread(self):
        from steam_pumper.line_config import LineConfig
        from steam_pumper.line_worker import LineDownloadWorker, LineWorkerSpec, LineWorkerState

        worker = LineDownloadWorker(
            LineConfig(),
            LineWorkerSpec(worker_id=1, target="http://example.test/file"),
            LineWorkerState(worker_id=1),
            lambda _message: None,
        )

        self.assertNotIsInstance(worker, threading.Thread)

    def test_line_download_worker_uses_a_nonblocking_status_pipe(self):
        from steam_pumper.line_config import LineConfig
        from steam_pumper.line_worker import LineDownloadWorker, LineWorkerSpec, LineWorkerState

        process = Mock(pid=123)
        process.poll.return_value = None
        process.stdout = None
        worker = LineDownloadWorker(
            LineConfig(),
            LineWorkerSpec(worker_id=1, target="http://example.test/file"),
            LineWorkerState(worker_id=1),
            lambda _message: None,
        )

        with patch("steam_pumper.line_worker.subprocess.Popen", return_value=process) as popen:
            worker.start()

        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--connections") + 1], "8")
        self.assertEqual(command[command.index("--max-connections") + 1], "12")

    def test_line_engine_scales_connections_without_restarting_process(self):
        from steam_pumper.line_config import LineConfig
        from steam_pumper.line_worker import LineDownloadWorker, LineWorkerSpec, LineWorkerState

        process = Mock(pid=123)
        process.poll.return_value = None
        worker = LineDownloadWorker(
            LineConfig(connections=2),
            LineWorkerSpec(worker_id=1, target="http://example.test/file"),
            LineWorkerState(worker_id=1),
            lambda _message: None,
            connections=2,
        )
        worker.process = process

        with patch("steam_pumper.line_worker.os.kill") as kill:
            worker.set_connection_count(4)

        self.assertEqual(kill.call_count, 2)
        self.assertTrue(all(call.args == (123, signal.SIGUSR1) for call in kill.call_args_list))
        self.assertEqual(worker.state.connections, 4)

    def test_line_engine_drains_source_errors_without_a_reader_thread(self):
        from steam_pumper.line_config import LineConfig
        from steam_pumper.line_worker import LineDownloadWorker, LineWorkerSpec, LineWorkerState

        process = Mock(pid=123)
        process.poll.return_value = None
        process.stdout.read.return_value = b"worker=1-1 url=http://bad.test/file error=timeout\n"
        worker = LineDownloadWorker(
            LineConfig(),
            LineWorkerSpec(worker_id=1, target="http://bad.test/file"),
            LineWorkerState(worker_id=1),
            lambda _message: None,
        )
        worker.process = process

        worker.poll(now=1.0)

        self.assertEqual(worker.source_failures["http://bad.test/file"], 1)
        self.assertIn("timeout", worker.state.last_error)

    def test_line_metrics_report_daily_volume_goal(self):
        from steam_pumper.line_config import LineConfig, save_line_config
        from steam_pumper.line_controller import LineController

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            save_line_config(config_path, LineConfig(target_mbps=800, start_time="00:00", end_time="18:00"))
            controller = LineController(config_path)
            controller.tracker.today_bytes = 5_184_000_000_000

            metrics = controller.metrics()

        self.assertEqual(metrics["theoretical_window_bytes"], 6_480_000_000_000)
        self.assertEqual(metrics["minimum_accept_bytes"], 5_184_000_000_000)
        self.assertEqual(metrics["daily_target_percent"], 80.0)

    def test_line_config_api_rejects_unknown_fields(self):
        from steam_pumper.line_config import LineConfig, save_line_config
        from steam_pumper.line_controller import LineController

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            save_line_config(config_path, LineConfig())
            controller = LineController(config_path)

            with self.assertRaisesRegex(ValueError, "unsupported configuration fields"):
                controller.update_config({"line_count": 2})

    def test_ikuai_web_omits_removed_features(self):
        from steam_pumper.line_web import HTML

        forbidden = ["Steam", "steam", "EGRESS_MODE", "LAN_IPS", "line_count", "爱快 WAN", "download_mode"]
        for text in forbidden:
            with self.subTest(text=text):
                self.assertNotIn(text, HTML)
        self.assertIn('name="target_mbps"', HTML)
        self.assertIn('name="connections"', HTML)
        self.assertIn('name="source_pool"', HTML)

    def test_ikuai_line_dockerfile_is_slim_and_not_steam_based(self):
        dockerfile = (ROOT / "Dockerfile.ikuai-line").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.13-slim", dockerfile)
        self.assertIn("COPY steam_pumper/line_", dockerfile)
        self.assertIn("CMD", dockerfile)
        self.assertIn("PYTHON_COLORS=0", dockerfile)
        self.assertIn("NO_COLOR=1", dockerfile)
        self.assertIn("GOMAXPROCS=2", dockerfile)
        self.assertNotIn("cm2network/steamcmd", dockerfile)
        self.assertNotIn("steamcmd", dockerfile.lower())
        self.assertNotIn("trickle", dockerfile.lower())
        self.assertNotIn("iproute2", dockerfile.lower())

    def test_line_controller_logs_thread_resource_failures_without_crashing(self):
        from steam_pumper.line_config import LineConfig, save_line_config
        from steam_pumper.line_controller import LineController

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            save_line_config(config_path, LineConfig())
            controller = LineController(config_path)

            def fail_start():
                raise RuntimeError("can't start new thread")

            controller.scheduler_thread.start = fail_start
            controller.metrics_thread.start = fail_start

            controller.start_scheduler()

            self.assertTrue(any("can't start new thread" in line for line in controller.logs))

    def test_publish_script_supports_ghcr_dockerhub_and_release_tar(self):
        script = (ROOT / "publish-ikuai-line.sh").read_text(encoding="utf-8")

        self.assertIn("ghcr.io/mengxingfusheng/ikuai-line-pumper", script)
        self.assertIn("DOCKERHUB_IMAGE", script)
        self.assertIn("docker save", script)
        self.assertIn("gh release", script)
        self.assertIn("Dockerfile.ikuai-line", script)

    def test_publish_script_has_valid_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(ROOT / "publish-ikuai-line.sh")], capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
