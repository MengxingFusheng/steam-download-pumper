import base64
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tests.test_publisher_config import BASE_ENV


class _FakeOSS:
    def __init__(self, fail_read=None):
        self.events = []
        self.objects = {}
        self.fail_read = fail_read
        self.io_options = []

    def upload(self, path, object_key, *, overwrite=True, **kwargs):
        self.io_options.append(kwargs)
        relative = object_key.removeprefix("pumper/v1/")
        self.events.append(("upload", object_key, overwrite))
        if not overwrite and relative in self.objects:
            raise RuntimeError("immutable object already exists")
        self.objects[relative] = Path(path).read_bytes()

    def read_public(self, relative_key, **kwargs):
        self.io_options.append(kwargs)
        self.events.append(("read", relative_key))
        if relative_key == self.fail_read:
            raise RuntimeError("public read failed")
        return self.objects[relative_key]


def _envelope(payload, _path, key_id, _tool, **_kwargs):
    return json.dumps({
        "key_id": key_id,
        "algorithm": "Ed25519",
        "payload": base64.b64encode(payload).decode(),
        "signature": base64.b64encode(bytes(64)).decode(),
    }, separators=(",", ":")).encode()


def _verify(envelope, _path, _key_id, _tool, **_kwargs):
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
                ("read", "latest.json"),
                ("read", release),
                ("upload", f"pumper/v1/{release}", False),
                ("read", release),
                ("upload", "pumper/v1/latest.json", True),
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
            self.assertFalse(any(
                event[0] == "upload" and event[1] == "pumper/v1/latest.json"
                for event in oss.events
            ))
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

    def test_restart_recovers_remote_commit_without_republishing(self):
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            now = datetime(2026, 7, 20, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
            probe_calls = []

            def probes(urls, **_kwargs):
                probe_calls.append(True)
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
            from source_publisher import service as service_module

            real_atomic_write = service_module.atomic_write

            def fail_state_write(path, data, mode=0o600):
                if Path(path).name == "state.json":
                    raise OSError("simulated state disk failure")
                return real_atomic_write(path, data, mode)

            with patch("source_publisher.service.atomic_write", side_effect=fail_state_write):
                with self.assertRaises(OSError):
                    service.run(now)
            uploads_after_crash = [event for event in oss.events if event[0] == "upload"]
            self.assertEqual(len(uploads_after_crash), 2)
            self.assertFalse((config.state_dir / "state.json").exists())

            result = service.run(now)
            self.assertEqual(len(probe_calls), 1)
            self.assertEqual(
                [event for event in oss.events if event[0] == "upload"],
                uploads_after_crash,
            )
            state = json.loads((config.state_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_revision"], result.revision)
            self.assertEqual(state["last_source_count"], result.source_count)

    def test_atomic_write_fsyncs_file_and_parent_directory(self):
        from source_publisher.service import atomic_write

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            calls = []
            real_fsync = __import__("os").fsync

            def record(descriptor):
                calls.append(descriptor)
                return real_fsync(descriptor)

            with patch("source_publisher.service.os.fsync", side_effect=record):
                atomic_write(path, b'{}')
            self.assertEqual(len(calls), 2)

    def test_remote_recovery_never_rolls_back_local_revision(self):
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            now = datetime(2026, 7, 20, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
            probes = lambda urls, **_kwargs: [
                ProbeResult(url, now, 100.0, 4_194_304, True, "") for url in urls
            ]
            oss = _FakeOSS()
            service = PublicationService(
                config,
                PublisherSecrets("private", "id", "secret"),
                oss_client=oss,
                probe_fn=probes,
                sign_fn=_envelope,
                verify_fn=_verify,
            )
            remote = service.run(now)
            local_revision = remote.revision + 100
            state_path = config.state_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_revision"] = local_revision
            state_path.write_text(json.dumps(state), encoding="utf-8")

            result = service.run(now)
            self.assertGreater(result.revision, local_revision)

    def test_publication_propagates_one_deadline_and_cancel_token_to_all_io(self):
        import threading
        import time

        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            now = datetime(2026, 7, 20, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
            cancel = threading.Event()
            deadline = time.monotonic() + 10
            observed = []

            def probes(urls, **kwargs):
                observed.append(kwargs)
                return [ProbeResult(url, now, 100.0, 4_194_304, True, "") for url in urls]

            def sign(payload, path, key_id, tool, **kwargs):
                observed.append(kwargs)
                return _envelope(payload, path, key_id, tool)

            def verify(envelope, path, key_id, tool, **kwargs):
                observed.append(kwargs)
                return _verify(envelope, path, key_id, tool)

            oss = _FakeOSS()
            PublicationService(
                config,
                PublisherSecrets("private", "id", "secret"),
                oss_client=oss,
                probe_fn=probes,
                sign_fn=sign,
                verify_fn=verify,
            ).run(now, cancel_event=cancel, deadline=deadline)
            for options in observed + oss.io_options:
                self.assertIs(options["cancel_event"], cancel)
                self.assertLessEqual(options["deadline"], deadline)



if __name__ == "__main__":
    unittest.main()
