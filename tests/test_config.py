import json
import math
import os
import tempfile
import unittest
from datetime import time
from pathlib import Path
from unittest.mock import patch

from steam_pumper.config import (
    MAX_CONNECTIONS_PER_LINE,
    CommonConfig,
    IkuaiLineConfig,
    MultiIPConfig,
    default_source_pool,
    load_config,
    save_config,
    validate_source_pool,
)


class ConfigTests(unittest.TestCase):
    def test_supported_configs_share_names_and_expected_defaults(self):
        shared_names = {
            "target_mbps",
            "connections_per_line",
            "max_connections_per_line",
            "rate_limit_enabled",
            "start_time",
            "end_time",
            "source_pool",
            "loop_pause_seconds",
            "startup_stagger_seconds",
            "worker_min_session_seconds",
            "worker_restart_jitter_seconds",
            "schedule_poll_seconds",
            "log_level",
        }

        self.assertTrue(shared_names.issubset(CommonConfig.__dataclass_fields__))
        ikuai = IkuaiLineConfig()
        multi_ip = MultiIPConfig()
        self.assertEqual(MAX_CONNECTIONS_PER_LINE, 12)
        self.assertEqual(
            (ikuai.topology, ikuai.target_mbps, ikuai.connections_per_line, ikuai.max_connections_per_line),
            ("ikuai_line", 400, 8, 12),
        )
        self.assertEqual(
            (
                multi_ip.topology,
                multi_ip.target_mbps,
                multi_ip.connections_per_line,
                multi_ip.max_connections_per_line,
                multi_ip.line_count,
                multi_ip.lan_ips,
            ),
            ("multi_ip", 800, 8, 12, 2, ["192.168.1.233", "192.168.1.234"]),
        )
        for topology_field in ("line_count", "lan_ip", "lan_ips"):
            with self.subTest(topology_field=topology_field):
                self.assertFalse(hasattr(ikuai, topology_field))

    def test_hard_cap_is_rejected_for_both_topologies(self):
        for config_type in (IkuaiLineConfig, MultiIPConfig):
            with self.subTest(config_type=config_type.__name__):
                with self.assertRaisesRegex(ValueError, "at most 12"):
                    config_type(max_connections_per_line=13).validate()

    def test_invalid_connection_relationships_are_rejected(self):
        for config_type in (IkuaiLineConfig, MultiIPConfig):
            for kwargs in (
                {"connections_per_line": 0},
                {"connections_per_line": 13},
                {"max_connections_per_line": 0},
                {"connections_per_line": 9, "max_connections_per_line": 8},
            ):
                with self.subTest(config_type=config_type.__name__, kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        config_type(**kwargs).validate()

    def test_schedule_and_time_window_are_shared(self):
        with self.assertRaisesRegex(ValueError, "start_time"):
            IkuaiLineConfig(start_time="25:00").validate()
        with self.assertRaisesRegex(ValueError, "end_time"):
            MultiIPConfig(end_time="6:00").validate()

        day = IkuaiLineConfig(start_time="08:00", end_time="18:00")
        night = MultiIPConfig(start_time="22:00", end_time="06:00")
        self.assertTrue(day.is_within_window(time(12, 0)))
        self.assertFalse(day.is_within_window(time(20, 0)))
        self.assertTrue(night.is_within_window(time(3, 0)))
        self.assertFalse(night.is_within_window(time(12, 0)))

    def test_source_pool_rejects_invalid_non_http_and_credentialed_urls(self):
        invalid_pools = (
            ["not-a-url"],
            ["ftp://example.test/file"],
            ["https://user:secret@example.test/file"],
        )
        for source_pool in invalid_pools:
            with self.subTest(source_pool=source_pool):
                with self.assertRaises(ValueError):
                    IkuaiLineConfig(source_pool=source_pool).validate()

    def test_default_source_pool_contains_only_verified_endpoints(self):
        sources = default_source_pool()

        self.assertEqual(
            sources,
            [
                "http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg",
                "http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg",
                "https://mirror.iscas.ac.cn/ubuntu-releases/24.04.4/ubuntu-24.04.4-live-server-amd64.iso",
                "https://mirrors.pku.edu.cn/ubuntu-releases/24.04.4/ubuntu-24.04.4-live-server-amd64.iso",
                "https://mirrors.huaweicloud.com/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso",
            ],
        )
        self.assertEqual(len(sources), len(set(sources)))
        self.assertFalse(any("jsinfo.net" in source for source in sources))

    def test_source_pool_deduplicates_urls_without_reordering(self):
        sources = validate_source_pool(
            [
                "https://one.test/file",
                "https://two.test/file",
                "https://one.test/file",
            ]
        )

        self.assertEqual(sources, ["https://one.test/file", "https://two.test/file"])

    def test_multi_ip_requires_two_to_ten_unique_ipv4_addresses(self):
        with self.assertRaisesRegex(ValueError, "between 2 and 10"):
            MultiIPConfig(line_count=1, lan_ips=["192.168.1.233"]).validate()
        with self.assertRaisesRegex(ValueError, "between 2 and 10"):
            MultiIPConfig(line_count=11, lan_ips=[f"192.168.1.{index}" for index in range(1, 12)]).validate()
        with self.assertRaisesRegex(ValueError, "exactly line_count"):
            MultiIPConfig(line_count=3, lan_ips=["192.168.1.233", "192.168.1.234"]).validate()
        with self.assertRaisesRegex(ValueError, "duplicates"):
            MultiIPConfig(line_count=2, lan_ips=["192.168.1.233", "192.168.1.233"]).validate()
        with self.assertRaisesRegex(ValueError, "IPv4"):
            MultiIPConfig(line_count=2, lan_ips=["192.168.1.233", "2001:db8::1"]).validate()

    def test_removed_and_topology_specific_environment_is_rejected(self):
        for topology in ("ikuai_line", "multi_ip"):
            for env_name, value in (("EGRESS_MODE", "single_ip"), ("LAN_IP", "192.168.1.233")):
                with self.subTest(topology=topology, env_name=env_name):
                    with self.assertRaisesRegex(ValueError, f"{env_name} is not supported"):
                        load_config(topology, "/missing.json", {env_name: value})

        for env_name, value in (("LINE_COUNT", "2"), ("LAN_IPS", "192.168.1.233,192.168.1.234")):
            with self.subTest(env_name=env_name):
                with self.assertRaisesRegex(ValueError, f"{env_name} is not supported"):
                    load_config("ikuai_line", "/missing.json", {env_name: value})

    def test_saved_config_wins_over_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "target_mbps": 901,
                        "line_count": 2,
                        "lan_ips": ["192.168.1.240", "192.168.1.241"],
                    }
                ),
                encoding="utf-8",
            )

            cfg = load_config(
                "multi_ip",
                path,
                {
                    "TARGET_MBPS": "700",
                    "LINE_COUNT": "3",
                    "LAN_IPS": "192.168.1.233,192.168.1.234,192.168.1.235",
                },
            )

        self.assertEqual(cfg.target_mbps, 901)
        self.assertEqual(cfg.line_count, 2)
        self.assertEqual(cfg.lan_ips, ["192.168.1.240", "192.168.1.241"])

    def test_unknown_persisted_keys_and_wrong_topology_fields_are_rejected(self):
        cases = (
            ("multi_ip", {"mystery": True}, "unknown persisted config key"),
            ("ikuai_line", {"line_count": 2}, "line_count is not supported"),
            ("ikuai_line", {"lan_ips": ["192.168.1.233"]}, "lan_ips is not supported"),
            ("multi_ip", {"topology": "ikuai_line"}, "does not match"),
        )
        for topology, saved, message in cases:
            with self.subTest(topology=topology, saved=saved):
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = Path(tmpdir) / "config.json"
                    path.write_text(json.dumps(saved), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_config(topology, path, {})

    def test_legacy_persisted_keys_are_rejected_even_with_new_keys(self):
        legacy_cases = (
            ("connections", 4, "connections_per_line", 8),
            ("max_connections", 11, "max_connections_per_line", 12),
            ("rate_limit_mbps", 500, "target_mbps", 800),
            ("download_urls", ["https://old.test/file"], "source_pool", ["https://new.test/file"]),
        )
        for old_key, old_value, new_key, new_value in legacy_cases:
            for saved in ({old_key: old_value}, {old_key: old_value, new_key: new_value}):
                with self.subTest(old_key=old_key, saved=saved):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        path = Path(tmpdir) / "config.json"
                        path.write_text(json.dumps(saved), encoding="utf-8")
                        with self.assertRaisesRegex(ValueError, f"unknown persisted config key: {old_key}"):
                            load_config("multi_ip", path, {})

    def test_unknown_topology_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported topology"):
            load_config("single_ip", "/missing.json", {})

    def test_to_dict_includes_the_explicit_topology(self):
        with self.assertRaises(TypeError):
            CommonConfig()
        self.assertEqual(IkuaiLineConfig().to_dict()["topology"], "ikuai_line")
        self.assertEqual(MultiIPConfig().to_dict()["topology"], "multi_ip")

    def test_persisted_scalar_types_are_rejected_before_use(self):
        invalid_values = (
            ("target_mbps", True, "target_mbps must be an integer"),
            ("connections_per_line", 8.0, "connections_per_line must be an integer"),
            ("rate_limit_enabled", "false", "rate_limit_enabled must be a boolean"),
            ("startup_stagger_seconds", math.nan, "startup_stagger_seconds must be a finite number"),
            ("log_level", 7, "log_level must be a string"),
            ("line_count", 2.0, "line_count must be an integer"),
        )
        for field_name, value, message in invalid_values:
            with self.subTest(field_name=field_name, value=value):
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = Path(tmpdir) / "config.json"
                    path.write_text(json.dumps({field_name: value}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_config("multi_ip", path, {})

    def test_config_rejects_non_string_source_and_ip_items(self):
        with self.assertRaisesRegex(ValueError, "source_pool items must be strings"):
            IkuaiLineConfig(source_pool=[123]).validate()
        with self.assertRaisesRegex(ValueError, "lan_ips items must be strings"):
            MultiIPConfig(lan_ips=["192.168.1.233", 234]).validate()

    def test_save_config_uses_same_directory_temporary_file_and_os_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text("not json", encoding="utf-8")

            with (
                patch("steam_pumper.config.os.replace", wraps=os.replace) as replace,
                patch("steam_pumper.config._fsync_directory") as fsync_directory,
            ):
                save_config(path, MultiIPConfig(target_mbps=801))

            saved = json.loads(path.read_text(encoding="utf-8"))
            leftovers = [item for item in path.parent.iterdir() if item != path]
            replace.assert_called_once()
            temporary_path = Path(replace.call_args.args[0])
            destination_path = Path(replace.call_args.args[1])
            self.assertEqual(saved["target_mbps"], 801)
            self.assertEqual(saved["topology"], "multi_ip")
            self.assertEqual(temporary_path.parent, path.parent)
            self.assertNotEqual(temporary_path, path)
            self.assertEqual(destination_path, path)
            self.assertFalse(temporary_path.exists())
            self.assertEqual(leftovers, [])
            fsync_directory.assert_called_once_with(path.parent)

    def test_save_config_replace_failure_preserves_prior_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            original = MultiIPConfig(target_mbps=700).validate().to_dict()
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch("steam_pumper.config.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    save_config(path, MultiIPConfig(target_mbps=801))

            persisted = json.loads(path.read_text(encoding="utf-8"))
            leftovers = [item for item in path.parent.iterdir() if item != path]

        self.assertEqual(persisted, original)
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
