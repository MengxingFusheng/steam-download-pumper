from __future__ import annotations

import logging
import os
import signal
from collections.abc import Mapping
from pathlib import Path

from .config import save_config
from .controller import PumperController
from .remote_sources import RemoteSourceManager, RemoteSourceSettings
from .web import run_web


def build_remote_source_manager(
    topology_name: str,
    config_path: str | Path,
    env: Mapping[str, str],
) -> tuple[RemoteSourceManager | None, str]:
    if topology_name != "multi_ip":
        return None, ""
    try:
        settings = RemoteSourceSettings.from_env(env)
    except ValueError as exc:
        return None, str(exc)
    if not settings.enabled:
        return None, ""
    return RemoteSourceManager(settings, data_dir=Path(config_path).parent), ""


def run_application(topology_name: str) -> int:
    config_path = os.environ.get("CONFIG_PATH", "/data/config.json")
    config_exists = os.path.exists(config_path)
    remote_manager, remote_error = build_remote_source_manager(topology_name, config_path, os.environ)
    controller = PumperController(
        topology_name,
        config_path,
        remote_source_manager=remote_manager,
        remote_source_error=remote_error,
    )
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
    run_web(controller, "0.0.0.0", int(os.environ.get("WEB_PORT", "80")), topology_name)
    return 0
