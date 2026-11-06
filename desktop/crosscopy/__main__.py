"""``python -m crosscopy`` / ``CrossCopy.exe`` entry point.

Default behaviour is the tray app: the WebSocket server runs on the asyncio
loop and a system-tray icon (also the way to quit) sits in the notification
area. ``--no-tray`` keeps the original headless mode for scripting.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import sys

from . import startup
from .app import CrossCopyApp
from .config import build_parser, settings_from_args
from .logfile import configure_logging, has_console, log_path

_MUTEX_NAME = "CrossCopy.Singleton"
_mutex_handle: int | None = None

logger = logging.getLogger("crosscopy")


def _ensure_streams() -> None:
    """A windowed build has no stdout/stderr; give libraries something to write to."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _acquire_single_instance() -> bool:
    """False when another CrossCopy already runs in this Windows session."""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception:  # pragma: no cover - never block startup on this
        logger.debug("single-instance check failed", exc_info=True)
        return True


def _show_error(message: str, title: str = "CrossCopy") -> None:
    """Surface a fatal error when there is no console to print it to."""
    if has_console():
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:  # pragma: no cover
        pass


def _handle_startup_flags(args) -> int | None:
    if args.install_startup:
        if not startup.is_supported():
            print("autostart is only supported on Windows")
            return 1
        command = startup.enable()
        print(f"CrossCopy will start with Windows:\n  {command}")
        return 0
    if args.uninstall_startup:
        removed = startup.disable()
        print("autostart removed" if removed else "autostart was not enabled")
        return 0
    if args.startup_status:
        enabled = startup.is_enabled()
        print("enabled" if enabled else "disabled")
        if enabled:
            print(f"  {startup.current_command() or ''}")
        return 0
    return None


def main(argv: list[str] | None = None) -> int:
    _ensure_streams()
    parser = build_parser()
    args = parser.parse_args(argv)

    handled = _handle_startup_flags(args)
    if handled is not None:
        return handled

    settings = settings_from_args(args)
    log_file = configure_logging(
        settings.log_level,
        to_file=True,
        to_console=has_console() and not args.hide_console,
    )

    if not _acquire_single_instance():
        logger.info("CrossCopy is already running; exiting")
        return 0

    logger.info(
        "CrossCopy starting%s (log: %s)",
        " with tray" if settings.tray else " headless",
        log_file or "console only",
    )

    try:
        asyncio.run(CrossCopyApp(settings).run())
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        logger.error("could not start the server: %s", exc)
        logger.error("is another CrossCopy instance already using port %d?", settings.port)
        _show_error(
            f"Could not start the CrossCopy server on port {settings.port}.\n\n{exc}\n\n"
            f"Details: {log_path()}"
        )
        return 1
    except Exception as exc:  # a windowed build would otherwise vanish
        logger.exception("unexpected failure")
        _show_error(f"CrossCopy stopped unexpectedly.\n\n{exc}\n\nDetails: {log_path()}")
        return 1

    logger.info("goodbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
