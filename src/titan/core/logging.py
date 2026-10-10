"""Bounded local operational logs; never record snapshots or identifiers."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(directory: Path) -> None:
    logger = logging.getLogger("titan")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            directory / "titan.log", maxBytes=256_000, backupCount=2, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.info("Session started; logs contain operational events only")
