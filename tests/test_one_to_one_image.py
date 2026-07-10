import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OneToOneImageTests(unittest.TestCase):
    def test_one_to_one_compose_uses_prebuilt_ghcr_image(self):
        compose = (ROOT / "docker-compose.one-to-one.yml").read_text(encoding="utf-8")

        self.assertIn("ghcr.io/mengxingfusheng/steam-download-pumper:one-to-one", compose)
        self.assertIn('EGRESS_MODE: "${EGRESS_MODE:-multi_ip}"', compose)
        self.assertIn('LAN_IPS: "${LAN_IPS:-192.168.1.233,192.168.1.234}"', compose)
        self.assertIn("NET_ADMIN", compose)
        self.assertNotIn("build:", compose)

    def test_one_to_one_installer_pulls_image_without_building(self):
        script = (ROOT / "install-one-to-one.sh").read_text(encoding="utf-8")

        self.assertIn("COMPOSE_FILE_PATH", script)
        self.assertIn("docker-compose.one-to-one.yml", script)
        self.assertIn("COMPOSE_BUILD", script)
        self.assertIn("PULL_IMAGE", script)
        self.assertIn("EGRESS_MODE", script)
        self.assertNotIn("--build", script)

    def test_one_to_one_installer_has_valid_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(ROOT / "install-one-to-one.sh")], capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dockerfile_can_bake_one_to_one_defaults(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ARG DEFAULT_EGRESS_MODE=single_ip", dockerfile)
        self.assertIn("ARG DEFAULT_LINE_COUNT=2", dockerfile)
        self.assertIn("ARG DEFAULT_LAN_IPS=192.168.1.233", dockerfile)
        self.assertIn("EGRESS_MODE=${DEFAULT_EGRESS_MODE}", dockerfile)
        self.assertIn("LAN_IPS=${DEFAULT_LAN_IPS}", dockerfile)
        self.assertIn("FROM python:3.13-slim", dockerfile)
        self.assertNotIn("steamcmd", dockerfile.lower())

    def test_compose_files_have_no_legacy_download_volumes(self):
        for name in ("docker-compose.yml", "docker-compose.one-to-one.yml"):
            compose = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotIn("steamcmd", compose.lower())
                self.assertNotIn("/steam/", compose.lower())


if __name__ == "__main__":
    unittest.main()
