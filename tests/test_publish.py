import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublishTests(unittest.TestCase):
    def test_publish_script_contains_both_registry_names_and_dockerfiles(self):
        script = (ROOT / "publish-images.sh").read_text(encoding="utf-8")

        for value in (
            "traveler1314/ikuai-line-pumper",
            "traveler1314/multi-ip-pumper",
            "ghcr.io/mengxingfusheng/ikuai-line-pumper",
            "ghcr.io/mengxingfusheng/multi-ip-pumper",
            "Dockerfile.ikuai-line",
            "Dockerfile.multi-ip",
        ):
            with self.subTest(value=value):
                self.assertIn(value, script)

    def test_publish_script_tests_and_builds_both_before_first_push(self):
        script = (ROOT / "publish-images.sh").read_text(encoding="utf-8")
        gate_call = script.index("\nrun_all_gates\n")
        publish_call = script.index("\npublish_all\n")

        self.assertIn("python3 -m unittest discover", script)
        self.assertIn("go test -race", script)
        self.assertIn('build_image "ikuai-line"', script)
        self.assertIn('build_image "multi-ip"', script)
        self.assertIn('smoke_image "ikuai-line"', script)
        self.assertIn('smoke_image "multi-ip"', script)
        self.assertLess(gate_call, publish_call)

    def test_publish_script_supports_dry_run_and_matching_tags(self):
        script = (ROOT / "publish-images.sh").read_text(encoding="utf-8")

        self.assertIn('DRY_RUN="${DRY_RUN:-0}"', script)
        self.assertIn("latest ikuai3 \"$SHORT_SHA\"", script)
        self.assertIn("pumper-${SHORT_SHA}", script)
        self.assertIn("image-digests-${SHORT_SHA}.txt", script)

    def test_publish_script_has_valid_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "publish-images.sh")],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_v2_release_scripts_have_valid_syntax(self):
        for name in ("publish-multi-ip-v2.sh", "publish-publisher-image.sh"):
            with self.subTest(name=name):
                result = subprocess.run(
                    ["bash", "-n", str(ROOT / name)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_multi_ip_v2_release_is_independent(self):
        script = (ROOT / "publish-multi-ip-v2.sh").read_text(encoding="utf-8")

        self.assertIn("Dockerfile.multi-ip", script)
        self.assertIn("traveler1314/multi-ip-pumper", script)
        self.assertIn("ghcr.io/mengxingfusheng/multi-ip-pumper", script)
        self.assertIn("oss-v2", script)
        self.assertIn("DRY_RUN", script)
        self.assertNotIn("Dockerfile.ikuai-line", script)
        self.assertNotIn("ikuai-line-pumper", script)
        self.assertNotIn("pumper-source-publisher", script)

    def test_publisher_release_is_independent(self):
        script = (ROOT / "publish-publisher-image.sh").read_text(encoding="utf-8")

        self.assertIn("Dockerfile.publisher", script)
        self.assertIn("traveler1314/pumper-source-publisher", script)
        self.assertIn("ghcr.io/mengxingfusheng/pumper-source-publisher", script)
        self.assertIn("publisher-v1", script)
        self.assertIn("DRY_RUN", script)
        self.assertNotIn("Dockerfile.multi-ip", script)
        self.assertNotIn("Dockerfile.ikuai-line", script)


if __name__ == "__main__":
    unittest.main()
