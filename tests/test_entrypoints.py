import base64
import tempfile
import unittest
from pathlib import Path


class EntrypointTests(unittest.TestCase):
    def test_multi_ip_deployment_passes_only_canonical_remote_source_environment(self):
        root = Path(__file__).parents[1]
        compose = (root / "docker-compose.multi-ip.yml").read_text(encoding="utf-8")
        installer = (root / "install-multi-ip.sh").read_text(encoding="utf-8")
        variables = (
            "REMOTE_SOURCE_LIST_ENABLED",
            "SOURCE_LIST_URL",
            "SOURCE_LIST_PUBLIC_KEY",
            "SOURCE_LIST_KEY_ID",
            "SOURCE_LIST_REFRESH_TIME",
            "SOURCE_LIST_REFRESH_JITTER_SECONDS",
            "SOURCE_LIST_FETCH_TIMEOUT_SECONDS",
            "SOURCE_LIST_MAX_BYTES",
            "SOURCE_LIST_MIN_SOURCES",
        )

        for variable in variables:
            with self.subTest(variable=variable):
                self.assertIn(f"{variable}: ${{{variable}", compose)
                self.assertIn(f'{variable}="${{{variable}:-', installer)
                self.assertIn(f"{variable}=${{{variable}}}", installer)
        for alias in ("JITTER", "TIMEOUT", "MAX_BYTES", "MIN_SOURCES"):
            self.assertNotIn(f"\n{alias}=", installer)
            self.assertNotIn(f"\n      {alias}:", compose)

    def test_entrypoints_select_explicit_topologies(self):
        from steam_pumper.ikuai_main import TOPOLOGY as ikuai_topology
        from steam_pumper.multi_ip_main import TOPOLOGY as multi_ip_topology

        self.assertEqual(ikuai_topology, "ikuai_line")
        self.assertEqual(multi_ip_topology, "multi_ip")

    def test_application_builds_remote_manager_only_for_multi_ip(self):
        from steam_pumper.application import build_remote_source_manager

        env = {
            "REMOTE_SOURCE_LIST_ENABLED": "true",
            "SOURCE_LIST_URL": "https://bucket.example.test/latest.json",
            "SOURCE_LIST_PUBLIC_KEY": base64.b64encode(bytes(range(32))).decode("ascii"),
            "SOURCE_LIST_KEY_ID": "test-key",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            multi_manager, multi_error = build_remote_source_manager("multi_ip", config_path, env)
            ikuai_manager, ikuai_error = build_remote_source_manager("ikuai_line", config_path, env)

        self.assertIsNotNone(multi_manager)
        self.assertEqual(multi_error, "")
        self.assertIsNone(ikuai_manager)
        self.assertEqual(ikuai_error, "")

    def test_invalid_remote_environment_fails_closed_to_local_sources(self):
        from steam_pumper.application import build_remote_source_manager

        manager, error = build_remote_source_manager(
            "multi_ip",
            "/data/config.json",
            {"REMOTE_SOURCE_LIST_ENABLED": "true", "SOURCE_LIST_URL": "http://not-secure.test/list"},
        )

        self.assertIsNone(manager)
        self.assertIn("HTTPS", error)

    def test_multi_ip_image_packages_verifier_and_remote_defaults(self):
        dockerfile = (Path(__file__).parents[1] / "Dockerfile.multi-ip").read_text(encoding="utf-8")

        self.assertIn("COPY cmd/manifestctl ./cmd/manifestctl", dockerfile)
        self.assertIn("go build -trimpath -ldflags=\"-s -w\" -o /out/manifestctl ./cmd/manifestctl", dockerfile)
        self.assertIn("COPY --from=discarder-builder /out/manifestctl /usr/local/bin/manifestctl", dockerfile)
        for expected in (
            "REMOTE_SOURCE_LIST_ENABLED=false",
            "SOURCE_LIST_URL=",
            "SOURCE_LIST_PUBLIC_KEY=",
            "SOURCE_LIST_KEY_ID=",
            "SOURCE_LIST_REFRESH_TIME=04:00",
            "SOURCE_LIST_REFRESH_JITTER_SECONDS=1800",
            "SOURCE_LIST_FETCH_TIMEOUT_SECONDS=15",
            "SOURCE_LIST_MAX_BYTES=524288",
            "SOURCE_LIST_MIN_SOURCES=3",
            "MAX_CONNECTIONS_PER_LINE=12",
        ):
            self.assertIn(expected, dockerfile)
        self.assertNotIn("oss-cn-", dockerfile)


if __name__ == "__main__":
    unittest.main()
