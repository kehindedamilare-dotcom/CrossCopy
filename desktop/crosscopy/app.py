"""Wires the clipboard watcher, the sync core and the websocket server together."""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid

from . import protocol
from .clipboard import ClipboardBackend, ClipboardSyncCore, ClipboardWatcher, PyperclipClipboard
from .config import Settings
from .hub import Hub, Peer
from .protocol import MAX_TEXT_BYTES
from .server import SyncServer

logger = logging.getLogger("crosscopy.app")


class CrossCopyApp:
    def __init__(self, settings: Settings, *, backend: ClipboardBackend | None = None) -> None:
        self.settings = settings
        self.hub = Hub(server_id=f"desktop-{uuid.uuid4().hex[:8]}")
        self.backend = backend if backend is not None else PyperclipClipboard()
        self.core = ClipboardSyncCore(self.backend, max_text_bytes=MAX_TEXT_BYTES)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._watcher = ClipboardWatcher(
            self.core,
            self._enqueue_from_watcher,
            poll_interval=settings.poll_interval,
        )
        self.server = SyncServer(
            settings,
            self.hub,
            on_remote_text=self._apply_remote_text,
            read_local_clipboard=self._read_local_text,
        )

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()

        # Remember what is already on the clipboard instead of re-broadcasting it.
        await asyncio.to_thread(self.core.prime)

        pump = asyncio.create_task(self._pump_outbox(), name="outbox")
        self._watcher.start()
        logger.info("watching the clipboard every %.2fs", self.settings.poll_interval)
        try:
            await self.server.serve_until_stopped()
        finally:
            self._watcher.stop()
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass

    # -- clipboard -> phone ------------------------------------------------

    def _enqueue_from_watcher(self, text: str) -> None:
        """Called on the watcher thread; hand the value to the event loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._outbox.put_nowait, text)
        except RuntimeError:
            pass  # loop shut down between the check and the call

    async def _pump_outbox(self) -> None:
        while True:
            text = await self._outbox.get()
            message = protocol.make_text_update(self.hub.id, text)
            delivered = await self.hub.broadcast(message)
            if delivered:
                logger.info(
                    "sent clipboard (%d chars) to %d device(s)", protocol.text_size(text), delivered
                )
            else:
                logger.debug("clipboard changed while no phone was connected")

    # -- phone -> clipboard ------------------------------------------------

    async def _apply_remote_text(self, peer: Peer, text: str) -> bool:
        changed = await asyncio.to_thread(self.core.apply_remote, text)
        if changed:
            logger.info("applied clipboard from %s (%d chars)", peer.name, protocol.text_size(text))
        return changed

    async def _read_local_text(self) -> str | None:
        return await asyncio.to_thread(self.core.snapshot)

    # -- shutdown ----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def request_stop(*_: object) -> None:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self.server.stop)

        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, request_stop)
            except (ValueError, OSError):
                logger.debug("could not install handler for %s", name)
