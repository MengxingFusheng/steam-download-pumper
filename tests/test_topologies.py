import os
import subprocess
import unittest
from unittest.mock import patch

from steam_pumper.config import IkuaiLineConfig, MultiIPConfig
from steam_pumper.topology import (
    IkuaiLineTopology,
    MultiIPTopology,
    allocate_targets,
    apply_ipv4_addresses,
    topology_for,
)


class TopologyTests(unittest.TestCase):
    def test_allocate_targets_distributes_remainder_from_first_line(self):
        self.assertEqual(allocate_targets(801, 2), [401, 400])
        self.assertEqual(allocate_targets(5, 3), [2, 2, 1])

    def test_ikuai_produces_one_unbound_line(self):
        lines = IkuaiLineTopology().lines(IkuaiLineConfig(target_mbps=400))

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].line_id, "line-1")
        self.assertEqual(lines[0].target_mbps, 400)
        self.assertEqual(lines[0].bind_ip, "")

    def test_multi_ip_produces_one_bound_line_per_address(self):
        cfg = MultiIPConfig(
            target_mbps=801,
            line_count=2,
            lan_ips=["192.168.1.233", "192.168.1.234"],
        )

        lines = MultiIPTopology().lines(cfg)

        self.assertEqual(
            [(line.line_id, line.target_mbps, line.bind_ip) for line in lines],
            [
                ("line-1", 401, "192.168.1.233"),
                ("line-2", 400, "192.168.1.234"),
            ],
        )

    def test_topology_for_supports_exactly_two_names(self):
        self.assertIsInstance(topology_for("ikuai_line"), IkuaiLineTopology)
        self.assertIsInstance(topology_for("multi_ip"), MultiIPTopology)
        with self.assertRaisesRegex(ValueError, "unsupported topology"):
            topology_for("single_ip")

    @patch("steam_pumper.topology.subprocess.run")
    def test_address_application_skips_existing_address(self, run):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "2: eth0    inet 192.168.1.233/24 brd 192.168.1.255 scope global eth0\\n"
            ),
            stderr="",
        )

        with patch.dict(os.environ, {"APPLY_LAN_IPS": "true"}, clear=False):
            apply_ipv4_addresses(
                ["192.168.1.233", "192.168.1.234"],
                "eth0",
                "24",
            )

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["ip", "-4", "-o", "addr", "show", "dev", "eth0"],
                ["ip", "addr", "add", "192.168.1.234/24", "dev", "eth0"],
            ],
        )

    @patch("steam_pumper.topology.subprocess.run")
    def test_address_application_reports_failed_address(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CalledProcessError(2, ["ip", "addr", "add"]),
        ]

        with patch.dict(os.environ, {"APPLY_LAN_IPS": "true"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "192.168.1.234"):
                apply_ipv4_addresses(["192.168.1.234"], "eth0", "24")

    @patch("steam_pumper.topology.subprocess.run")
    def test_address_application_can_be_disabled(self, run):
        with patch.dict(os.environ, {"APPLY_LAN_IPS": "false"}, clear=False):
            apply_ipv4_addresses(["192.168.1.233"], "eth0", "24")

        run.assert_not_called()

    @patch("steam_pumper.topology.subprocess.run")
    def test_address_application_validates_all_addresses_before_commands(self, run):
        with patch.dict(os.environ, {"APPLY_LAN_IPS": "true"}, clear=False):
            with self.assertRaisesRegex(ValueError, "IPv4"):
                apply_ipv4_addresses(["192.168.1.233", "2001:db8::1"], "eth0", "24")

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
