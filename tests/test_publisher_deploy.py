import os
import re
import subprocess
import tempfile
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
            "PROBE_CONCURRENCY", "PROBE_TIMEOUT_SECONDS",
        ):
            self.assertRegex(env, rf"(?m)^{name}=")
        self.assertNotRegex(env, r"(?m)^PROBE_BYTES=")
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

    def test_installer_never_executes_malicious_dotenv_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "publisher-config").mkdir()
            secrets = root / "publisher-secrets"
            secrets.mkdir()
            (root / "docker-compose.publisher.yml").write_text("services: {}\n", encoding="utf-8")
            (root / "publisher-config" / "candidates.json").write_text("{}\n", encoding="utf-8")
            for name in (
                "source_signing_private_key", "oss_access_key_id", "oss_access_key_secret"
            ):
                path = secrets / name
                path.write_text("fixture\n", encoding="utf-8")
                path.chmod(0o600)
            marker = root / "dotenv-executed"
            (root / ".env.publisher").write_text(
                f"OSS_BUCKET=$(touch {marker}; printf pumper-source-list-example)\n"
                "OSS_REGION=cn-beijing\n"
                "OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com\n"
                "OSS_PUBLIC_BASE_URL=https://pumper-source-list-example.oss-cn-beijing.aliyuncs.com/pumper/v1\n"
                "SOURCE_LIST_KEY_ID=pumper-source-2026-01\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            docker.chmod(0o755)
            completed = subprocess.run(
                ["bash", str(ROOT / "install-publisher.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "INSTALL_DIR": str(root),
                },
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists(), completed.stderr)


if __name__ == "__main__":
    unittest.main()
