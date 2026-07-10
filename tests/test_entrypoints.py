import unittest


class EntrypointTests(unittest.TestCase):
    def test_entrypoints_select_explicit_topologies(self):
        from steam_pumper.ikuai_main import TOPOLOGY as ikuai_topology
        from steam_pumper.multi_ip_main import TOPOLOGY as multi_ip_topology

        self.assertEqual(ikuai_topology, "ikuai_line")
        self.assertEqual(multi_ip_topology, "multi_ip")


if __name__ == "__main__":
    unittest.main()
