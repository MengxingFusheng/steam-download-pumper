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
            "DISCOVERY_ENABLED": "false",
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

    def test_low_speed_sources_are_not_published(self):
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import InsufficientSources, PublicationService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            probes = lambda urls, **kwargs: [
                ProbeResult(url, now, 99.9, 4_194_304, True, "") for url in urls
            ]
            service = PublicationService(
                config, PublisherSecrets("private", "id", "secret"),
                oss_client=_FakeOSS(), probe_fn=probes, sign_fn=_envelope, verify_fn=_verify,
            )
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

    def test_explicit_publication_reprobes_and_increments_same_day_revision(self):
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            now = datetime(2026, 10, 19, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
            calls = []
            def probes(urls, **kwargs):
                calls.append(list(urls))
                return [ProbeResult(url, now, 200, 4_194_304, True, "") for url in urls]
            service = PublicationService(config, PublisherSecrets("private", "id", "secret"),
                oss_client=_FakeOSS(), probe_fn=probes, sign_fn=_envelope, verify_fn=_verify)
            first = service.run(now)
            second = service.run(now, force_republish=True)
            self.assertEqual(len(calls), 2)
            self.assertGreater(second.revision, first.revision)
            self.assertEqual(second.source_count, len(json.loads(second.payload)["sources"]))

    def test_final_host_limit_quantity_diagnostic_and_manifest_count_match(self):
        import time
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            observed = []
            def probes(urls, **kwargs):
                observed.append(kwargs["deadline"])
                return [ProbeResult(f"https://host{i // 3}.example/file{i}", now,
                                    300 + i, 4_194_304, True, "") for i in range(60)]
            result = PublicationService(config, probe_fn=probes).run(now, validate_only=True)
            sources = json.loads(result.payload)["sources"]
            self.assertEqual(result.source_count, 30)
            self.assertEqual(len(sources), 30)
            from collections import Counter
            from urllib.parse import urlsplit
            self.assertLessEqual(max(Counter(urlsplit(s["url"]).hostname for s in sources).values()), 2)
            self.assertTrue(result.diagnostics["quantity_target_met"])
            self.assertGreater(observed[0] - time.monotonic(), 25)

    def test_duplicate_host_cannot_satisfy_minimum_source_requirement(self):
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService, InsufficientSources

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            def probes(urls, **kwargs):
                return [ProbeResult(f"https://one.example/{i}", now, 300, 4_194_304, True, "") for i in range(10)]
            with self.assertRaises(InsufficientSources):
                PublicationService(config, probe_fn=probes).run(now, validate_only=True)
            diagnostics = json.loads((config.state_dir / "last-probes.json").read_bytes())
            self.assertEqual(diagnostics["published_count"], 2)
            self.assertFalse(diagnostics["quantity_target_met"])

    def test_long_probe_uses_completion_timestamp_and_manifest_passes_node_parser(self):
        from datetime import timedelta
        from source_publisher.config import PublisherSecrets
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService
        from steam_pumper.remote_sources import parse_manifest_payload

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            now = datetime(2026, 10, 19, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
            clock = [100.0]
            def probes(urls, **kwargs):
                clock[0] += 16 * 60
                checked = now + timedelta(minutes=16)
                return [ProbeResult(url, checked, 200, 4_194_304, True, "") for url in urls]
            service = PublicationService(config, PublisherSecrets("private", "id", "secret"),
                oss_client=_FakeOSS(), probe_fn=probes, sign_fn=_envelope, verify_fn=_verify)
            with patch("source_publisher.service.time.monotonic", side_effect=lambda: clock[0]):
                result = service.run(now)
            completed = now + timedelta(minutes=16)
            parsed = parse_manifest_payload(result.payload, min_sources=3, now=completed, resolve_dns=False)
            self.assertEqual(parsed.generated_at, completed)
            self.assertTrue(all(entry.checked_at <= parsed.generated_at for entry in parsed.sources))
            state = json.loads((config.state_dir / "state.json").read_bytes())
            self.assertEqual(datetime.fromisoformat(state["last_success_at"]), completed)

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


class OoklaPublisherSelectionTests(unittest.TestCase):
    _config = PublisherServiceTests._config
    def test_validated_direct_sources_survive_low_publisher_throughput(self):
        from source_publisher.probe import ProbeResult
        from source_publisher.service import PublicationService
        from source_publisher.ookla import DOWNLOAD_QUERY
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            urls = [f'https://ookla{i}.example/download?{DOWNLOAD_QUERY}' for i in range(3)]
            config.candidates_path.write_text(json.dumps({'schema':1, 'sources':[
                {'url':url, 'enabled':True} for url in urls]}))
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            def probes(values, **kwargs):
                return [ProbeResult(url, now, 5, 4194304, True, '') for url in values]
            result = PublicationService(config, probe_fn=probes).run(now, validate_only=True)
            self.assertEqual(result.source_count, 3)
            self.assertEqual(result.diagnostics['ookla_published_count'], 3)
            self.assertEqual([x['url'] for x in json.loads(result.payload)['sources']], urls)
