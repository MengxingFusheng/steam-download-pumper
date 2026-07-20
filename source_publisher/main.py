from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from datetime import datetime
from enum import IntEnum
from typing import Sequence

from .config import PublisherConfig, PublisherSecrets
from .manifest import ManifestError
from .oss import OSSFailure
from .scheduler import LockHeld, exclusive_lock, health_is_healthy, run_scheduler
from .service import InsufficientSources, PublicationInterrupted, PublicationService


class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    INSUFFICIENT_SOURCES = 3
    SIGNING_FAILURE = 4
    OSS_FAILURE = 5
    LOCKED = 6
    UNHEALTHY = 7


COMMANDS = ("scheduler", "publish-once", "validate-only", "healthcheck")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="publisher")
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="scheduler")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = PublisherConfig.from_env()
        if args.command == "validate-only":
            result = PublicationService(config).run(
                datetime.now(config.timezone), validate_only=True
            )
            print(json.dumps({
                "status": "valid",
                "revision": result.revision,
                "healthy_sources": result.source_count,
            }, separators=(",", ":")))
            return int(ExitCode.OK)

        if args.command == "healthcheck":
            try:
                PublisherSecrets.from_directory(config.secret_dir)
            except (ValueError, OSError):
                return int(ExitCode.UNHEALTHY)
            healthy = health_is_healthy(
                config.state_dir / "health.json", datetime.now(config.timezone)
            )
            return int(ExitCode.OK if healthy else ExitCode.UNHEALTHY)

        secrets = PublisherSecrets.from_directory(config.secret_dir)
        service = PublicationService(config, secrets)
        with exclusive_lock(config.state_dir / "publish.lock"):
            if args.command == "publish-once":
                result = service.run(datetime.now(config.timezone))
                print(json.dumps({
                    "status": "published",
                    "revision": result.revision,
                    "healthy_sources": result.source_count,
                }, separators=(",", ":")))
                return int(ExitCode.OK)

            stop_event = threading.Event()

            def stop(_signum, _frame):  # type: ignore[no-untyped-def]
                stop_event.set()

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            return run_scheduler(config, service, stop_event)
    except (ValueError, OSError):
        _error("invalid configuration or candidate data")
        return int(ExitCode.INVALID_INPUT)
    except InsufficientSources:
        _error("insufficient healthy sources")
        return int(ExitCode.INSUFFICIENT_SOURCES)
    except (ManifestError, PublicationInterrupted):
        _error("manifest signing or verification failed")
        return int(ExitCode.SIGNING_FAILURE)
    except OSSFailure:
        _error("OSS publication or public verification failed")
        return int(ExitCode.OSS_FAILURE)
    except LockHeld:
        _error("publisher lock is already held")
        return int(ExitCode.LOCKED)
    except Exception:
        _error("publication failed")
        return int(ExitCode.OSS_FAILURE)


def _error(message: str) -> None:
    print(json.dumps({"level": "error", "message": message}, separators=(",", ":")), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
