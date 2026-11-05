"""Clipboard access and echo cancellation for the desktop side.

The OS clipboard is polled from a dedicated thread instead of being watched
with Win32 events. That keeps the code portable, avoids a message loop on the
asyncio thread, and costs one cheap ``OpenClipboard`` call every
``poll_interval`` seconds.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol, runtime_checkable

from .protocol import MAX_TEXT_BYTES, canonical_text, text_digest, text_size

logger = logging.getLogger("crosscopy.clipboard")


class ClipboardError(RuntimeError):
    """The OS clipboard could not be read or written."""


@runtime_checkable
class ClipboardBackend(Protocol):
    """Minimal clipboard surface.

    Phase 2 can add ``read_files``/``write_files`` here without touching the
    sync core or the networking layer.
    """

    def read_text(self) -> str | None: ...

    def write_text(self, text: str) -> None: ...


class PyperclipClipboard:
    """Windows clipboard through ``pyperclip``.

    The clipboard is a shared OS resource: another process can hold it open for
    a few milliseconds, so every operation retries briefly instead of failing
    the whole sync.
    """

    def __init__(self, retries: int = 3, retry_delay: float = 0.05) -> None:
        import pyperclip  # imported lazily so tests can run without it

        self._clipboard = pyperclip
        self._retries = retries
        self._retry_delay = retry_delay
        self._lock = threading.Lock()

    def _attempt(self, operation: Callable[[], str | None]) -> str | None:
        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                with self._lock:
                    return operation()
            except Exception as exc:  # pyperclip raises several unrelated types
                last_error = exc
                if attempt + 1 < self._retries:
                    time.sleep(self._retry_delay * (attempt + 1))
        raise ClipboardError(f"clipboard access failed: {last_error}") from last_error

    def read_text(self) -> str | None:
        text = self._attempt(self._clipboard.paste)
        # pyperclip returns "" for non-text formats (images, files, ...).
        if not isinstance(text, str) or text == "":
            return None
        return text

    def write_text(self, text: str) -> None:
        self._attempt(lambda: (self._clipboard.copy(text), None)[1])


class ClipboardSyncCore:
    """Decides what is a real local copy and what is our own echo.

    Loop prevention uses two independent guards:

    1. ``_last_observed`` is the canonical text the clipboard is believed to
       hold. A poll that returns it again is not a change at all.
    2. ``_suppressed`` maps content digests to an expiry. When remote text is
       written to the clipboard the digest is recorded *before* the write, so a
       poll that happens between "write" and "state update" is still dropped.

    Suppression entries expire so that intentionally copying the same text again
    a few seconds later propagates normally. The previous clipboard value is
    also briefly suppressed, which swallows reads that were already in flight
    when a remote update landed.
    """

    _MAX_SUPPRESSED_ENTRIES = 64

    def __init__(
        self,
        backend: ClipboardBackend,
        *,
        suppress_ttl: float = 5.0,
        stale_ttl: float = 1.5,
        max_text_bytes: int = MAX_TEXT_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self._suppress_ttl = suppress_ttl
        self._stale_ttl = stale_ttl
        self._max_text_bytes = max_text_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._last_observed: str | None = None
        self._suppressed: dict[str, float] = {}

    # -- state helpers -----------------------------------------------------

    def _prune(self, now: float) -> None:
        expired = [digest for digest, expiry in self._suppressed.items() if expiry <= now]
        for digest in expired:
            del self._suppressed[digest]

    def _suppress(self, text: str, now: float, ttl: float) -> None:
        if len(self._suppressed) >= self._MAX_SUPPRESSED_ENTRIES:
            oldest = min(self._suppressed, key=self._suppressed.__getitem__)
            del self._suppressed[oldest]
        self._suppressed[text_digest(text)] = now + ttl

    # -- public API --------------------------------------------------------

    def prime(self) -> None:
        """Adopt the current clipboard as already-known state.

        Called once at startup so restarting the desktop app does not re-broadcast
        whatever happened to be on the clipboard.
        """
        try:
            text = self.backend.read_text()
        except ClipboardError as exc:
            logger.debug("could not prime clipboard: %s", exc)
            return
        with self._lock:
            self._last_observed = canonical_text(text) if text is not None else None

    def observe_local(self, text: str) -> str | None:
        """Return ``text`` if it is a genuine local change, else ``None``."""
        if not text:
            return None

        canonical = canonical_text(text)
        if text_size(canonical) > self._max_text_bytes:
            logger.warning("local clipboard ignored: larger than %d bytes", self._max_text_bytes)
            return None

        digest = text_digest(canonical)
        now = self._clock()

        with self._lock:
            self._prune(now)

            if canonical == self._last_observed:
                return None

            suppressed_until = self._suppressed.pop(digest, None)
            if suppressed_until is not None and suppressed_until > now:
                # This is the clipboard read of a remote write, not a user action.
                self._last_observed = canonical
                return None

            self._last_observed = canonical
            return text

    def apply_remote(self, text: str) -> bool:
        """Write remote text locally without ever echoing it back.

        Returns ``True`` when the local clipboard actually changed.
        """
        canonical = canonical_text(text)
        if text_size(canonical) > self._max_text_bytes:
            raise ValueError(f"remote text exceeds {self._max_text_bytes} bytes")

        with self._lock:
            now = self._clock()
            self._prune(now)

            if canonical == self._last_observed:
                return False

            previous = self._last_observed
            self._suppress(canonical, now, self._suppress_ttl)
            if previous is not None:
                self._suppress(previous, now, self._stale_ttl)
            self._last_observed = canonical

        try:
            self.backend.write_text(canonical)
        except Exception:
            # Roll back so the next poll is treated as a normal local change.
            with self._lock:
                self._last_observed = previous
            raise

        return True

    def snapshot(self) -> str | None:
        """Read the clipboard for a one-off share (does not affect state)."""
        try:
            return self.backend.read_text()
        except ClipboardError as exc:
            logger.debug("could not read clipboard for snapshot: %s", exc)
            return None


class ClipboardWatcher:
    """Polls the clipboard on a background thread.

    ``on_change`` runs on the watcher thread; callers must hand the value over
    to their event loop (the app uses ``loop.call_soon_threadsafe``).
    """

    def __init__(
        self,
        core: ClipboardSyncCore,
        on_change: Callable[[str], None],
        *,
        poll_interval: float = 0.35,
    ) -> None:
        self._core = core
        self._on_change = on_change
        self._poll_interval = max(0.05, poll_interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="clipboard-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._core.backend.read_text()
                if text:
                    changed = self._core.observe_local(text)
                    if changed is not None:
                        self._on_change(changed)
            except ClipboardError as exc:
                # Very common when another app owns the clipboard mid-copy.
                logger.debug("clipboard poll skipped: %s", exc)
            except Exception:  # never let the watcher die
                logger.exception("unexpected error while polling the clipboard")
            self._stop.wait(self._poll_interval)
