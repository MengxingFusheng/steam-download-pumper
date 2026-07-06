import unittest

from steam_pumper.ikuai import parse_interfaces_status


class IkuaiTests(unittest.TestCase):
    def test_parse_interfaces_status_keeps_wan_download_and_connection_count(self):
        payload = {
            "iface_stream": [
                {"interface": "lan1", "download": 100, "connect_num": "--"},
                {"interface": "wan1", "download": 77_257_769, "connect_num": "123"},
                {"interface": "wan2", "download": 18_265_405, "connect_num": "99"},
            ]
        }

        rows = parse_interfaces_status(payload)

        self.assertEqual([row["interface"] for row in rows], ["wan1", "wan2"])
        self.assertEqual(round(rows[0]["download_mbps"]), 618)
        self.assertEqual(rows[0]["connect_num"], 123)
        self.assertEqual(round(rows[0]["share_percent"]), 81)


if __name__ == "__main__":
    unittest.main()
