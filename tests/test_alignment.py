import tempfile
import unittest
from pathlib import Path

from steam_pumper.controller import PumperController


ROOT = Path(__file__).resolve().parents[1]


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

    def test_removed_runtime_modules_do_not_exist(self):
        removed = (
            "line_config.py",
            "line_controller.py",
            "line_main.py",
            "line_metrics.py",
            "line_web.py",
            "line_worker.py",
            "main.py",
            "networking.py",
            "null_download.py",
            "worker.py",
            "ikuai.py",
        )
        for name in removed:
            with self.subTest(name=name):
                self.assertFalse((ROOT / "steam_pumper" / name).exists())

    def test_supported_surface_has_no_removed_mode_or_image(self):
        paths = [
            *ROOT.glob("Dockerfile*"),
            *ROOT.glob("*.yml"),
            *ROOT.glob("*.sh"),
            ROOT / "README.md",
            ROOT / "docs/design.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in paths if path.exists())
        for forbidden in (
            "single_ip",
            "EGRESS_MODE",
            "one-to-one",
            "steam-download-pumper:one-to-one",
            "新建连接数分流",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)

    def test_ci_builds_both_images_from_one_revision(self):
        workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

        self.assertIn("Dockerfile.ikuai-line", workflow)
        self.assertIn("Dockerfile.multi-ip", workflow)
        self.assertIn("python3 -m unittest discover", workflow)
        self.assertIn("go test -race ./...", workflow)


if __name__ == "__main__":
    unittest.main()
