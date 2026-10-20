import os
import unittest
from unittest.mock import patch

from steam_pumper.fleet import FleetAccount, FleetGroup
from steam_pumper.fleet_schedule import allocate_daily


class DownstreamProfileTests(unittest.TestCase):
    def test_default_profiles_stay_200_50_and_500_500(self):
        with patch.dict(os.environ, {}, clear=True):
            ordinary = FleetAccount("one", "192.168.1.237", "wan1", weight=1)
            symmetric = FleetAccount("two", "192.168.1.238", "wan2", weight=10)
            self.assertEqual((ordinary.download_mbps, ordinary.upload_mbps), (200, 50))
            self.assertEqual((symmetric.download_mbps, symmetric.upload_mbps), (500, 500))

    def test_500_downstream_keeps_50_uplink_and_requires_full_slot(self):
        with patch.dict(os.environ, {"FLEET_STANDARD_DOWNLOAD_MBPS": "500"}):
            account = FleetAccount("one", "192.168.1.237", "wan1", weight=1, group_id="standard")
            groups = [FleetGroup("standard", "普通", 500, 1)]
            forecast = {"one": {"remaining_download_bytes": 1_000_000_000}}
            self.assertEqual(account.upload_mbps, 50)
            self.assertEqual(allocate_daily([account], groups, forecast, 499, {}, {}), {})
            self.assertEqual(allocate_daily([account], groups, forecast, 500, {}, {}), {"one": 500})

    def test_invalid_downstream_profile_is_rejected(self):
        account = FleetAccount("one", "192.168.1.237", "wan1", weight=1)
        for value in ["0", "-1", "1001", "500.5", "invalid"]:
            with self.subTest(value=value), patch.dict(os.environ, {"FLEET_STANDARD_DOWNLOAD_MBPS": value}):
                with self.assertRaises(ValueError):
                    _ = account.download_mbps
