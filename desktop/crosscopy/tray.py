"""System-tray presence for the desktop app.

The tray is intentionally small: one icon that shows whether a phone is
connected, copies the pairing link, opens the log, toggles "start with Windows"
and quits. All state lives in :class:`~crosscopy.app.CrossCopyApp`; this module
only renders it. ``pystray`` is optional at import time so the headless mode and
the test-suite keep working without it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import startup
from .logfile import log_path

try:  # pragma: no cover - exercised only when the deps are missing
    from PIL import Image, ImageDraw
    from pystray import Icon, Menu, MenuItem
except ImportError:  # pragma: no cover
    Image = None
    ImageDraw = None
    Icon = None
    Menu = None
    MenuItem = None

if TYPE_CHECKING:
    from .app import CrossCopyApp

logger = logging.getLogger("crosscopy.tray")

_ICON_FILES = ("crosscopy.png", "crosscopy.ico")


def asset_path(*names: str) -> Path | None:
    """Find a bundled asset both in the source tree and in a PyInstaller onefile."""
    candidates: list[Path] = []
    frozen_dir = getattr(sys, "_MEIPASS", None)
    if frozen_dir:
        candidates += [Path(frozen_dir) / "assets" / name for name in names]
        candidates += [Path(frozen_dir) / name for name in names]
    here = Path(__file__).resolve()
    for base in here.parents[:3]:
        candidates += [base / "assets" / name for name in names]
    for path in candidates:
        if path.exists():
            return path
    return None


def _fallback_image() -> Any:
    """Tiny drawn icon so the tray still works when the assets are missing."""
    if Image is None or ImageDraw is None:  # pragma: no cover
        return None
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 61, 61), radius=14, fill=(11, 18, 36, 255))
    draw.rounded_rectangle((18, 12, 46, 52), radius=4, outline=(94, 234, 255, 255), width=3)
    draw.line((24, 32, 40, 32), fill=(170, 150, 255, 255), width=3)
    return image


def load_icon_image() -> Any:
    if Image is None:  # pragma: no cover
        return None
    path = asset_path(*_ICON_FILES)
    if path is not None:
        try:
            with Image.open(path) as image:
                return image.convert("RGBA")
        except Exception:
            logger.warning("could not load tray icon from %s", path, exc_info=True)
    logger.info("using the built-in tray icon")
    return _fallback_image()


class TrayController:
    """Owns the tray icon; safe to instantiate when pystray is not installed."""

    def __init__(self, app: "CrossCopyApp") -> None:
        self.app = app
        self._icon: Any = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def available(self) -> bool:
        return Icon is not None

    @property
    def running(self) -> bool:
        return self._icon is not None

    def start(self) -> bool:
        """Show the tray icon. Returns True when it is running."""
        with self._lock:
            if self._icon is not None:
                return True
            if Icon is None or Menu is None or MenuItem is None:
                logger.warning(
                    "tray unavailable: install pystray and Pillow "
                    "(`pip install -r requirements.txt`)"
                )
                return False

            image = load_icon_image()
            if image is None:  # pragma: no cover
                logger.warning("tray unavailable: no icon image")
                return False

            try:
                icon = Icon(
                    "crosscopy",
                    image,
                    "CrossCopy",
                    menu=self._build_menu(),
                )
                # run_detached() starts pystray's own message loop in a thread,
                # which leaves the asyncio loop free to serve the phones.
                icon.run_detached()
            except Exception:
                logger.exception("could not start the tray icon")
                return False

            self._icon = icon
            logger.info("tray icon visible")
            return True

    def stop(self) -> None:
        with self._lock:
            icon, self._icon = self._icon, None
        if icon is None:
            return
        try:
            icon.visible = False
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            logger.debug("tray shutdown failed", exc_info=True)

    def refresh(self) -> None:
        """Re-evaluate the dynamic menu entries (status, device name)."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.update_menu()
        except Exception:
            logger.debug("tray menu refresh failed", exc_info=True)

    def notify(self, message: str, title: str = "CrossCopy") -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(message, title)
        except Exception:
            logger.debug("tray notification failed", exc_info=True)

    # -- menu --------------------------------------------------------------

    def _build_menu(self) -> Any:
        return Menu(
            MenuItem(self._status_label, None, enabled=False),
            MenuItem(self._detail_label, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Open CrossCopy", self._open_window, default=True),
            MenuItem("Copy pairing link", self._copy_link),
            Menu.SEPARATOR,
            MenuItem("Open log", self._open_log),
            MenuItem("Start with Windows", self._toggle_startup, checked=self._startup_checked),
            Menu.SEPARATOR,
            MenuItem("Quit CrossCopy", self._quit),
        )

    def _status_label(self, item: Any = None) -> str:
        count = self._peer_count()
        if count == 0:
            return "Waiting for a phone"
        return f"Connected to {count} phone" if count == 1 else f"Connected to {count} phones"

    def _detail_label(self, item: Any = None) -> str:
        names = self._peer_names()
        if names:
            return ", ".join(names)
        url = self._url()
        return url if len(url) <= 60 else url[:57] + "..."

    def _startup_checked(self, item: Any = None) -> bool:
        try:
            return startup.is_enabled()
        except Exception:  # pragma: no cover - registry access is best effort
            return False

    def _peer_count(self) -> int:
        try:
            return len(self.app.hub.peers())
        except Exception:  # pragma: no cover - app may be shutting down
            return 0

    def _peer_names(self) -> list[str]:
        try:
            return [peer.name for peer in self.app.hub.peers()]
        except Exception:  # pragma: no cover
            return []

    def _url(self) -> str:
        try:
            return self.app.pairing_url()
        except Exception:  # pragma: no cover
            return ""

    # -- actions (pystray calls these on its own thread) -------------------

    def _open_window(self, icon: Any, item: Any) -> None:
        try:
            self.app.ui.open()
        except Exception:
            logger.exception("could not open the settings window")
            self.notify("Could not open the window; see the log")

    def _copy_link(self, icon: Any, item: Any) -> None:
        url = self._url()
        if not url:
            return
        if self.app.copy_to_clipboard(url):
            self.notify(f"Pairing link copied:\n{url}")
        else:
            self.notify("Could not copy the pairing link", title="CrossCopy")

    def _open_log(self, icon: Any, item: Any) -> None:
        path = log_path()
        try:
            if path.exists():
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                self.notify(f"No log file yet:\n{path}")
        except Exception:
            logger.warning("could not open the log file", exc_info=True)
            self.notify(f"Log file:\n{path}")

    def _toggle_startup(self, icon: Any, item: Any) -> None:
        try:
            if startup.is_enabled():
                startup.disable()
                self.notify("CrossCopy will no longer start with Windows")
            else:
                command = startup.enable()
                self.notify(f"CrossCopy will start with Windows\n{command}")
        except Exception as exc:
            logger.exception("could not change the autostart setting")
            self.notify(f"Could not change autostart:\n{exc}")
        finally:
            self.refresh()

    def _quit(self, icon: Any, item: Any) -> None:
        logger.info("quit requested from the tray")
        self.app.request_stop()
        self.stop()
