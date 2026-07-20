import json
import os
import tempfile
import unittest
from pathlib import Path

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
            self.assertIn("cp", recorded["argv"])
            self.assertIn("--force", recorded["argv"])
            self.assertNotIn("--acl", recorded["argv"])
            self.assertEqual(dict(os.environ), before)

    def test_public_read_requires_https_and_is_bounded(self):
        from source_publisher.config import PublisherConfig, PublisherSecrets
        from source_publisher.oss import OSSClient, OSSFailure

        config = PublisherConfig.from_env(BASE_ENV)
        client = OSSClient(config, PublisherSecrets("private", "id", "secret"))
        with self.assertRaises(OSSFailure):
            client.read_url("http://example.test/latest.json")


if __name__ == "__main__":
    unittest.main()
