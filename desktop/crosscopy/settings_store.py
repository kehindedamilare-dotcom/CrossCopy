"""Settings the user edits in the window, persisted between runs.

Stored as JSON next to the log (``%LOCALAPPDATA%\\CrossCopy\\config.json``) so a
windowed build can change the pairing token without a console or CLI flags.

Precedence when the app starts: ``--token``/``--port``/``--name`` on the command
line, then this file, then ``CROSSCOPY_TOKEN``. The file outranks the environment
variable on purpose: a token edited in the window must survive a restart even when
an older ``CROSSCOPY_TOKEN`` is still exported in the user's profile.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("crosscopy.config")

DEFAULT_PORT = 8765
MIN_PORT = 1024
MAX_PORT = 65535
MAX_TOKEN_LENGTH = 128
MAX_NAME_LENGTH = 40


def app_dir() -> Path:
    """Directory holding the config file and the log."""
    override = os.environ.get("CROSSCOPY_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "CrossCopy"
    return Path.home() / ".crosscopy"


def config_path() -> Path:
    override = os.environ.get("CROSSCOPY_CONFIG")
    if override:
        return Path(override)
    return app_dir() / "config.json"


@dataclass(frozen=True, slots=True)
class StoredSettings:
    token: str | None = None
    port: int = DEFAULT_PORT
    device_name: str = ""


def _clean_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token or None


def _clean_port(value: object) -> int:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if MIN_PORT <= port <= MAX_PORT else DEFAULT_PORT


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value.strip() if char.isprintable())[:MAX_NAME_LENGTH]


def load() -> StoredSettings:
    """Read the saved settings, falling back to defaults for anything unusable."""
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return StoredSettings()
    except (OSError, ValueError):
        logger.warning("ignoring unreadable settings file %s", path, exc_info=True)
        return StoredSettings()
    if not isinstance(raw, dict):
        logger.warning("ignoring settings file %s: expected a JSON object", path)
        return StoredSettings()
    return StoredSettings(
        token=_clean_token(raw.get("token")),
        port=_clean_port(raw.get("port")),
        device_name=_clean_name(raw.get("device_name")),
    )


def save(token: str | None, port: int, device_name: str = "") -> Path:
    """Write the settings atomically and return the file path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": _clean_token(token),
        "port": _clean_port(port),
        "device_name": _clean_name(device_name),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    logger.info("settings saved to %s", path)
    return path


def validate_fields(
    token: str, port_text: str, device_name: str
) -> tuple[dict[str, object] | None, list[str]]:
    """Check the window's text fields before anything is written.

    Returns ``(values, errors)``; ``values`` is ``None`` while ``errors`` is non-empty.
    """
    errors: list[str] = []

    cleaned = _clean_token(token)
    if cleaned is not None:
        if any(char.isspace() for char in cleaned):
            errors.append("The token can't contain spaces.")
        elif len(cleaned) > MAX_TOKEN_LENGTH:
            errors.append(f"Keep the token under {MAX_TOKEN_LENGTH} characters.")
        elif not all(char.isprintable() for char in cleaned):
            errors.append("The token can only contain printable characters.")

    try:
        port = int(port_text.strip())
    except (TypeError, ValueError):
        errors.append("The port must be a number, for example 8765.")
        port = DEFAULT_PORT
    else:
        if not MIN_PORT <= port <= MAX_PORT:
            errors.append(f"Choose a port between {MIN_PORT} and {MAX_PORT}.")

    name = _clean_name(device_name)
    if device_name.strip() and (not name or len(device_name.strip()) > MAX_NAME_LENGTH):
        errors.append(f"The device name must be 1-{MAX_NAME_LENGTH} characters.")

    if errors:
        return None, errors
    return {"token": cleaned, "port": port, "device_name": name}, []
