"""``python -m crosscopy`` entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

from .app import CrossCopyApp
from .config import from_args

def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # The library logs a line per websocket frame at INFO; warnings are enough.
    logging.getLogger("websockets").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    settings = from_args(argv)
    configure_logging(settings.log_level)
    logger = logging.getLogger("crosscopy")

    try:
        asyncio.run(CrossCopyApp(settings).run())
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        logger.error("could not start the server: %s", exc)
        logger.error("is another CrossCopy instance already using port %d?", settings.port)
        return 1

    logger.info("goodbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
