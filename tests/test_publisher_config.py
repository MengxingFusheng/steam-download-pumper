import os
import stat
import tempfile
import unittest
from pathlib import Path


BASE_ENV = {
    "OSS_BUCKET": "pumper-source-list-example",
    "OSS_REGION": "cn-beijing",
    "OSS_ENDPOINT": "https://oss-cn-beijing.aliyuncs.com",
    "OSS_PUBLIC_BASE_URL": "https://pumper-source-list-example.oss-cn-beijing.aliyuncs.com/pumper/v1",
    "SOURCE_LIST_KEY_ID": "pumper-source-2026-01",
}


class PublisherConfigTests(unittest.TestCase):
    def test_defaults_match_runtime_contract(self):
        from source_publisher.config import PublisherConfig

        cfg = PublisherConfig.from_env(BASE_ENV)
        self.assertEqual(cfg.publish_time.isoformat(timespec="minutes"), "03:17")
        self.assertEqual(cfg.timezone.key, "Asia/Shanghai")
        self.assertEqual(cfg.retry_seconds, (900, 3600, 21600))
        self.assertEqual(cfg.min_healthy_sources, 3)
        self.assertEqual(cfg.max_healthy_sources, 100)
        self.assertEqual(cfg.probe_concurrency, 4)
        self.assertEqual(cfg.probe_bytes, 8 * 1024 * 1024)
        self.assertEqual(cfg.probe_timeout_seconds, 20)

    def test_required_public_configuration_is_validated(self):
        from source_publisher.config import PublisherConfig

        invalid = {
            "OSS_BUCKET": "Bad_Bucket",
            "OSS_REGION": "cn-shanghai",
            "OSS_ENDPOINT": "http://oss-cn-beijing.aliyuncs.com",
            "OSS_PUBLIC_BASE_URL": "http://example.test/pumper/v1",
            "SOURCE_LIST_KEY_ID": "contains spaces",
            "PUBLISH_TIME": "24:00",
            "PUBLISH_TIMEZONE": "Not/AZone",
            "PUBLISH_RETRY_SECONDS": "900,0,21600",
            "MIN_HEALTHY_SOURCES": "2",
            "MAX_HEALTHY_SOURCES": "101",
            "PROBE_CONCURRENCY": "9",
            "PROBE_BYTES": str(1024),
            "PROBE_TIMEOUT_SECONDS": "26",
        }
        for name, value in invalid.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                PublisherConfig.from_env({**BASE_ENV, name: value})

    def test_validate_only_does_not_read_secrets(self):
        from source_publisher.config import PublisherConfig

        cfg = PublisherConfig.from_env(BASE_ENV)
        self.assertEqual(cfg.bucket, BASE_ENV["OSS_BUCKET"])

    def test_probe_size_is_fixed_to_eight_mib(self):
        from source_publisher.config import PublisherConfig

        self.assertEqual(
            PublisherConfig.from_env({**BASE_ENV, "PROBE_BYTES": "8388608"}).probe_bytes,
            8 * 1024 * 1024,
        )
        for value in ("2097152", "4194304", "16777216"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PublisherConfig.from_env({**BASE_ENV, "PROBE_BYTES": value})

    def test_secret_directory_cannot_be_overridden_by_environment(self):
        from source_publisher.config import PublisherConfig

        cfg = PublisherConfig.from_env({**BASE_ENV, "SECRETS_DIR": "/tmp/attacker"})
        self.assertEqual(cfg.secret_dir, Path("/run/secrets"))

    def test_public_base_url_must_be_exact_pumper_v1_prefix(self):
        from source_publisher.config import PublisherConfig

        with self.assertRaises(ValueError):
            PublisherConfig.from_env({
                **BASE_ENV,
                "OSS_PUBLIC_BASE_URL": "https://bucket.example/other/v1",
            })


class PublisherSecretsTests(unittest.TestCase):
    def test_reads_regular_nonempty_secret_files(self):
        from source_publisher.config import PublisherSecrets

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "source_signing_private_key": "private-material",
                "oss_access_key_id": "access-id",
                "oss_access_key_secret": "access-secret",
            }
            for name, value in values.items():
                path = root / name
                path.write_text(value + "\n", encoding="utf-8")
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            secrets = PublisherSecrets.from_directory(root)
            self.assertEqual(secrets.signing_private_key, "private-material")
            self.assertEqual(secrets.oss_access_key_id, "access-id")
            self.assertEqual(secrets.oss_access_key_secret, "access-secret")
            self.assertNotIn("private-material", repr(secrets))
            self.assertNotIn("access-secret", repr(secrets))

    def test_rejects_empty_missing_and_symlink_secrets_without_leaking_values(self):
        from source_publisher.config import PublisherSecrets

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source_signing_private_key").write_text("top-secret-value", encoding="utf-8")
            (root / "oss_access_key_id").write_text("", encoding="utf-8")
            os.symlink(root / "source_signing_private_key", root / "oss_access_key_secret")
            with self.assertRaises(ValueError) as caught:
                PublisherSecrets.from_directory(root)
            self.assertNotIn("top-secret-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
