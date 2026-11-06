"""Log file location and logging setup for the desktop app.

A packaged (``--windowed``) build has no console, so every run writes to
``%LOCALAPPDATA%\\CrossCopy\\crosscopy.log``. The tray's *Open log* item points
at the same file, which makes "it silently did nothing" debuggable.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "CrossCopy"


def log_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / APP_NAME


def log_path() -> Path:
    return log_directory() / "crosscopy.log"


def has_console() -> bool:
    """True when stdout can actually be written to (false for a windowed exe)."""
    return sys.stdout is not None and sys.stderr is not None


def configure_logging(
    level: str = "INFO",
    *,
    to_file: bool = True,
    to_console: bool | None = None,
) -> Path | None:
    """Install root handlers; returns the log file path when file logging works."""
    if to_console is None:
        to_console = has_console()

    handlers: list[logging.Handler] = []
    path: Path | None = None

    if to_file:
        try:
            log_directory().mkdir(parents=True, exist_ok=True)
            path = log_path()
            handlers.append(
                RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            )
        except OSError:
            path = None

    if to_console:
        stream = sys.stderr if sys.stderr is not None else sys.stdout
        if stream is not None:
            handlers.append(logging.StreamHandler(stream))

    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # These libraries log a line per frame / per decode at INFO.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("pystray").setLevel(logging.INFO)
    return path
