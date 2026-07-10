from __future__ import annotations

import logging
import os
import signal

from .config import save_config
from .controller import PumperController
from .web import run_web


def run_application(topology_name: str) -> int:
    config_path = os.environ.get("CONFIG_PATH", "/data/config.json")
    config_exists = os.path.exists(config_path)
    controller = PumperController(topology_name, config_path)
    if not config_exists:
        save_config(config_path, controller.cfg)
    logging.basicConfig(
        level=getattr(logging, controller.cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    def stop(_signum: int, _frame: object) -> None:
        controller.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    controller.start_scheduler()
    run_web(controller, "0.0.0.0", int(os.environ.get("WEB_PORT", "80")), topology_name)
    return 0
