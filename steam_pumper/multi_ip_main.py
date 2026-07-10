from __future__ import annotations

import sys

from .application import run_application


TOPOLOGY = "multi_ip"


def main() -> int:
    return run_application(TOPOLOGY)


if __name__ == "__main__":
    sys.exit(main())
