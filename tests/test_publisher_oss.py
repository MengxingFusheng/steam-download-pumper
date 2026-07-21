import json
import http.client
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.test_publisher_config import BASE_ENV


class PublisherOSSTests(unittest.TestCase):
    def test_credentials_are_only_in_private_child_environment(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.json"
            fake = root / "ossutil"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,sys\n"
                "p=os.path.join(os.path.dirname(sys.argv[0]),'capture.json')\n"
                "json.dump({'argv':sys.argv[1:],'env':{k:v for k,v in os.environ.items() if k.startswith('OSS_')}},open(p,'w'))\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            source = root / "release.json"
            source.write_text("{}", encoding="utf-8")
            config = PublisherConfig.from_env({**BASE_ENV, "OSSUTIL_PATH": str(fake)})
            secrets = PublisherSecrets("private", "AK-ID-VALUE", "AK-SECRET-VALUE")
            before = dict(os.environ)
            OSSClient(config, secrets).upload(source, "pumper/v1/releases/20260720031700.json")
            recorded = json.loads(capture.read_text(encoding="utf-8"))
            argv_text = " ".join(recorded["argv"])
            self.assertNotIn("AK-ID-VALUE", argv_text)
            self.assertNotIn("AK-SECRET-VALUE", argv_text)
            self.assertEqual(recorded["env"]["OSS_ACCESS_KEY_ID"], "AK-ID-VALUE")
            self.assertEqual(recorded["env"]["OSS_ACCESS_KEY_SECRET"], "AK-SECRET-VALUE")
            self.assertEqual(recorded["env"]["OSS_REGION"], "cn-beijing")
            self.assertEqual(recorded["argv"][:2], ["api", "put-object"])
            self.assertIn("--bucket", recorded["argv"])
            self.assertIn("--key", recorded["argv"])
            self.assertIn("--body", recorded["argv"])
            self.assertNotIn("--forbid-overwrite", recorded["argv"])
            self.assertNotIn("--acl", recorded["argv"])
            self.assertEqual(dict(os.environ), before)

    def test_immutable_release_upload_never_uses_force(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.json"
            fake = root / "ossutil"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,sys\n"
                "p=os.path.join(os.path.dirname(sys.argv[0]),'capture.json')\n"
                "json.dump(sys.argv[1:],open(p,'w'))\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            source = root / "release.json"
            source.write_text("{}", encoding="utf-8")
            config = PublisherConfig.from_env({**BASE_ENV, "OSSUTIL_PATH": str(fake)})
            client = OSSClient(config, PublisherSecrets("private", "id", "secret"))
            client.upload(
                source,
                "pumper/v1/releases/20260720031700.json",
                overwrite=False,
            )
            argv = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(argv[:2], ["api", "put-object"])
            self.assertIn("--forbid-overwrite", argv)
            self.assertNotIn("true", argv)

    def test_upload_subprocess_is_terminated_on_cancellation(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient, OSSFailure

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "ossutil"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os,time\n"
                "open(os.path.join(os.path.dirname(__file__),'pid'),'w').write(str(os.getpid()))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            source = root / "release.json"
            source.write_text("{}", encoding="utf-8")
            config = PublisherConfig.from_env({**BASE_ENV, "OSSUTIL_PATH": str(fake)})
            cancel = threading.Event()
            timer = threading.Timer(0.15, cancel.set)
            timer.start()
            started = time.monotonic()
            with self.assertRaises(OSSFailure):
                OSSClient(config, PublisherSecrets("private", "id", "secret")).upload(
                    source,
                    "pumper/v1/releases/20260720031700.json",
                    overwrite=False,
                    cancel_event=cancel,
                    deadline=started + 10,
                )
            timer.cancel()
            self.assertLess(time.monotonic() - started, 1.5)

    def test_public_read_forwards_publication_deadline_and_cancellation(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient

        calls = []
        cancel = threading.Event()

        def request(_url, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(status=200, headers={}, body=b"{}")

        config = PublisherConfig.from_env(BASE_ENV)
        deadline = time.monotonic() + 10
        client = OSSClient(
            config,
            PublisherSecrets("private", "id", "secret"),
            resolver=lambda *_args: ("93.184.216.34",),
            request_fn=request,
        )
        client.read_public("latest.json", deadline=deadline, cancel_event=cancel)
        self.assertIs(calls[0]["cancel_event"], cancel)
        self.assertEqual(calls[0]["deadline"], deadline)

    def test_public_read_requires_https_and_is_bounded(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient, OSSFailure

        config = PublisherConfig.from_env(BASE_ENV)
        client = OSSClient(config, PublisherSecrets("private", "id", "secret"))
        with self.assertRaises(OSSFailure):
            client.read_url("http://example.test/latest.json")

    def test_public_read_is_limited_to_configured_origin_and_object_path(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient, OSSFailure

        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status=200, headers={}, body=b"{}")

        config = PublisherConfig.from_env(BASE_ENV)
        client = OSSClient(
            config,
            PublisherSecrets("private", "id", "secret"),
            resolver=lambda *_args: ("93.184.216.34",),
            request_fn=request,
        )
        self.assertEqual(client.read_public("latest.json"), b"{}")
        self.assertEqual(
            calls[0][0],
            "https://pumper-source-list-example.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json",
        )
        for url in (
            "https://evil.example/pumper/v1/latest.json",
            "http://pumper-source-list-example.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json",
            "https://pumper-source-list-example.oss-cn-beijing.aliyuncs.com/other/latest.json",
            "https://pumper-source-list-example.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json?x=1",
        ):
            with self.subTest(url=url), self.assertRaises(OSSFailure):
                client.read_url(url)
        self.assertEqual(len(calls), 1)

    def test_public_read_rejects_private_resolution_and_all_redirects(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient, OSSFailure

        config = PublisherConfig.from_env(BASE_ENV)
        private = OSSClient(
            config,
            PublisherSecrets("private", "id", "secret"),
            resolver=lambda *_args: ("127.0.0.1",),
        )
        with self.assertRaises(OSSFailure):
            private.read_public("latest.json")

        redirect = OSSClient(
            config,
            PublisherSecrets("private", "id", "secret"),
            resolver=lambda *_args: ("93.184.216.34",),
            request_fn=lambda *_args, **_kwargs: SimpleNamespace(
                status=302,
                headers={"Location": "https://evil.example/pumper/v1/latest.json"},
                body=b"",
            ),
        )
        with self.assertRaises(OSSFailure):
            redirect.read_public("latest.json")

    def test_public_read_maps_malformed_http_response_to_oss_failure(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient, OSSFailure

        config = PublisherConfig.from_env(BASE_ENV)
        client = OSSClient(
            config,
            PublisherSecrets("private", "id", "secret"),
            resolver=lambda *_args: ("93.184.216.34",),
            request_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                http.client.BadStatusLine("malformed")
            ),
        )
        with self.assertRaises(OSSFailure):
            client.read_public("latest.json")


if __name__ == "__main__":
    unittest.main()
