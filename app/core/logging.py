"""Canonical console logger — keep identical everywhere (see CLAUDE.md).

A single StreamHandler to stdout so the developer sees clean, structured lines
live in cmd while `make run` is active. Never use bare `print()`.
"""

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-20s %(message)s", "%H:%M:%S"
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
