import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublisherImageTests(unittest.TestCase):
    def test_publisher_is_hardened_multi_stage_image(self):
        dockerfile = (ROOT / "Dockerfile.publisher").read_text(encoding="utf-8")
        self.assertIn("FROM golang:1.23", dockerfile)
        self.assertIn("FROM python:3.13-slim", dockerfile)
        self.assertIn("ca-certificates", dockerfile)
        self.assertIn("tzdata", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^\s*EXPOSE\b")
        self.assertIn('ENTRYPOINT ["/usr/local/bin/publisher"]', dockerfile)
        self.assertIn('CMD ["scheduler"]', dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertNotRegex(dockerfile, r"(?i)^(?:ARG|ENV).*ACCESS_KEY")

    def test_ossutil_archives_have_required_version_and_checksums(self):
        dockerfile = (ROOT / "Dockerfile.publisher").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^ARG TARGETARCH=amd64$")
        self.assertIn("ossutil-2.3.0-linux-${TARGETARCH}.zip", dockerfile)
        self.assertIn("3ae4d9fc85a7a6e9f5654d1599766f1a3a42a3692870887b5ae9338d582ef65a", dockerfile)
        self.assertIn("f6c95ba0c2d2ef30290af686ce4d706c701f4734ce8090bee4288a77e3f1d764", dockerfile)
        self.assertIn("sha256sum -c", dockerfile)

    def test_repository_has_exactly_three_dockerfiles(self):
        self.assertEqual(sorted(path.name for path in ROOT.glob("Dockerfile*")), [
            "Dockerfile.ikuai-line", "Dockerfile.multi-ip", "Dockerfile.publisher"
        ])


if __name__ == "__main__":
    unittest.main()
