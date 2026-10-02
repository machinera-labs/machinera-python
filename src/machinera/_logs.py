from __future__ import annotations

import logging
import os

logger = logging.getLogger("machinera")

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO}


def configure_from_env() -> None:
    level = _LEVELS.get(os.environ.get("MACHINERA_LOG", "").strip().lower())
    if level is None:
        return
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
