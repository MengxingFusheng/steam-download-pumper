import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


class PublisherManifestTests(unittest.TestCase):
    def test_payload_is_compact_deterministic_and_sorted(self):
        from source_publisher.manifest import build_payload
        from source_publisher.probe import ProbeResult

        checked = datetime(2026, 7, 19, 19, 14, 12, tzinfo=timezone.utc)
        results = [
            ProbeResult("https://b.example/file", checked, 12.25, 4_194_304, True, ""),
            ProbeResult("https://a.example/file", checked, 12.25, 4_194_304, True, ""),
            ProbeResult("https://fast.example/file", checked, 300.5, 4_194_304, True, ""),
            ProbeResult("https://bad.example/file", checked, 0.0, 0, False, "probe failed"),
        ]
        now = datetime(2026, 7, 20, 3, 17, 0, tzinfo=timezone.utc).astimezone()
        first, revision = build_payload(results, now)
        second, second_revision = build_payload(results, now)
        self.assertEqual(first, second)
        self.assertEqual(revision, second_revision)
        self.assertNotIn(b" ", first)
        payload = json.loads(first)
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(len(str(payload["revision"])), 14)
        self.assertEqual(
            [source["url"] for source in payload["sources"]],
            ["https://fast.example/file", "https://a.example/file", "https://b.example/file"],
        )
        generated = datetime.fromisoformat(payload["generated_at"])
        expires = datetime.fromisoformat(payload["expires_at"])
        self.assertEqual((expires - generated).total_seconds(), 72 * 3600)

    def test_sign_and_verify_use_private_files_and_stdin(self):
        from source_publisher.manifest import (
            sign_payload,
            verify_envelope,
            verify_envelope_with_private_key,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key = root / "private.key"
            private_key.write_text("private-material", encoding="utf-8")
            fake = root / "manifestctl"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import base64,json,sys\n"
                "if sys.argv[1] == 'sign':\n"
                " p=sys.stdin.buffer.read(); print(json.dumps({'key_id':sys.argv[-1],'algorithm':'Ed25519','payload':base64.b64encode(p).decode(),'signature':base64.b64encode(bytes(64)).decode()},separators=(',',':')))\n"
                "else:\n"
                " e=json.load(sys.stdin); sys.stdout.buffer.write(base64.b64decode(e['payload']))\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            payload = b'{"schema":1}'
            envelope = sign_payload(payload, private_key, "test-key", str(fake))
            self.assertEqual(verify_envelope(envelope, "public-key", "test-key", str(fake)), payload)
            self.assertEqual(
                verify_envelope_with_private_key(envelope, private_key, "test-key", str(fake)),
                payload,
            )
            self.assertNotIn(b"private-material", envelope)

    def test_signing_subprocess_is_terminated_on_cancellation(self):
        from source_publisher.manifest import ManifestError, sign_payload

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key = root / "private.key"
            private_key.write_text("private-material", encoding="utf-8")
            fake = root / "manifestctl"
            fake.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            cancel = threading.Event()
            timer = threading.Timer(0.15, cancel.set)
            timer.start()
            started = time.monotonic()
            with self.assertRaises(ManifestError):
                sign_payload(
                    b'{"schema":1}',
                    private_key,
                    "test-key",
                    str(fake),
                    cancel_event=cancel,
                    deadline=started + 10,
                )
            timer.cancel()
            self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
