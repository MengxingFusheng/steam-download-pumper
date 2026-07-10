import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class ImageTests(unittest.TestCase):
    def test_repository_has_exactly_two_dockerfiles(self):
        self.assertEqual(
            sorted(path.name for path in ROOT.glob("Dockerfile*")),
            ["Dockerfile.ikuai-line", "Dockerfile.multi-ip"],
        )

    def test_supported_images_have_explicit_entrypoints_and_dependencies(self):
        ikuai = read("Dockerfile.ikuai-line")
        multi = read("Dockerfile.multi-ip")

        self.assertIn('CMD ["python3", "-m", "steam_pumper.ikuai_main"]', ikuai)
        self.assertIn('CMD ["python3", "-m", "steam_pumper.multi_ip_main"]', multi)
        self.assertNotIn("iproute2", ikuai)
        self.assertIn("iproute2", multi)
        for content in (ikuai, multi):
            self.assertIn("MAX_CONNECTIONS_PER_LINE=12", content)
            self.assertNotIn("steamcmd", content.lower())

    def test_multi_ip_compose_uses_only_new_image_and_fields(self):
        compose = read("docker-compose.multi-ip.yml")

        self.assertIn("traveler1314/multi-ip-pumper:latest", compose)
        self.assertIn("NET_ADMIN", compose)
        self.assertIn("LAN_IPS", compose)
        self.assertIn("CONTAINER_IP", compose)
        self.assertNotIn("EGRESS_MODE", compose)
        self.assertNotIn("steam-download-pumper", compose)

    def test_multi_ip_installer_has_valid_syntax_and_no_mode_prompt(self):
        script = read("install-multi-ip.sh")
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "install-multi-ip.sh")],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker-compose.multi-ip.yml", script)
        self.assertIn("CONTAINER_IP", script)
        self.assertNotIn("EGRESS_MODE", script)

    def test_docker_context_excludes_runtime_cache_and_release_output(self):
        dockerignore = read(".dockerignore")

        self.assertIn("__pycache__", dockerignore)
        self.assertIn("*.pyc", dockerignore)
        self.assertIn("dist", dockerignore)
        self.assertIn("data/*", dockerignore)


if __name__ == "__main__":
    unittest.main()
