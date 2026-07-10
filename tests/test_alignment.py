import tempfile
import unittest
from pathlib import Path

from steam_pumper.controller import PumperController


class AlignmentTests(unittest.TestCase):
    def test_metrics_schema_is_identical_for_both_topologies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ikuai = PumperController("ikuai_line", root / "ikuai.json", env={}).metrics()
            multi = PumperController("multi_ip", root / "multi.json", env={}).metrics()

        self.assertEqual(set(ikuai), set(multi))
        self.assertEqual(set(ikuai["lines"][0]), set(multi["lines"][0]))

    def test_both_topologies_use_the_same_controller_class(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ikuai = PumperController("ikuai_line", root / "ikuai.json", env={})
            multi = PumperController("multi_ip", root / "multi.json", env={})

        self.assertIs(type(ikuai), PumperController)
        self.assertIs(type(multi), PumperController)


if __name__ == "__main__":
    unittest.main()
