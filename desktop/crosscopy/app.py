"""Wires the clipboard watcher, the sync core and the websocket server together."""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid

from . import protocol
from .clipboard import ClipboardBackend, ClipboardSyncCore, ClipboardWatcher, SystemClipboard
from .config import Settings, lan_addresses
from .hub import Hub, Peer
from .protocol import MAX_TEXT_BYTES
from .server import SyncServer
from .tray import TrayController

logger = logging.getLogger("crosscopy.app")


class CrossCopyApp:
    def __init__(self, settings: Settings, *, backend: ClipboardBackend | None = None) -> None:
        self.settings = settings
        self.hub = Hub(server_id=f"desktop-{uuid.uuid4().hex[:8]}")
        self.backend = backend if backend is not None else SystemClipboard()
        self.core = ClipboardSyncCore(self.backend, max_text_bytes=MAX_TEXT_BYTES)
        self.tray = TrayController(self)

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
            on_remote_clip=self._apply_remote_clip,
            read_local_clipboard=self._read_local_clip,
        )

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()

        # Remember what is already on the clipboard instead of re-broadcasting it.
        await asyncio.to_thread(self.core.prime)

        pump = asyncio.create_task(self._pump_outbox(), name="outbox")
        tray_sync = asyncio.create_task(self._sync_tray(), name="tray-sync")
        self._watcher.start()
        logger.info("watching the clipboard every %.2fs", self.settings.poll_interval)
        try:
            await self.server.serve_until_stopped()
        finally:
            self._watcher.stop()
            pump.cancel()
            tray_sync.cancel()
            if self.settings.tray:
                self.tray.stop()
            for task in (pump, tray_sync):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # -- tray --------------------------------------------------------------

    def pairing_url(self) -> str:
        """The URL a phone should connect to, using this machine's LAN address."""
        host = self.settings.host
        if host in ("", "0.0.0.0", "::", "[::]"):
            addresses = lan_addresses()
            host = addresses[0] if addresses else "127.0.0.1"
        suffix = f"?token={self.settings.token}" if self.settings.token else ""
        return f"ws://{host}:{self.server.port}{suffix}"

    def copy_to_clipboard(self, text: str) -> bool:
        """Put text on the clipboard *without* broadcasting it to the phones."""
        try:
            self.core.apply_remote(text)
        except Exception:
            logger.exception("could not write to the clipboard")
            return False
        return True

    def request_stop(self) -> None:
        """Ask the server to stop; safe to call from any thread (tray, signal)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.server.stop)
        except RuntimeError:
            pass

    async def _sync_tray(self) -> None:
        """Start the tray once the port is known, then keep it in step."""
        if not self.settings.tray:
            return
        await self.server.started.wait()
        started = await asyncio.to_thread(self.tray.start)
        if started:
            logger.info("pairing link: %s", self.pairing_url())

        last: tuple[int, tuple[str, ...]] | None = None
        while True:
            try:
                peers = self.hub.peers()
                state = (len(peers), tuple(peer.name for peer in peers))
            except Exception:  # pragma: no cover - hub is always usable here
                state = (0, ())
            if state != last:
                joined = last is not None and state[0] > last[0]
                left = last is not None and state[0] < last[0]
                last = state
                if started:
                    await asyncio.to_thread(self.tray.refresh)
                    if joined and state[0] == 1:
                        await asyncio.to_thread(
                            self.tray.notify, f"{state[1][0]} connected"
                        )
                    elif left and state[0] == 0:
                        await asyncio.to_thread(self.tray.notify, "Phone disconnected")
            await asyncio.sleep(1.0)

    # -- clipboard -> phone ------------------------------------------------

    def _enqueue_from_watcher(self, clip: protocol.Clip) -> None:
        """Called on the watcher thread; hand the value to the event loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._outbox.put_nowait, clip)
        except RuntimeError:
            pass  # loop shut down between the check and the call

    async def _pump_outbox(self) -> None:
        while True:
            clip = await self._outbox.get()
            message = protocol.make_clip_update(self.hub.id, clip)
            delivered = await self.hub.broadcast(message)
            if delivered:
                logger.info("sent clipboard %s to %d device(s)", clip.describe(), delivered)
            else:
                logger.debug("clipboard changed while no phone was connected")

    # -- phone -> clipboard ------------------------------------------------

    async def _apply_remote_clip(self, peer: Peer, clip: protocol.Clip) -> bool:
        changed = await asyncio.to_thread(self.core.apply_item, clip)
        if changed:
            logger.info("applied clipboard from %s (%s)", peer.name, clip.describe())
        return changed

    async def _read_local_clip(self) -> protocol.Clip | None:
        return await asyncio.to_thread(self.core.snapshot_item)

    # -- shutdown ----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def request_stop(*_: object) -> None:
            self.request_stop()

        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, request_stop)
            except (ValueError, OSError):
                logger.debug("could not install handler for %s", name)
