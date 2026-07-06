from __future__ import annotations

import logging
import os
import signal
import sys

from .config import PumperConfig, save_config
from .controller import PumperController
from .web import run_web


def main() -> int:
    config_path = os.environ.get("CONFIG_PATH", "/data/config.json")
    if not os.path.exists(config_path):
        save_config(config_path, PumperConfig())
    controller = PumperController(config_path)
    logging.basicConfig(level=getattr(logging, controller.cfg.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")

    def stop(_signum: int, _frame: object) -> None:
        controller.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    controller.start_scheduler()
    run_web(controller, "0.0.0.0", int(os.environ.get("WEB_PORT", "8080")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
