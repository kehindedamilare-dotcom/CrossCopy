"""Desktop runtime configuration (CLI flags + environment)."""

from __future__ import annotations

import argparse
import os
import socket
from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        if not self.device_name:
            object.__setattr__(self, "device_name", _default_device_name())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crosscopy",
        description="CrossCopy desktop: syncs your clipboard with your phone over Wi-Fi.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8765, help="TCP port to listen on (default: 8765)")
    parser.add_argument(
        "--token",
        default=os.environ.get("CROSSCOPY_TOKEN") or None,
        help="shared secret the phone must send (env: CROSSCOPY_TOKEN)",
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
    parser.add_argument("--name", default="", help="device name shown on the phone")
    parser.add_argument("--verbose", action="store_true", help="log every clipboard event")
    return parser


def from_args(argv: list[str] | None = None) -> Settings:
    args = build_parser().parse_args(argv)
    if args.port < 1 or args.port > 65535:
        raise SystemExit(f"invalid --port {args.port}")
    if args.poll_interval <= 0.05:
        raise SystemExit("--poll-interval must be greater than 0.05 seconds")
    return Settings(
        host=args.host,
        port=args.port,
        token=args.token,
        poll_interval=args.poll_interval,
        share_on_connect=not args.no_share_on_connect,
        device_name=args.name,
        log_level="DEBUG" if args.verbose else "INFO",
    )


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
