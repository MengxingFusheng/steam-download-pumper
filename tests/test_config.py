import json
import tempfile
import unittest
from datetime import time
from pathlib import Path

from steam_pumper.config import PumperConfig, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_match_requested_lan_and_schedule_controls(self):
        cfg = PumperConfig()

        self.assertEqual(cfg.lan_ip, "192.168.1.233")
        self.assertEqual(cfg.gateway, "192.168.1.1")
        self.assertEqual(cfg.line_count, 2)
        self.assertEqual(cfg.target_mbps, 900)
        self.assertEqual(cfg.download_mode, "public_http")
        self.assertGreaterEqual(cfg.max_connections_per_line, cfg.connections_per_line)
        self.assertEqual(cfg.max_connections_per_line, 12)
        self.assertTrue(cfg.rate_limit_enabled)
        self.assertEqual(cfg.start_time, "00:00")
        self.assertEqual(cfg.end_time, "18:00")
        self.assertEqual(cfg.source_pool, cfg.download_urls)

    def test_time_window_supports_same_day_and_cross_midnight(self):
        day = PumperConfig(start_time="08:00", end_time="18:00")
        night = PumperConfig(start_time="22:00", end_time="06:00")

        self.assertTrue(day.is_within_window(time(12, 0)))
        self.assertFalse(day.is_within_window(time(20, 0)))
        self.assertTrue(night.is_within_window(time(23, 0)))
        self.assertTrue(night.is_within_window(time(3, 0)))
        self.assertFalse(night.is_within_window(time(12, 0)))

    def test_load_config_merges_file_and_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps({"line_count": 4, "app_ids": ["740"]}), encoding="utf-8")

            cfg = load_config(
                path,
                {
                    "TARGET_MBPS": "1200",
                    "APP_IDS": "740,90",
                    "SOURCE_POOL": "https://a.test/file,https://b.test/file",
                    "MAX_CONNECTIONS_PER_LINE": "99",
                },
            )

        self.assertEqual(cfg.line_count, 4)
        self.assertEqual(cfg.target_mbps, 1200)
        self.assertEqual(cfg.rate_limit_mbps, 1200)
        self.assertEqual(cfg.app_ids, ["740", "90"])
        self.assertEqual(cfg.source_pool, ["https://a.test/file", "https://b.test/file"])
        self.assertEqual(cfg.max_connections_per_line, 12)

    def test_rejects_invalid_time_and_counts(self):
        with self.assertRaises(ValueError):
            PumperConfig(start_time="25:00").validate()
        with self.assertRaises(ValueError):
            PumperConfig(line_count=1).validate()
        with self.assertRaises(ValueError):
            PumperConfig(line_count=11).validate()
        with self.assertRaises(ValueError):
            PumperConfig(connections_per_line=0).validate()
        with self.assertRaises(ValueError):
            PumperConfig(max_connections_per_line=0).validate()
        with self.assertRaises(ValueError):
            PumperConfig(connections_per_line=4, max_connections_per_line=3).validate()
        with self.assertRaises(ValueError):
            PumperConfig(connections_per_line=13).validate()
        with self.assertRaises(ValueError):
            PumperConfig(target_mbps=0).validate()

    def test_download_mode_aliases_keep_existing_configs_working(self):
        self.assertEqual(PumperConfig(download_mode="null").validate().download_mode, "public_http")
        self.assertEqual(PumperConfig(download_mode="steam").validate().download_mode, "steam_tmpfs")


if __name__ == "__main__":
    unittest.main()
