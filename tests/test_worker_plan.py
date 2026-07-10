import unittest

from steam_pumper.config import PumperConfig
from steam_pumper.worker import SourceEndpoint, build_download_command, build_worker_plan


class WorkerPlanTests(unittest.TestCase):
    def test_worker_count_uses_line_count_times_connections_per_line(self):
        cfg = PumperConfig(line_count=3, connections_per_line=5, rate_limit_mbps=900)

        plan = build_worker_plan(cfg)

        self.assertEqual(len(plan), 15)
        self.assertEqual([worker.line_index for worker in plan[:6]], [1, 2, 3, 1, 2, 3])
        self.assertTrue(all(worker.rate_limit_kbps == 60000 for worker in plan))

    def test_worker_plan_can_grow_to_max_connections_per_line(self):
        cfg = PumperConfig(line_count=2, connections_per_line=4, max_connections_per_line=6)

        plan = build_worker_plan(cfg, worker_count=12)

        self.assertEqual(len(plan), 12)
        self.assertEqual([worker.line_index for worker in plan[-4:]], [1, 2, 1, 2])

    def test_worker_plan_rejects_more_than_configured_max_workers(self):
        cfg = PumperConfig(line_count=2, connections_per_line=4, max_connections_per_line=6)

        with self.assertRaises(ValueError):
            build_worker_plan(cfg, worker_count=13)

    def test_worker_rate_limit_is_disabled_when_requested(self):
        cfg = PumperConfig(line_count=2, connections_per_line=2, rate_limit_enabled=False)

        plan = build_worker_plan(cfg)

        self.assertEqual([worker.rate_limit_kbps for worker in plan], [None, None, None, None])

    def test_public_http_command_downloads_to_go_discarder(self):
        cfg = PumperConfig(source_pool=["https://example.test/file.bin"])

        command = build_download_command(cfg, "https://example.test/file.bin", 3)

        self.assertEqual(command[0], "discarder")
        self.assertIn("--worker-id", command)
        self.assertIn("--min-session-seconds", command)
        self.assertIn("--restart-jitter-seconds", command)
        self.assertIn("https://example.test/file.bin", command)

    def test_public_http_command_can_bind_to_line_source_ip(self):
        cfg = PumperConfig(source_pool=["https://example.test/file.bin"])

        command = build_download_command(cfg, "https://example.test/file.bin", 3, source_ip="192.168.1.234")

        self.assertIn("--bind-ip", command)
        self.assertIn("192.168.1.234", command)

    def test_worker_plan_assigns_source_ip_per_line_in_multi_ip_mode(self):
        cfg = PumperConfig(
            line_count=3,
            connections_per_line=2,
            egress_mode="multi_ip",
            lan_ips=["192.168.1.233", "192.168.1.234", "192.168.1.235"],
        )

        plan = build_worker_plan(cfg)

        self.assertEqual(
            [(worker.line_index, worker.source_ip) for worker in plan],
            [
                (1, "192.168.1.233"),
                (2, "192.168.1.234"),
                (3, "192.168.1.235"),
                (1, "192.168.1.233"),
                (2, "192.168.1.234"),
                (3, "192.168.1.235"),
            ],
        )

    def test_worker_plan_distributes_public_sources_by_remote_ip(self):
        cfg = PumperConfig(line_count=2, connections_per_line=6, source_pool=["https://a.test/file"])
        sources = [
            SourceEndpoint(url="https://a.test/file", ip="1.1.1.1"),
            SourceEndpoint(url="https://b.test/file", ip="2.2.2.2"),
            SourceEndpoint(url="https://c.test/file", ip="3.3.3.3"),
        ]

        plan = build_worker_plan(cfg, sources=sources)
        counts = {}
        for worker in plan:
            counts[worker.target_ip] = counts.get(worker.target_ip, 0) + 1

        self.assertEqual(set(counts), {"1.1.1.1", "2.2.2.2", "3.3.3.3"})
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)


if __name__ == "__main__":
    unittest.main()
