import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class ImageTests(unittest.TestCase):
    def test_repository_has_the_supported_dockerfiles(self):
        self.assertEqual(
            sorted(path.name for path in ROOT.glob("Dockerfile*")),
            ["Dockerfile.dashboard", "Dockerfile.fleet", "Dockerfile.ikuai-line", "Dockerfile.multi-ip", "Dockerfile.publisher"],
        )

    def test_supported_images_have_explicit_entrypoints_and_dependencies(self):
        ikuai = read("Dockerfile.ikuai-line")
        multi = read("Dockerfile.multi-ip")
        fleet = read("Dockerfile.fleet")

        self.assertIn('CMD ["python3", "-m", "steam_pumper.ikuai_main"]', ikuai)
        self.assertIn('CMD ["python3", "-m", "steam_pumper.multi_ip_main"]', multi)
        self.assertIn('CMD ["python3", "-m", "steam_pumper.fleet_main"]', fleet)
        self.assertNotIn("iproute2", ikuai)
        self.assertIn("iproute2", multi)
        self.assertIn("snmp", fleet)
        for content in (ikuai, multi):
            self.assertIn("MAX_CONNECTIONS_PER_LINE=12", content)
            self.assertNotIn("steamcmd", content.lower())

    def test_multi_ip_and_publisher_package_manifestctl(self):
        multi = read("Dockerfile.multi-ip")
        publisher = read("Dockerfile.publisher")

        self.assertIn("cmd/manifestctl", multi)
        self.assertIn("/usr/local/bin/manifestctl", multi)
        self.assertIn("cmd/manifestctl", publisher)
        self.assertIn("/usr/local/bin/manifestctl", publisher)
        self.assertIn("/usr/local/bin/ossutil", publisher)
        self.assertNotIn("EXPOSE", publisher)

    def test_multi_ip_compose_uses_only_new_image_and_fields(self):
        compose = read("docker-compose.multi-ip.yml")

        self.assertIn("traveler1314/multi-ip-pumper:latest", compose)
        self.assertIn("NET_ADMIN", compose)
        self.assertIn("LAN_IPS", compose)
        self.assertIn("CONTAINER_IP", compose)
        self.assertNotIn("EGRESS_MODE", compose)
        self.assertNotIn("steam-download-pumper", compose)

    def test_fleet_compose_is_resource_limited_and_safe_for_a_pcdn_host(self):
        compose = read("docker-compose.fleet.yml")

        for expected in (
            "init: true", "cpu_shares: 128", "cpus: ${FLEET_CPUS:-8.0}",
            "mem_limit: ${FLEET_MEMORY:-2g}", "pids_limit: ${FLEET_PIDS_LIMIT:-512}",
            "cap_drop:", "- ALL", "NET_ADMIN", "no-new-privileges:true",
            "ionice", '"-c3"', '"-n"', '"19"', "FLEET_AUTOSTART: ${FLEET_AUTOSTART:-false}",
            "FLEET_ACCOUNTS_CSV: ${FLEET_ACCOUNTS_CSV:-}", "${FLEET_DATA_DIR:-./fleet-data}:/data", "noexec,nosuid,nodev",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, compose)
        for forbidden in ("privileged:", "network_mode: host", "/var/run/docker.sock"):
            self.assertNotIn(forbidden, compose)
        fleet_service = compose.split("  fleet-pumper:", 1)[1].split("\n  fleet-web-proxy:", 1)[0]
        self.assertNotRegex(fleet_service, r"(?m)^    ports:")

    def test_fleet_environment_example_has_safe_pcdn_defaults(self):
        env = read(".env.fleet.example")

        for expected in ("FLEET_AUTOSTART=false", "FLEET_CPUS=8.0", "FLEET_MEMORY=2g", "FLEET_PIDS_LIMIT=512", "FLEET_DATA_DIR=./fleet-data"):
            with self.subTest(expected=expected):
                self.assertIn(expected, env)

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
        self.assertIn("publisher-secrets", dockerignore)
        self.assertIn("fleet-secrets", dockerignore)
        self.assertIn("fleet-config/accounts.csv", dockerignore)
        self.assertIn("source_signing_private_key", dockerignore)


if __name__ == "__main__":
    unittest.main()
