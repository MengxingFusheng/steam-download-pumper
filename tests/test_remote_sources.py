import base64
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, patch


PUBLIC_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
SIGNATURE = base64.b64encode(bytes(64)).decode("ascii")
NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone(timedelta(hours=8)))


def public_resolver(_host, _port, family=0, type=0):
    return [(family, type, 6, "", ("8.8.8.8", 0))]


def manifest_payload(revision=20260720031700, source_count=3, reference_now=NOW, **overrides):
    generated_at = reference_now - timedelta(minutes=43)
    payload = {
        "schema": 1,
        "revision": revision,
        "generated_at": generated_at.isoformat(),
        "expires_at": (generated_at + timedelta(hours=72)).isoformat(),
        "sources": [
            {
                "url": f"https://mirror{index}.example.test/file.iso",
                "checked_at": (generated_at - timedelta(minutes=5)).isoformat(),
                "probe_mbps": 100.0 + index,
            }
            for index in range(source_count)
        ],
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def signed_envelope(payload):
    return json.dumps(
        {
            "key_id": "test-key",
            "algorithm": "Ed25519",
            "payload": base64.b64encode(payload).decode("ascii"),
            "signature": SIGNATURE,
        },
        separators=(",", ":"),
    ).encode("utf-8")


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class RemoteSourceSettingsTests(unittest.TestCase):
    def test_settings_are_environment_only_and_support_contract_defaults(self):
        from steam_pumper.config import MultiIPConfig
        from steam_pumper.remote_sources import RemoteSourceSettings

        settings = RemoteSourceSettings.from_env(
            {
                "REMOTE_SOURCE_LIST_ENABLED": "true",
                "SOURCE_LIST_URL": "https://bucket.example.test/latest.json",
                "SOURCE_LIST_PUBLIC_KEY": PUBLIC_KEY,
                "SOURCE_LIST_KEY_ID": "test-key",
            }
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.refresh_time, "04:00")
        self.assertEqual(settings.refresh_jitter_seconds, 1800)
        self.assertEqual(settings.fetch_timeout_seconds, 15)
        self.assertEqual(settings.max_bytes, 524_288)
        self.assertEqual(settings.min_sources, 3)
        config = MultiIPConfig().to_dict()
        self.assertNotIn("source_list_url", config)
        self.assertNotIn("source_list_public_key", config)

    def test_settings_ignore_undeclared_generic_aliases(self):
        from steam_pumper.remote_sources import RemoteSourceSettings

        settings = RemoteSourceSettings.from_env(
            {
                "REMOTE_SOURCE_LIST_ENABLED": "true",
                "SOURCE_LIST_URL": "https://bucket.example.test/latest.json",
                "SOURCE_LIST_PUBLIC_KEY": PUBLIC_KEY,
                "SOURCE_LIST_KEY_ID": "test-key",
                "JITTER": "17",
                "TIMEOUT": "9",
                "MAX_BYTES": "4096",
                "MIN_SOURCES": "4",
            }
        )

        self.assertEqual(settings.refresh_jitter_seconds, 1800)
        self.assertEqual(settings.fetch_timeout_seconds, 15)
        self.assertEqual(settings.max_bytes, 524_288)
        self.assertEqual(settings.min_sources, 3)

    def test_enabled_settings_reject_non_https_and_invalid_public_keys(self):
        from steam_pumper.remote_sources import RemoteSourceSettings

        base = {
            "REMOTE_SOURCE_LIST_ENABLED": "true",
            "SOURCE_LIST_URL": "https://bucket.example.test/latest.json",
            "SOURCE_LIST_PUBLIC_KEY": PUBLIC_KEY,
            "SOURCE_LIST_KEY_ID": "test-key",
        }
        cases = (
            ({**base, "SOURCE_LIST_URL": "http://bucket.example.test/latest.json"}, "HTTPS"),
            ({**base, "SOURCE_LIST_PUBLIC_KEY": base64.b64encode(b"short").decode()}, "32 bytes"),
            ({**base, "SOURCE_LIST_KEY_ID": ""}, "KEY_ID"),
            ({**base, "SOURCE_LIST_REFRESH_JITTER_SECONDS": "-1"}, "SOURCE_LIST_REFRESH_JITTER_SECONDS"),
            ({**base, "SOURCE_LIST_MIN_SOURCES": "101"}, "SOURCE_LIST_MIN_SOURCES"),
        )
        for env, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    RemoteSourceSettings.from_env(env)


class ManifestValidationTests(unittest.TestCase):
    def test_structural_validation_without_dns_still_rejects_private_ip_literals(self):
        from steam_pumper.remote_sources import parse_manifest_payload

        document = json.loads(manifest_payload())
        document["sources"][0]["url"] = "https://169.254.169.254/latest/meta-data"

        with self.assertRaisesRegex(ValueError, "public IPv4"):
            parse_manifest_payload(
                json.dumps(document).encode(),
                min_sources=3,
                now=NOW,
                resolve_dns=False,
            )

    def test_valid_payload_returns_ordered_urls(self):
        from steam_pumper.remote_sources import parse_manifest_payload

        manifest = parse_manifest_payload(
            manifest_payload(),
            min_sources=3,
            now=NOW,
            resolver=public_resolver,
        )

        self.assertEqual(manifest.revision, 20260720031700)
        self.assertEqual(
            manifest.urls,
            tuple(f"https://mirror{index}.example.test/file.iso" for index in range(3)),
        )

    def test_payload_rejects_schema_time_count_and_url_violations(self):
        from steam_pumper.remote_sources import parse_manifest_payload

        generated = NOW - timedelta(minutes=43)
        valid_sources = json.loads(manifest_payload())["sources"]
        cases = (
            ({"schema": 2}, "schema"),
            ({"revision": 12}, "revision"),
            ({"revision": 20260230031700}, "revision"),
            ({"revision": 20261320031700}, "revision"),
            ({"revision": 20260720241700}, "revision"),
            ({"generated_at": (NOW + timedelta(minutes=11)).isoformat()}, "future"),
            ({"expires_at": (generated + timedelta(hours=71)).isoformat()}, "72 hours"),
            ({"sources": valid_sources[:2]}, "at least 3"),
            ({"sources": valid_sources + [valid_sources[0]]}, "duplicate"),
            ({"sources": [{**valid_sources[0], "url": "ftp://example.test/a"}, *valid_sources[1:]]}, "HTTP"),
            ({"sources": [{**valid_sources[0], "url": "https://u:p@example.test/a"}, *valid_sources[1:]]}, "credentials"),
            ({"sources": [{**valid_sources[0], "url": "https://example.test/a#x"}, *valid_sources[1:]]}, "fragment"),
            ({"sources": [{**valid_sources[0], "probe_mbps": -1}, *valid_sources[1:]]}, "probe_mbps"),
        )
        for overrides, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    parse_manifest_payload(
                        manifest_payload(**overrides),
                        min_sources=3,
                        now=NOW,
                        resolver=public_resolver,
                    )

    def test_payload_rejects_any_non_public_ipv4_resolution(self):
        from steam_pumper.remote_sources import parse_manifest_payload

        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "192.0.2.1"):
            def resolver(_host, _port, family=0, type=0, resolved=address):
                return [(family, type, 6, "", (resolved, 0))]

            with self.subTest(address=address):
                with self.assertRaisesRegex(ValueError, "public IPv4"):
                    parse_manifest_payload(
                        manifest_payload(),
                        min_sources=3,
                        now=NOW,
                        resolver=resolver,
                    )


class RemoteSourceManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = Path(self.tempdir.name)
        self.verifier = self.data_dir / "manifestctl"
        self.verifier_record = self.data_dir / "manifestctl-record.json"
        self.verifier.write_text(
            "#!/usr/bin/env python3\n"
            "import base64,json,sys\n"
            "raw=sys.stdin.buffer.read()\n"
            f"open({str(self.verifier_record)!r},'w').write(json.dumps({{'argv':sys.argv[1:],'stdin':base64.b64encode(raw).decode()}}))\n"
            "body=json.loads(raw)\n"
            "sys.stdout.buffer.write(base64.b64decode(body['payload']))\n",
            encoding="utf-8",
        )
        self.verifier.chmod(0o755)

    def settings(self):
        from steam_pumper.remote_sources import RemoteSourceSettings

        return RemoteSourceSettings.from_env(
            {
                "REMOTE_SOURCE_LIST_ENABLED": "true",
                "SOURCE_LIST_URL": "https://bucket.example.test/latest.json",
                "SOURCE_LIST_PUBLIC_KEY": PUBLIC_KEY,
                "SOURCE_LIST_KEY_ID": "test-key",
                "SOURCE_LIST_REFRESH_JITTER_SECONDS": "0",
            }
        )

    def manager(self, bodies):
        from steam_pumper.remote_sources import RemoteSourceManager

        queued = list(bodies)

        def urlopen(request, timeout):
            self.assertEqual(request.full_url, self.settings().url)
            self.assertEqual(timeout, 15)
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, FakeResponse):
                return item
            return FakeResponse(item, headers={"ETag": '"revision"'})

        return RemoteSourceManager(
            self.settings(),
            data_dir=self.data_dir,
            verifier_path=self.verifier,
            urlopen=urlopen,
            resolver=public_resolver,
            hostname="stable-host",
        )

    def test_refresh_verifies_protocol_and_commits_state_before_envelope(self):
        envelope = signed_envelope(manifest_payload())
        manager = self.manager([envelope])

        with patch.object(manager, "_atomic_write", wraps=manager._atomic_write) as atomic_write:
            changed, sources, snapshot = manager.refresh(NOW)

        self.assertTrue(changed)
        self.assertEqual(len(sources), 3)
        self.assertEqual(snapshot.revision, 20260720031700)
        self.assertEqual(snapshot.source_count, 3)
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.last_success_at, NOW.isoformat())
        self.assertEqual(snapshot.next_refresh_at, (NOW + timedelta(days=1)).isoformat())
        self.assertEqual(
            [call.args[0].name for call in atomic_write.call_args_list],
            ["source-list-state.json", "source-list-envelope.json"],
        )
        self.assertTrue((self.data_dir / "source-list-envelope.json").exists())
        self.assertTrue((self.data_dir / "source-list-state.json").exists())
        verifier_record = json.loads(self.verifier_record.read_text(encoding="utf-8"))
        self.assertEqual(
            verifier_record["argv"],
            [
                "verify",
                "--public-key-base64",
                PUBLIC_KEY,
                "--key-id",
                "test-key",
                "--max-bytes",
                "524288",
            ],
        )
        self.assertEqual(base64.b64decode(verifier_record["stdin"]), envelope)

    def test_manifest_verifier_has_explicit_fetch_timeout(self):
        envelope = signed_envelope(manifest_payload())
        manager = self.manager([envelope])

        with patch("steam_pumper.remote_sources.subprocess.run", wraps=__import__("subprocess").run) as run:
            manager.refresh(NOW)

        run.assert_called_once_with(
            ANY,
            input=envelope,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def test_atomic_write_fsyncs_parent_directory_after_replace(self):
        from steam_pumper.remote_sources import RemoteSourceManager

        target = self.data_dir / "nested" / "state.json"
        real_fsync = __import__("os").fsync
        fsynced = []

        def record_fsync(descriptor):
            fsynced.append(descriptor)
            return real_fsync(descriptor)

        with patch("steam_pumper.remote_sources.os.fsync", side_effect=record_fsync):
            RemoteSourceManager._atomic_write(target, b"{}")

        self.assertEqual(target.read_bytes(), b"{}")
        self.assertEqual(len(fsynced), 2)

    def test_atomic_write_fsyncs_directory_only_after_replace(self):
        from steam_pumper.remote_sources import RemoteSourceManager

        target = self.data_dir / "ordered" / "state.json"
        events = []
        real_replace = __import__("os").replace
        real_fsync = __import__("os").fsync

        def record_replace(source, destination):
            events.append("replace")
            return real_replace(source, destination)

        def record_fsync(descriptor):
            events.append("fsync")
            return real_fsync(descriptor)

        with (
            patch("steam_pumper.remote_sources.os.replace", side_effect=record_replace),
            patch("steam_pumper.remote_sources.os.fsync", side_effect=record_fsync),
        ):
            RemoteSourceManager._atomic_write(target, b"{}")

        self.assertEqual(events, ["fsync", "replace", "fsync"])

    def test_lkg_load_does_not_require_dns_but_refresh_data_plane_will_validate(self):
        envelope = signed_envelope(manifest_payload())
        manager = self.manager([envelope])
        manager.refresh(NOW)

        def offline_dns(*_args):
            raise OSError("temporary DNS outage")

        from steam_pumper.remote_sources import RemoteSourceManager

        reloaded = RemoteSourceManager(
            self.settings(),
            data_dir=self.data_dir,
            verifier_path=self.verifier,
            resolver=offline_dns,
            hostname="stable-host",
        )
        sources, snapshot = reloaded.load_last_known_good(NOW)

        self.assertEqual(len(sources), 3)
        self.assertEqual(snapshot.revision, 20260720031700)

    def test_next_daily_refresh_uses_today_when_scheduled_time_has_not_passed(self):
        manager = self.manager([])
        manager.stable_jitter_seconds = 600

        before = manager._next_daily_refresh(NOW.replace(hour=3, minute=0))
        after = manager._next_daily_refresh(NOW.replace(hour=5, minute=0))

        self.assertEqual(before, NOW.replace(hour=4, minute=10))
        self.assertEqual(after, (NOW + timedelta(days=1)).replace(hour=4, minute=10))

    def test_startup_loads_expired_lkg_and_marks_it_stale(self):
        old_now = NOW - timedelta(days=4)
        manager = self.manager([signed_envelope(manifest_payload(reference_now=old_now))])
        manager.refresh(old_now)

        reloaded = self.manager([])
        sources, snapshot = reloaded.load_last_known_good(NOW)

        self.assertEqual(len(sources), 3)
        self.assertTrue(snapshot.stale)
        self.assertEqual(snapshot.status, "stale")

    def test_expired_lkg_remains_stale_after_not_modified_response(self):
        old_now = NOW - timedelta(days=4)
        manager = self.manager(
            [
                signed_envelope(manifest_payload(reference_now=old_now)),
                FakeResponse(b"", status=304),
            ]
        )
        manager.refresh(old_now)

        changed, sources, snapshot = manager.refresh(NOW)

        self.assertFalse(changed)
        self.assertEqual(len(sources), 3)
        self.assertEqual(snapshot.status, "stale")
        self.assertTrue(snapshot.stale)

    def test_startup_refresh_is_due_even_when_lkg_has_a_future_schedule(self):
        manager = self.manager([signed_envelope(manifest_payload())])
        manager.refresh(NOW)
        reloaded = self.manager([])
        reloaded.load_last_known_good(NOW + timedelta(hours=1))

        self.assertTrue(reloaded.due(NOW + timedelta(hours=1)))

    def test_lower_revision_and_fetch_failure_keep_current_lkg(self):
        import urllib.error

        manager = self.manager(
            [
                signed_envelope(manifest_payload(revision=20260720031700)),
                signed_envelope(manifest_payload(revision=20260719031700)),
                urllib.error.URLError("offline"),
            ]
        )
        manager.refresh(NOW)
        envelope_before = (self.data_dir / "source-list-envelope.json").read_bytes()

        changed, sources, rollback = manager.refresh(NOW + timedelta(minutes=1))
        self.assertFalse(changed)
        self.assertEqual(len(sources), 3)
        self.assertIn("rollback", rollback.last_error)
        self.assertEqual(rollback.next_refresh_at, (NOW + timedelta(minutes=6)).isoformat())

        changed, sources, failed = manager.refresh(NOW + timedelta(minutes=2))
        self.assertFalse(changed)
        self.assertEqual(len(sources), 3)
        self.assertIn("offline", failed.last_error)
        self.assertEqual(failed.next_refresh_at, (NOW + timedelta(minutes=32)).isoformat())
        self.assertEqual((self.data_dir / "source-list-envelope.json").read_bytes(), envelope_before)

    def test_equal_revision_requires_identical_envelope_and_payload(self):
        initial_payload = manifest_payload()
        initial_envelope = signed_envelope(initial_payload)
        changed_document = json.loads(initial_payload)
        changed_document["sources"][0]["probe_mbps"] += 1
        changed_payload = json.dumps(changed_document, separators=(",", ":")).encode("utf-8")
        manager = self.manager(
            [
                initial_envelope,
                signed_envelope(changed_payload),
                initial_envelope + b"\n",
                initial_envelope,
            ]
        )
        manager.refresh(NOW)

        for offset in (1, 2):
            changed, sources, snapshot = manager.refresh(NOW + timedelta(minutes=offset))
            self.assertFalse(changed)
            self.assertEqual(len(sources), 3)
            self.assertIn("revision", snapshot.last_error)
            self.assertEqual((self.data_dir / "source-list-envelope.json").read_bytes(), initial_envelope)

        changed, sources, snapshot = manager.refresh(NOW + timedelta(minutes=3))
        self.assertFalse(changed)
        self.assertEqual(len(sources), 3)
        self.assertEqual(snapshot.status, "ok")

    def test_missing_corrupt_or_interrupted_state_recovers_from_authoritative_envelope(self):
        envelope = signed_envelope(manifest_payload())
        manager = self.manager([envelope])
        manager.refresh(NOW)
        state_path = self.data_dir / "source-list-state.json"

        mutations = (
            lambda: state_path.unlink(),
            lambda: state_path.write_bytes(b"{"),
            lambda: state_path.write_text(
                json.dumps(
                    {
                        "revision": 20260721031700,
                        "failure_count": 0,
                        "last_success_at": (NOW + timedelta(days=1)).isoformat(),
                        "next_refresh_at": (NOW + timedelta(days=2)).isoformat(),
                    }
                ),
                encoding="utf-8",
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                mutate()
                reloaded = self.manager([])
                sources, snapshot = reloaded.load_last_known_good(NOW)
                self.assertEqual(len(sources), 3)
                self.assertEqual(snapshot.revision, 20260720031700)
                self.assertEqual(snapshot.status, "ok")

    def test_state_write_failure_keeps_previous_complete_lkg(self):
        old_envelope = signed_envelope(manifest_payload())
        new_envelope = signed_envelope(manifest_payload(revision=20260721031700, reference_now=NOW + timedelta(days=1)))
        manager = self.manager([old_envelope, new_envelope])
        manager.refresh(NOW)
        old_sources = list(manager.current_urls)
        original_write = manager._atomic_write
        failed = False

        def fail_state_once(path, data):
            nonlocal failed
            if path == manager.state_path and not failed:
                failed = True
                raise OSError("state disk error")
            return original_write(path, data)

        with patch.object(manager, "_atomic_write", side_effect=fail_state_once):
            changed, sources, snapshot = manager.refresh(NOW + timedelta(days=1))

        self.assertFalse(changed)
        self.assertEqual(sources, old_sources)
        self.assertIn("state disk error", snapshot.last_error)
        reloaded_sources, _snapshot = self.manager([]).load_last_known_good(NOW + timedelta(days=1))
        self.assertEqual(reloaded_sources, old_sources)
        self.assertEqual((self.data_dir / "source-list-envelope.json").read_bytes(), old_envelope)

    def test_envelope_write_failure_or_interruption_keeps_previous_lkg(self):
        old_envelope = signed_envelope(manifest_payload())
        new_envelope = signed_envelope(manifest_payload(revision=20260721031700, reference_now=NOW + timedelta(days=1)))
        manager = self.manager([old_envelope, new_envelope])
        manager.refresh(NOW)
        old_sources = list(manager.current_urls)
        original_write = manager._atomic_write

        def fail_envelope(path, data):
            if path == manager.envelope_path:
                raise OSError("envelope disk error")
            return original_write(path, data)

        with patch.object(manager, "_atomic_write", side_effect=fail_envelope):
            changed, sources, snapshot = manager.refresh(NOW + timedelta(days=1))

        self.assertFalse(changed)
        self.assertEqual(sources, old_sources)
        self.assertIn("envelope disk error", snapshot.last_error)
        reloaded_sources, reloaded_snapshot = self.manager([]).load_last_known_good(NOW + timedelta(days=1))
        self.assertEqual(reloaded_sources, old_sources)
        self.assertEqual(reloaded_snapshot.revision, 20260720031700)

    def test_no_lkg_failure_backoff_persists_across_restarts(self):
        import urllib.error

        attempt_times = [
            NOW,
            NOW + timedelta(seconds=300),
            NOW + timedelta(seconds=2100),
            NOW + timedelta(seconds=9300),
        ]
        delays = [300, 1800, 7200, 21600]
        for index, (attempt_at, delay) in enumerate(zip(attempt_times, delays), start=1):
            manager = self.manager([urllib.error.URLError(f"offline-{index}")])
            sources, loaded = manager.load_last_known_good(attempt_at)
            self.assertEqual(sources, [])
            if index > 1:
                self.assertEqual(manager.failure_count, index - 1)
            _changed, sources, failed = manager.refresh(attempt_at)
            self.assertEqual(sources, [])
            self.assertEqual(failed.next_refresh_at, (attempt_at + timedelta(seconds=delay)).isoformat())
            state = json.loads((self.data_dir / "source-list-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["revision"], 0)
            self.assertEqual(state["failure_count"], index)
            restarted = self.manager([])
            restarted.load_last_known_good(attempt_at + timedelta(seconds=1))
            self.assertFalse(restarted.due(attempt_at + timedelta(seconds=1)))
        self.assertFalse((self.data_dir / "source-list-envelope.json").exists())

    def test_oversized_envelope_is_rejected_before_verification(self):
        manager = self.manager([b"x" * (524_288 + 1)])

        changed, sources, snapshot = manager.refresh(NOW)

        self.assertFalse(changed)
        self.assertEqual(sources, [])
        self.assertIn("too large", snapshot.last_error)


class RemoteSourceRefreshWorkerTests(unittest.TestCase):
    def test_request_is_non_blocking_and_coalesces_while_refresh_is_running(self):
        from steam_pumper.remote_sources import RemoteSourceRefreshWorker, SourceListSnapshot

        entered = threading.Event()
        release = threading.Event()

        class SlowManager:
            def refresh(self, now=None):
                entered.set()
                release.wait(timeout=2)
                return True, ["https://mirror.example/file"], SourceListSnapshot(
                    status="ok", revision=20260720031700, source_count=1
                )

            def due(self, _now=None):
                return True

        worker = RemoteSourceRefreshWorker(SlowManager())
        self.addCleanup(worker.shutdown)

        started = time.monotonic()
        self.assertEqual(worker.request(NOW), "queued")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(entered.wait(timeout=1))
        self.assertEqual(worker.request(NOW), "running")
        self.assertIsNone(worker.poll_result())

        release.set()
        deadline = time.monotonic() + 1
        result = None
        while result is None and time.monotonic() < deadline:
            result = worker.poll_result()
            time.sleep(0.01)
        self.assertIsNotNone(result)
        self.assertEqual(result[2].revision, 20260720031700)

    def test_due_check_schedules_work_without_performing_refresh_inline(self):
        from steam_pumper.remote_sources import RemoteSourceRefreshWorker, SourceListSnapshot

        manager = unittest.mock.Mock()
        manager.due.return_value = True
        manager.refresh.return_value = (False, [], SourceListSnapshot(status="error"))
        worker = RemoteSourceRefreshWorker(manager)
        self.addCleanup(worker.shutdown)

        self.assertEqual(worker.request_if_due(NOW), "queued")
        manager.due.assert_called_once_with(NOW)


if __name__ == "__main__":
    unittest.main()
