import base64
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.test_publisher_config import BASE_ENV


class _FakeOSS:
    def __init__(self, fail_read=None):
        self.events = []
        self.objects = {}
        self.fail_read = fail_read

    def upload(self, path, object_key):
        self.events.append(("upload", object_key))
        self.objects[object_key.removeprefix("pumper/v1/")] = Path(path).read_bytes()

    def read_public(self, relative_key):
        self.events.append(("read", relative_key))
        if relative_key == self.fail_read:
            raise RuntimeError("public read failed")
        return self.objects[relative_key]


def _envelope(payload, _path, key_id, _tool):
    return json.dumps({
        "key_id": key_id,
        "algorithm": "Ed25519",
        "payload": base64.b64encode(payload).decode(),
        "signature": base64.b64encode(bytes(64)).decode(),
    }, separators=(",", ":")).encode()


def _verify(envelope, _path, _key_id, _tool):
    return base64.b64decode(json.loads(envelope)["payload"])


class PublisherServiceTests(unittest.TestCase):
    def _config(self, root):
        candidates = root / "candidates.json"
        candidates.write_text(json.dumps({"schema": 1, "sources": [
            {"url": f"https://mirror{i}.example/file", "enabled": True} for i in range(3)
        ]}), encoding="utf-8")
        secret_dir = root / "secrets"
        secret_dir.mkdir()
        (secret_dir / "source_signing_private_key").write_text("private", encoding="utf-8")
        from source_publisher.config import PublisherConfig
        config = PublisherConfig.from_env({
            **BASE_ENV,
            "CANDIDATES_PATH": str(candidates),
            "STATE_DIR": str(root / "state"),
        })
        return replace(config, secret_dir=secret_dir)

    def test_transaction_updates_state_only_after_both_public_verifications(self):
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            now = datetime(2026, 7, 20, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))

            def probes(urls, **_kwargs):
                return [ProbeResult(url, now, 100.0, 4_194_304, True, "") for url in urls]

            oss = _FakeOSS()
            service = PublicationService(
                config,
                PublisherSecrets("private", "id", "secret"),
                oss_client=oss,
                probe_fn=probes,
                sign_fn=_envelope,
                verify_fn=_verify,
            )
            result = service.run(now)
            release = f"releases/{result.revision}.json"
            self.assertEqual(oss.events, [
                ("upload", f"pumper/v1/{release}"),
                ("read", release),
                ("upload", "pumper/v1/latest.json"),
                ("read", "latest.json"),
            ])
            state = json.loads((config.state_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_revision"], result.revision)
            self.assertNotIn("secret", json.dumps(state).lower())

    def test_release_failure_never_uploads_latest_or_replaces_prior_state(self):
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            config.state_dir.mkdir()
            state_path = config.state_dir / "state.json"
            state_path.write_text('{"last_revision":20260719031700}', encoding="utf-8")
            prior = state_path.read_bytes()
            now = datetime(2026, 7, 20, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
            probes = lambda urls, **kwargs: [
                ProbeResult(url, now, 10.0, 4_194_304, True, "") for url in urls
            ]
            oss = _FakeOSS(fail_read="releases/20260720031700.json")
            service = PublicationService(config, PublisherSecrets("private", "id", "secret"),
                oss_client=oss, probe_fn=probes, sign_fn=_envelope, verify_fn=_verify)
            with self.assertRaises(Exception):
                service.run(now)
            self.assertNotIn(("upload", "pumper/v1/latest.json"), oss.events)
            self.assertEqual(state_path.read_bytes(), prior)

    def test_insufficient_sources_does_not_sign_or_upload(self):
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import InsufficientSources, PublicationService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            probes = lambda urls, **kwargs: [
                ProbeResult(url, now, 1.0, 0, index < 2, "probe failed")
                for index, url in enumerate(urls)
            ]
            service = PublicationService(config, PublisherSecrets("private", "id", "secret"),
                oss_client=_FakeOSS(), probe_fn=probes, sign_fn=_envelope, verify_fn=_verify)
            with self.assertRaises(InsufficientSources):
                service.run(now)


if __name__ == "__main__":
    unittest.main()
