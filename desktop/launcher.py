"""PyInstaller entry script (see CrossCopy.spec)."""

from __future__ import annotations

from crosscopy.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
