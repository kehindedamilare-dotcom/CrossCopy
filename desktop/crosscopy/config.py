"""Desktop runtime configuration (CLI flags + saved settings + environment)."""

from __future__ import annotations

import argparse
import logging
import os
import socket
from dataclasses import dataclass

from . import settings_store

logger = logging.getLogger("crosscopy.config")


def _default_device_name() -> str:
    try:
        return socket.gethostname() or "Windows PC"
    except OSError:
        return "Windows PC"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8765
    token: str | None = None
    poll_interval: float = 0.35
    share_on_connect: bool = True
    device_name: str = ""
    log_level: str = "INFO"
    tray: bool = True
    show_window: bool = False

    def __post_init__(self) -> None:
        if not self.device_name:
            object.__setattr__(self, "device_name", _default_device_name())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crosscopy",
        description="CrossCopy desktop: syncs your clipboard with your phone over Wi-Fi.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all interfaces)")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port to listen on (default: the saved value, else 8765)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "shared secret the phone must send "
            "(default: the saved value, else env CROSSCOPY_TOKEN)"
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.35,
        help="seconds between clipboard polls (default: 0.35)",
    )
    parser.add_argument(
        "--no-share-on-connect",
        action="store_true",
        help="do not push the current desktop clipboard to a phone that just connected",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="device name shown on the phone (default: the saved value, else the PC name)",
    )
    parser.add_argument("--verbose", action="store_true", help="log every clipboard event")
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="run without the system tray icon (headless mode)",
    )
    parser.add_argument(
        "--show-window",
        action="store_true",
        help="open the CrossCopy window as soon as the server is listening",
    )
    parser.add_argument(
        "--hide-console",
        action="store_true",
        help="log only to the file (no console output); implied by a windowed build",
    )
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="start CrossCopy with Windows, then exit",
    )
    parser.add_argument(
        "--uninstall-startup",
        action="store_true",
        help="stop starting CrossCopy with Windows, then exit",
    )
    parser.add_argument(
        "--startup-status",
        action="store_true",
        help="print whether CrossCopy starts with Windows, then exit",
    )
    return parser


def resolve(args: argparse.Namespace) -> Settings:
    """Merge CLI flags, the saved settings file and the environment variable."""
    stored = settings_store.load()

    port = args.port if args.port is not None else stored.port
    if port < 1 or port > 65535:
        raise SystemExit(f"invalid --port {port}")

    if args.token is not None:
        token = args.token.strip() or None
    elif stored.token:
        token = stored.token
    else:
        token = (os.environ.get("CROSSCOPY_TOKEN") or "").strip() or None
        if token:
            logger.info("using the CROSSCOPY_TOKEN environment variable")

    return Settings(
        host=args.host,
        port=port,
        token=token,
        poll_interval=args.poll_interval,
        share_on_connect=not args.no_share_on_connect,
        device_name=(args.name if args.name is not None else stored.device_name),
        log_level="DEBUG" if args.verbose else "INFO",
        tray=not args.no_tray,
        show_window=bool(getattr(args, "show_window", False)),
    )


def settings_from_args(args: argparse.Namespace) -> Settings:
    if args.poll_interval <= 0.05:
        raise SystemExit("--poll-interval must be greater than 0.05 seconds")
    return resolve(args)


def from_args(argv: list[str] | None = None) -> Settings:
    return settings_from_args(build_parser().parse_args(argv))


def lan_addresses() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses."""
    addresses: set[str] = set()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass

    # The UDP connect trick reveals the interface used for the default route.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    usable = [addr for addr in addresses if not addr.startswith("127.") and addr != "0.0.0.0"]
    return sorted(usable)
