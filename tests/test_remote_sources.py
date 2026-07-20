import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


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

    def test_settings_accept_short_runtime_variable_names(self):
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

        self.assertEqual(settings.refresh_jitter_seconds, 17)
        self.assertEqual(settings.fetch_timeout_seconds, 9)
        self.assertEqual(settings.max_bytes, 4096)
        self.assertEqual(settings.min_sources, 4)

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
            ({**base, "JITTER": "-1"}, "JITTER"),
            ({**base, "MIN_SOURCES": "101"}, "MIN_SOURCES"),
        )
        for env, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    RemoteSourceSettings.from_env(env)


class ManifestValidationTests(unittest.TestCase):
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
        self.verifier.write_text(
            "#!/usr/bin/env python3\n"
            "import base64,json,sys\n"
            "body=json.load(sys.stdin)\n"
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
                "JITTER": "0",
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
            return FakeResponse(item, headers={"ETag": '"revision"'})

        return RemoteSourceManager(
            self.settings(),
            data_dir=self.data_dir,
            verifier_path=self.verifier,
            urlopen=urlopen,
            resolver=public_resolver,
            hostname="stable-host",
        )

    def test_refresh_verifies_and_atomically_persists_envelope_and_state(self):
        manager = self.manager([signed_envelope(manifest_payload())])

        with patch("steam_pumper.remote_sources.os.replace", wraps=os.replace) as replace:
            changed, sources, snapshot = manager.refresh(NOW)

        self.assertTrue(changed)
        self.assertEqual(len(sources), 3)
        self.assertEqual(snapshot.revision, 20260720031700)
        self.assertEqual(snapshot.source_count, 3)
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.last_success_at, NOW.isoformat())
        self.assertEqual(snapshot.next_refresh_at, (NOW + timedelta(days=1)).isoformat())
        self.assertEqual(replace.call_count, 2)
        self.assertTrue((self.data_dir / "source-list-envelope.json").exists())
        self.assertTrue((self.data_dir / "source-list-state.json").exists())

    def test_startup_loads_expired_lkg_and_marks_it_stale(self):
        old_now = NOW - timedelta(days=4)
        manager = self.manager([signed_envelope(manifest_payload(reference_now=old_now))])
        manager.refresh(old_now)

        reloaded = self.manager([])
        sources, snapshot = reloaded.load_last_known_good(NOW)

        self.assertEqual(len(sources), 3)
        self.assertTrue(snapshot.stale)
        self.assertEqual(snapshot.status, "stale")

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

    def test_oversized_envelope_is_rejected_before_verification(self):
        manager = self.manager([b"x" * (524_288 + 1)])

        changed, sources, snapshot = manager.refresh(NOW)

        self.assertFalse(changed)
        self.assertEqual(sources, [])
        self.assertIn("too large", snapshot.last_error)


if __name__ == "__main__":
    unittest.main()
