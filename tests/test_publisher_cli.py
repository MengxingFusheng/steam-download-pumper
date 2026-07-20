import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_publisher_config import BASE_ENV


class PublisherCliTests(unittest.TestCase):
    def test_stable_exit_codes(self):
        from source_publisher.main import ExitCode

        self.assertEqual(
            {member.name: member.value for member in ExitCode},
            {
                "OK": 0,
                "INVALID_INPUT": 2,
                "INSUFFICIENT_SOURCES": 3,
                "SIGNING_FAILURE": 4,
                "OSS_FAILURE": 5,
                "LOCKED": 6,
                "UNHEALTHY": 7,
            },
        )

    def test_default_command_is_scheduler(self):
        from source_publisher.main import parse_args

        self.assertEqual(parse_args([]).command, "scheduler")
        for command in ("scheduler", "publish-once", "validate-only", "healthcheck"):
            with self.subTest(command=command):
                self.assertEqual(parse_args([command]).command, command)

    def test_healthcheck_returns_unhealthy_when_secrets_are_missing(self):
        from source_publisher.main import main

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {**BASE_ENV, "SECRETS_DIR": str(Path(directory) / "missing")},
            clear=True,
        ):
            self.assertEqual(main(["healthcheck"]), 7)


if __name__ == "__main__":
    unittest.main()
