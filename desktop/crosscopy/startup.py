"""Windows "start with Windows" support.

Uses the per-user ``HKCU\\...\\CurrentVersion\\Run`` key: no administrator
rights, no shortcut to maintain, and no console window flash at login (the
packaged build is a ``--windowed`` exe, and development mode uses ``pythonw.exe``).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

APP_NAME = "CrossCopy"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = APP_NAME

logger = logging.getLogger("crosscopy.startup")


def is_supported() -> bool:
    return sys.platform == "win32"


def _launcher_script() -> Path:
    # desktop/crosscopy/startup.py -> desktop/launcher.py
    return Path(__file__).resolve().parents[1] / "launcher.py"


def launch_command() -> str:
    """Command line that starts CrossCopy with no console window."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}"'

    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if not pythonw.exists():  # venvs without the GUI launcher
        pythonw = python
    return f'"{pythonw}" "{_launcher_script()}"'


def _open_run_key(write: bool = False):
    import winreg

    access = winreg.KEY_SET_VALUE if write else winreg.KEY_QUERY_VALUE
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def is_enabled(name: str = _VALUE_NAME) -> bool:
    """True when an autostart entry exists for this app (any target)."""
    if not is_supported():
        return False
    try:
        with _open_run_key() as key:
            import winreg

            winreg.QueryValueEx(key, name)
    except OSError:
        return False
    return True


def current_command(name: str = _VALUE_NAME) -> str | None:
    if not is_supported():
        return None
    try:
        with _open_run_key() as key:
            import winreg

            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value) if value else None


def enable(command: str | None = None, name: str = _VALUE_NAME) -> str:
    """Register CrossCopy to start at login; returns the command that was stored."""
    if not is_supported():
        raise OSError("autostart is only supported on Windows")

    value = command or launch_command()
    import winreg

    with _open_run_key(write=True) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    logger.info("autostart enabled: %s", value)
    return value


def disable(name: str = _VALUE_NAME) -> bool:
    """Remove the autostart entry; returns True when something was removed."""
    if not is_supported():
        return False
    try:
        with _open_run_key(write=True) as key:
            import winreg

            winreg.DeleteValue(key, name)
    except OSError:
        return False
    logger.info("autostart disabled")
    return True
