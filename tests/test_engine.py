import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from steam_pumper.config import IkuaiLineConfig
from steam_pumper.topology import LogicalLine


class EngineTests(unittest.TestCase):
    def test_poll_does_not_start_an_engine_that_was_never_started(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["http://a.test/file"],
            lambda _message: None,
        )

        with patch.object(engine, "start") as start:
            engine.poll(now=100)

        start.assert_not_called()

    def test_engine_command_binds_only_when_line_has_an_ip(self):
        from steam_pumper.engine import build_engine_command

        cfg = IkuaiLineConfig(connections_per_line=4, max_connections_per_line=12)
        unbound = build_engine_command(cfg, LogicalLine("line-1", 400), ["http://a.test/file"])
        bound = build_engine_command(
            cfg,
            LogicalLine("line-1", 400, "192.168.1.233"),
            ["http://a.test/file"],
        )

        self.assertNotIn("--bind-ip", unbound)
        self.assertEqual(bound[bound.index("--bind-ip") + 1], "192.168.1.233")
        self.assertEqual(unbound[unbound.index("--connections") + 1], "4")
        self.assertEqual(unbound[unbound.index("--max-connections") + 1], "12")
        self.assertEqual(unbound[unbound.index("--line-id") + 1], "line-1")

    def test_multi_ip_engine_command_uses_source_file_and_private_destination_guard(self):
        from steam_pumper.engine import build_engine_command

        command = build_engine_command(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400, "192.168.1.233"),
            ["http://fallback.test/file"],
            sources_file=Path("/run/pumper/line-1.sources.json"),
            reject_private_destinations=True,
        )

        self.assertEqual(
            command[command.index("--sources-file") + 1],
            "/run/pumper/line-1.sources.json",
        )
        self.assertIn("--reject-private-destinations", command)
        self.assertNotIn("http://fallback.test/file", command)

    def test_engine_parses_status_without_a_reader_thread(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(connections_per_line=4),
            LogicalLine("line-1", 400),
            ["http://a.test/file", "http://bad.test/file"],
            lambda _message: None,
        )

        engine._consume_line(
            '{"type":"status","line_id":"line-1","bytes":1048576,'
            '"connections":4,"url":"http://a.test/file"}'
        )
        engine._consume_line(
            '{"type":"source","line_id":"line-1","url":"http://bad.test/file",'
            '"error":"timeout"}'
        )

        self.assertEqual(engine.state.total_bytes, 1_048_576)
        self.assertEqual(engine.state.connections, 4)
        self.assertTrue(engine.state.has_metrics)
        self.assertEqual(engine.state.current_source, "http://a.test/file")
        self.assertEqual(engine.state.source_failures["http://bad.test/file"], 1)
        self.assertIn("timeout", engine.state.last_error)

    def test_engine_retains_structured_source_quarantine_and_recovery(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["http://bad.test/file"],
            lambda _message: None,
        )
        engine._consume_line(
            '{"type":"source","line_id":"line-1","url":"http://bad.test/file",'
            '"state":"quarantined","consecutive_failures":3,'
            '"retry_after":"2026-07-20T08:10:00Z","retry_in_seconds":600,'
            '"error":"connection refused"}'
        )

        source = engine.state.source_states["http://bad.test/file"]
        self.assertEqual(source.state, "quarantined")
        self.assertEqual(source.consecutive_failures, 3)
        self.assertEqual(source.retry_after, "2026-07-20T08:10:00Z")
        self.assertEqual(source.retry_in_seconds, 600)
        self.assertEqual(source.last_error, "connection refused")

        engine._consume_line(
            '{"type":"source","line_id":"line-1","url":"http://bad.test/file",'
            '"state":"healthy","recovered":true}'
        )

        recovered = engine.state.source_states["http://bad.test/file"]
        self.assertEqual(recovered.state, "healthy")
        self.assertEqual(recovered.consecutive_failures, 0)
        self.assertEqual(recovered.retry_in_seconds, 0)
        self.assertEqual(engine.state.source_failures["http://bad.test/file"], 0)

    def test_engine_ignores_events_for_another_line(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["http://a.test/file"],
            lambda _message: None,
        )

        engine._consume_line('{"type":"status","line_id":"line-2","bytes":99,"connections":8}')

        self.assertFalse(engine.state.has_metrics)
        self.assertEqual(engine.state.total_bytes, 0)

    def test_engine_keeps_total_bytes_monotonic_across_process_restart(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["http://a.test/file"],
            lambda _message: None,
        )
        engine._consume_line('{"type":"status","line_id":"line-1","bytes":100,"connections":8}')
        engine._schedule_restart("test restart", now=1.0)
        engine._consume_line('{"type":"status","line_id":"line-1","bytes":25,"connections":8}')

        self.assertEqual(engine.state.total_bytes, 125)

    def test_engine_hot_scales_without_restarting_process(self):
        from steam_pumper.engine import EngineProcess

        process = Mock(pid=321)
        process.poll.return_value = None
        engine = EngineProcess(
            IkuaiLineConfig(connections_per_line=4),
            LogicalLine("line-1", 400),
            ["http://a.test/file"],
            lambda _message: None,
        )
        engine.process = process

        with patch("steam_pumper.engine.os.kill") as kill:
            engine.set_connections(6)

        self.assertEqual(engine.process.pid, 321)
        self.assertEqual(kill.call_count, 2)
        self.assertTrue(all(call.args == (321, signal.SIGUSR1) for call in kill.call_args_list))
        self.assertEqual(engine.state.connections, 6)

    def test_engine_start_uses_nonblocking_combined_status_pipe(self):
        from steam_pumper.engine import EngineProcess

        process = Mock(pid=123, stdout=None)
        process.poll.return_value = None
        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["http://a.test/file"],
            lambda _message: None,
        )

        with patch("steam_pumper.engine.subprocess.Popen", return_value=process) as popen:
            engine.start()

        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_engine_can_start_again_after_an_explicit_stop(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["http://a.test/file"],
            lambda _message: None,
        )
        engine.stop()
        process = Mock(pid=456, stdout=None)
        process.poll.return_value = None

        with patch("steam_pumper.engine.subprocess.Popen", return_value=process) as popen:
            engine.start()

        popen.assert_called_once()
        self.assertEqual(engine.state.status, "downloading")

    def test_set_sources_atomically_updates_file_and_sighups_live_process(self):
        from steam_pumper.engine import EngineProcess

        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = Path(tmpdir) / "line-1.sources.json"
            process = Mock(pid=789)
            process.poll.return_value = None
            engine = EngineProcess(
                IkuaiLineConfig(),
                LogicalLine("line-1", 400),
                ["http://old.test/file"],
                lambda _message: None,
                sources_file=source_file,
                reject_private_destinations=True,
            )
            engine.process = process

            with patch("steam_pumper.engine.os.kill") as kill:
                changed = engine.set_sources(["https://new.test/a", "https://new.test/b"])

            self.assertTrue(changed)
            self.assertEqual(json.loads(source_file.read_text(encoding="utf-8")), ["https://new.test/a", "https://new.test/b"])
            kill.assert_called_once_with(789, signal.SIGHUP)
            self.assertEqual(engine.process.pid, 789)

    def test_set_sources_does_not_signal_stopped_or_unchanged_engine(self):
        from steam_pumper.engine import EngineProcess

        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = Path(tmpdir) / "line-1.sources.json"
            engine = EngineProcess(
                IkuaiLineConfig(),
                LogicalLine("line-1", 400),
                ["https://same.test/file"],
                lambda _message: None,
                sources_file=source_file,
            )

            with patch("steam_pumper.engine.os.kill") as kill:
                changed = engine.set_sources(["https://same.test/file"])

            self.assertFalse(changed)
            self.assertEqual(json.loads(source_file.read_text(encoding="utf-8")), ["https://same.test/file"])
            kill.assert_not_called()

    def test_set_sources_keeps_previous_list_when_atomic_write_fails(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["https://old.test/file"],
            lambda _message: None,
            sources_file="/run/pumper/line-1.sources.json",
        )
        process = Mock(pid=999)
        process.poll.return_value = None
        engine.process = process

        with (
            patch.object(engine, "_write_sources_file", side_effect=OSError("read only")),
            patch("steam_pumper.engine.os.kill") as kill,
            self.assertRaisesRegex(OSError, "read only"),
        ):
            engine.set_sources(["https://new.test/file"])

        self.assertEqual(engine.sources, ["https://old.test/file"])
        kill.assert_not_called()

    def test_stage_sources_waits_for_matching_helper_ack_and_prunes_removed_state(self):
        from steam_pumper.engine import EngineProcess, SourceRuntimeState

        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = Path(tmpdir) / "line-1.sources.json"
            engine = EngineProcess(
                IkuaiLineConfig(),
                LogicalLine("line-1", 400),
                ["https://old.test/file", "https://keep.test/file"],
                lambda _message: None,
                sources_file=source_file,
            )
            process = Mock(pid=789)
            process.poll.return_value = None
            engine.process = process
            engine.state.source_failures["https://old.test/file"] = 3
            engine.state.source_states["https://old.test/file"] = SourceRuntimeState(state="quarantined")

            with patch("steam_pumper.engine.os.kill"):
                engine.stage_sources(["https://keep.test/file", "https://new.test/file"], "20260721031700")

            self.assertEqual(
                json.loads(source_file.read_text(encoding="utf-8")),
                {
                    "generation": "20260721031700",
                    "sources": ["https://keep.test/file", "https://new.test/file"],
                },
            )
            self.assertFalse(engine.source_generation_confirmed("20260721031700"))
            engine._consume_line(
                '{"type":"source-list","line_id":"line-1","state":"reloaded",'
                '"generation":"20260721031700"}'
            )

            self.assertTrue(engine.source_generation_confirmed("20260721031700"))
            self.assertNotIn("https://old.test/file", engine.state.source_failures)
            self.assertNotIn("https://old.test/file", engine.state.source_states)

    def test_source_list_error_or_signal_failure_keeps_generation_pending_for_retry(self):
        from steam_pumper.engine import EngineProcess

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = EngineProcess(
                IkuaiLineConfig(),
                LogicalLine("line-1", 400),
                ["https://old.test/file"],
                lambda _message: None,
                sources_file=Path(tmpdir) / "sources.json",
            )
            process = Mock(pid=999)
            process.poll.return_value = None
            engine.process = process

            with patch("steam_pumper.engine.os.kill", side_effect=OSError("signal denied")):
                with self.assertRaisesRegex(OSError, "signal denied"):
                    engine.stage_sources(["https://new.test/file"], "rev-1")
            self.assertEqual(engine.state.pending_source_generation, "rev-1")

            engine._consume_line(
                '{"type":"source-list","line_id":"line-1","generation":"rev-1",'
                '"error":"invalid sources file"}'
            )
            self.assertEqual(engine.state.pending_source_generation, "rev-1")
            self.assertIn("invalid sources", engine.state.source_reload_error)

    def test_late_removed_source_event_cannot_reinsert_python_state(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(),
            LogicalLine("line-1", 400),
            ["https://keep.test/file"],
            lambda _message: None,
        )
        engine._consume_line(
            '{"type":"source","line_id":"line-1","url":"https://removed.test/file",'
            '"error":"late timeout","consecutive_failures":3}'
        )

        self.assertNotIn("https://removed.test/file", engine.state.source_failures)

    def test_restart_preserves_pending_generation_in_sources_file(self):
        from steam_pumper.engine import EngineProcess

        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = Path(tmpdir) / "sources.json"
            engine = EngineProcess(
                IkuaiLineConfig(),
                LogicalLine("line-1", 400),
                ["https://old.test/file"],
                lambda _message: None,
                sources_file=source_file,
            )
            live = Mock(pid=999)
            live.poll.return_value = None
            engine.process = live
            with patch("steam_pumper.engine.os.kill", side_effect=OSError("gone")):
                with self.assertRaises(OSError):
                    engine.stage_sources(["https://new.test/file"], "rev-2")
            engine.process = None
            replacement = Mock(pid=1000, stdout=None)
            replacement.poll.return_value = None
            with patch("steam_pumper.engine.subprocess.Popen", return_value=replacement):
                engine.start()

            self.assertEqual(
                json.loads(source_file.read_text(encoding="utf-8")),
                {"generation": "rev-2", "sources": ["https://new.test/file"]},
            )


if __name__ == "__main__":
    unittest.main()
