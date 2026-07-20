import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublisherDeployTests(unittest.TestCase):
    def test_compose_has_required_isolation_and_no_inbound_access(self):
        compose = (ROOT / "docker-compose.publisher.yml").read_text(encoding="utf-8")
        required = [
            "read_only: true", "cap_drop:", "- ALL", "no-new-privileges:true",
            "pids_limit: 64", "mem_limit: 192m", "cpus: 0.50",
            "/tmp:size=64m,noexec,nosuid,nodev", "source_publisher_state:/state",
            "./publisher-config/candidates.json:/config/candidates.json:ro",
        ]
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, compose)
        for forbidden in ("ports:", "privileged:", "network_mode:", "/var/run/docker.sock", "cap_add:"):
            self.assertNotIn(forbidden, compose)
        self.assertEqual(compose.count("/run/secrets/"), 3)

    def test_environment_example_contains_only_public_configuration(self):
        env = (ROOT / ".env.publisher.example").read_text(encoding="utf-8")
        for name in (
            "OSS_BUCKET", "OSS_REGION", "OSS_ENDPOINT", "OSS_PUBLIC_BASE_URL",
            "SOURCE_LIST_KEY_ID", "PUBLISH_TIME", "PUBLISH_TIMEZONE",
            "PUBLISH_RETRY_SECONDS", "MIN_HEALTHY_SOURCES", "MAX_HEALTHY_SOURCES",
            "PROBE_CONCURRENCY", "PROBE_BYTES", "PROBE_TIMEOUT_SECONDS",
        ):
            self.assertRegex(env, rf"(?m)^{name}=")
        self.assertNotRegex(env, r"(?i)(ACCESS_KEY|PRIVATE_KEY)=")

    def test_installer_validates_secret_permissions_and_compose_before_start(self):
        installer = (ROOT / "install-publisher.sh").read_text(encoding="utf-8")
        self.assertIn("docker compose version", installer)
        self.assertIn("docker compose", installer)
        self.assertIn("config", installer)
        self.assertIn("validate-only", installer)
        self.assertIn("up -d", installer)
        self.assertRegex(installer, r"(?:stat|find).*(?:600|%a)")
        self.assertNotRegex(installer, r"(?i)(cat|printf|echo).*ACCESS_KEY_SECRET")


if __name__ == "__main__":
    unittest.main()
