import signal
import subprocess
import unittest
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

    def test_engine_parses_status_without_a_reader_thread(self):
        from steam_pumper.engine import EngineProcess

        engine = EngineProcess(
            IkuaiLineConfig(connections_per_line=4),
            LogicalLine("line-1", 400),
            ["http://a.test/file"],
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


if __name__ == "__main__":
    unittest.main()
