"""Rotating log file (no console output)."""
import logging
import logging.handlers
import os

from . import config


def setup(level="INFO", folder=None):
    folder = folder or config.log_dir()
    log = logging.getLogger("knob")
    log.setLevel(getattr(logging, level, logging.INFO))
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    try:
        os.makedirs(folder, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(folder, config.LOG_NAME), maxBytes=256 * 1024, backupCount=2,
            encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.propagate = False
    return log
