"""Clipboard access and echo cancellation for the desktop side.

The OS clipboard is polled from a dedicated thread instead of being watched
with Win32 events. That keeps the code portable, avoids a message loop on the
asyncio thread, and costs one cheap ``GetClipboardSequenceNumber`` call every
``poll_interval`` seconds (the clipboard is only re-read when that number
changes).

A clipboard item is either text or a PNG image; the same suppression machinery
covers both, keyed by content digest.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol, runtime_checkable

from . import imageclip
from .protocol import (
    CONTENT_IMAGE,
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    Clip,
    canonical_text,
    image_clip,
    text_clip,
    text_digest,
    text_size,
)

logger = logging.getLogger("crosscopy.clipboard")


class ClipboardError(RuntimeError):
    """The OS clipboard could not be read or written."""


@runtime_checkable
class ClipboardBackend(Protocol):
    """Minimal clipboard surface.

    Phase 2 can add ``read_files``/``write_files`` here without touching the
    sync core or the networking layer.
    """

    def read_item(self) -> Clip | None: ...

    def write_item(self, clip: Clip) -> None: ...


class TextClipboard:
    """Adapt a text-only backend (tests, simple hosts) to the item interface."""

    def __init__(self, backend) -> None:
        self.backend = backend

    def read_item(self) -> Clip | None:
        text = self.backend.read_text()
        return text_clip(text) if text else None

    def write_item(self, clip: Clip) -> None:
        if not clip.is_text:
            raise ClipboardError("this backend only supports text")
        self.backend.write_text(clip.text or "")


class SystemClipboard:
    """The real Windows clipboard: text through pyperclip, images through Win32.

    ``GetClipboardSequenceNumber`` is used as a cheap "did anything change?"
    check, because decoding an image every 350 ms would be wasteful.
    """

    def __init__(self, retries: int = 3, retry_delay: float = 0.05, *, images: bool = True) -> None:
        import pyperclip  # imported lazily so tests can run without it

        self._clipboard = pyperclip
        self._retries = retries
        self._retry_delay = retry_delay
        self._lock = threading.Lock()
        self._use_images = images and imageclip.supported()
        self._sequence: int | None = None
        self._cached: Clip | None = None
        if not self._use_images:
            logger.info("image clipboard support is off (Windows + Pillow required)")

    # -- helpers -----------------------------------------------------------

    def _attempt(self, operation: Callable[[], object]) -> object:
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

    def _read_text(self) -> str | None:
        text = self._attempt(self._clipboard.paste)
        # pyperclip returns "" for non-text formats (images, files, ...).
        if not isinstance(text, str) or text == "":
            return None
        return text

    def _write_text(self, text: str) -> None:
        self._attempt(lambda: (self._clipboard.copy(text), None)[1])

    def _invalidate(self) -> None:
        self._sequence = None
        self._cached = None

    # -- ClipboardBackend --------------------------------------------------

    def read_item(self) -> Clip | None:
        sequence = imageclip.sequence_number() if self._use_images else None
        if sequence is not None and sequence == self._sequence:
            return self._cached

        item: Clip | None = None
        if self._use_images:
            png = imageclip.read_png()
            if png:
                width, height = imageclip.image_size(png)
                item = image_clip(png, width, height)

        if item is None:
            text = self._read_text()
            item = text_clip(text) if text else None

        if sequence is not None:
            self._sequence = sequence
            self._cached = item
        return item

    def write_item(self, clip: Clip) -> None:
        try:
            if clip.kind == CONTENT_IMAGE:
                if not self._use_images:
                    raise ClipboardError("image clipboard support is not available")
                png = clip.png or b""
                if len(png) > MAX_IMAGE_BYTES:
                    raise ClipboardError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
                imageclip.write_png(png)
            else:
                self._write_text(canonical_text(clip.text or ""))
        finally:
            self._invalidate()


class ClipboardSyncCore:
    """Decides what is a real local copy and what is our own echo.

    Loop prevention uses two independent guards:

    1. ``_last_observed`` is the digest of the item the clipboard is believed
       to hold. A poll that returns it again is not a change at all.
    2. ``_suppressed`` maps content digests to an expiry. When remote content is
       written to the clipboard the digest is recorded *before* the write, so a
       poll that happens between "write" and "state update" is still dropped.

    Suppression entries expire so that intentionally copying the same content
    again a few seconds later propagates normally. The previous clipboard value
    is also briefly suppressed, which swallows reads that were already in flight
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
        max_image_bytes: int = MAX_IMAGE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend if hasattr(backend, "read_item") else TextClipboard(backend)
        self._suppress_ttl = suppress_ttl
        self._stale_ttl = stale_ttl
        self._max_text_bytes = max_text_bytes
        self._max_image_bytes = max_image_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._last_observed: str | None = None
        self._suppressed: dict[str, float] = {}

    # -- state helpers -----------------------------------------------------

    def _prune(self, now: float) -> None:
        expired = [digest for digest, expiry in self._suppressed.items() if expiry <= now]
        for digest in expired:
            del self._suppressed[digest]

    def _suppress(self, digest: str, now: float, ttl: float) -> None:
        if len(self._suppressed) >= self._MAX_SUPPRESSED_ENTRIES:
            oldest = min(self._suppressed, key=self._suppressed.__getitem__)
            del self._suppressed[oldest]
        self._suppressed[digest] = now + ttl

    def _too_large(self, item: Clip) -> bool:
        if item.is_image:
            return item.size > self._max_image_bytes
        return item.size > self._max_text_bytes

    # -- public API --------------------------------------------------------

    def prime(self) -> None:
        """Adopt the current clipboard as already-known state.

        Called once at startup so restarting the desktop app does not re-broadcast
        whatever happened to be on the clipboard.
        """
        try:
            item = self.backend.read_item()
        except ClipboardError as exc:
            logger.debug("could not prime clipboard: %s", exc)
            return
        with self._lock:
            self._last_observed = item.digest if item is not None else None

    def observe_item(self, item: Clip | None) -> Clip | None:
        """Return ``item`` if it is a genuine local change, else ``None``."""
        if item is None:
            return None
        if item.is_text and not (item.text or ""):
            return None
        if self._too_large(item):
            logger.warning("%s clipboard item ignored: larger than the size limit", item.kind)
            return None

        digest = item.digest
        now = self._clock()

        with self._lock:
            self._prune(now)

            if digest == self._last_observed:
                return None

            suppressed_until = self._suppressed.pop(digest, None)
            if suppressed_until is not None and suppressed_until > now:
                # This is the clipboard read of a remote write, not a user action.
                self._last_observed = digest
                return None

            self._last_observed = digest
            return item

    def apply_item(self, item: Clip) -> bool:
        """Write remote content locally without ever echoing it back.

        Returns ``True`` when the local clipboard actually changed.
        """
        if item.is_text:
            item = text_clip(item.text or "")
        if self._too_large(item):
            raise ValueError(f"remote {item.kind} exceeds the size limit")

        digest = item.digest
        with self._lock:
            now = self._clock()
            self._prune(now)

            if digest == self._last_observed:
                return False

            previous = self._last_observed
            self._suppress(digest, now, self._suppress_ttl)
            if previous is not None:
                self._suppress(previous, now, self._stale_ttl)
            self._last_observed = digest

        try:
            self.backend.write_item(item)
        except Exception:
            # Roll back so the next poll is treated as a normal local change.
            with self._lock:
                self._last_observed = previous
            raise

        return True

    # -- text shims (kept for callers that only deal in text) --------------

    def observe_local(self, text: str) -> str | None:
        item = self.observe_item(text_clip(text) if text else None)
        return item.text if item is not None else None

    def apply_remote(self, text: str) -> bool:
        return self.apply_item(text_clip(text))

    def snapshot_item(self) -> Clip | None:
        """Read the clipboard for a one-off share (does not affect state)."""
        try:
            return self.backend.read_item()
        except ClipboardError as exc:
            logger.debug("could not read clipboard for snapshot: %s", exc)
            return None

    def snapshot(self) -> str | None:
        item = self.snapshot_item()
        return item.text if item is not None and item.is_text else None


class ClipboardWatcher:
    """Polls the clipboard on a background thread.

    ``on_change`` runs on the watcher thread; callers must hand the value over
    to their event loop (the app uses ``loop.call_soon_threadsafe``).
    """

    def __init__(
        self,
        core: ClipboardSyncCore,
        on_change: Callable[[Clip], None],
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
                changed = self._core.observe_item(self._core.backend.read_item())
                if changed is not None:
                    self._on_change(changed)
            except ClipboardError as exc:
                # Very common when another app owns the clipboard mid-copy.
                logger.debug("clipboard poll skipped: %s", exc)
            except Exception:  # never let the watcher die
                logger.exception("unexpected error while polling the clipboard")
            self._stop.wait(self._poll_interval)
