"""WebSocket server: handshake, authentication, routing, replay protection."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Headers, Request, Response

from . import protocol
from .config import Settings, lan_addresses
from .hub import Hub, Peer
from .protocol import MAX_FRAME_BYTES, Clip, Message, MessageType, ProtocolError

logger = logging.getLogger("crosscopy.server")

HELLO_TIMEOUT = 10.0
_PEER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

OnRemoteClip = Callable[[Peer, Clip], Awaitable[bool]]
ReadLocalClipboard = Callable[[], Awaitable[Clip | None]]


class SyncServer:
    def __init__(
        self,
        settings: Settings,
        hub: Hub,
        *,
        on_remote_clip: OnRemoteClip,
        read_local_clipboard: ReadLocalClipboard,
    ) -> None:
        self._settings = settings
        self._hub = hub
        self._on_remote_clip = on_remote_clip
        self._read_local_clipboard = read_local_clipboard
        self._stop = asyncio.Event()
        #: Set once the listening socket is bound (handy for tests and embedding).
        self.started = asyncio.Event()
        #: Actual bound port; differs from settings.port when that is 0.
        self.port: int = settings.port

    # -- authentication ----------------------------------------------------

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        path = urlsplit(request.path)
        if path.path not in ("/", ""):
            return _plain_response(404, "Not Found", "unknown endpoint\n")

        expected = self._settings.token
        if expected:
            supplied = parse_qs(path.query).get("token", [""])[0]
            if not secrets.compare_digest(supplied, expected):
                logger.warning("rejected connection from %s: bad token", _remote(connection))
                return _plain_response(401, "Unauthorized", "invalid or missing token\n")

        return None

    # -- lifecycle ---------------------------------------------------------

    async def serve_until_stopped(self) -> None:
        async with serve(
            self._handle_connection,
            self._settings.host,
            self._settings.port,
            process_request=self._process_request,
            max_size=MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as server:
            socket = server.sockets[0]
            self.port = socket.getsockname()[1]
            self._log_banner(self.port)
            self.started.set()
            try:
                await self._stop.wait()
            finally:
                server.close(close_connections=True)
                await server.wait_closed()
                logger.info("server stopped")

    def stop(self) -> None:
        self._stop.set()

    # -- connection handling ----------------------------------------------

    async def _handle_connection(self, connection: ServerConnection) -> None:
        peer: Peer | None = None
        try:
            peer = await self._handshake(connection)
            async for raw in connection:
                await self._dispatch(peer, raw)
        except ConnectionClosed as exc:
            logger.debug("connection from %s closed: %s", _remote(connection), exc)
        except ProtocolError as exc:
            logger.info("protocol error from %s: %s", _remote(connection), exc)
            await _safe_close(connection, 1008, f"{exc.code}: {exc.detail}")
        except Exception:
            logger.exception("unexpected error handling %s", _remote(connection))
            await _safe_close(connection, 1011, "internal server error")
        finally:
            if peer is not None:
                self._hub.remove(peer)
                # A newer socket with the same device id may already have replaced us.
                if self._hub.get(peer.id) is None:
                    logger.info("phone disconnected: %s (%s)", peer.name, peer.id)
                    await self._hub.broadcast(
                        Message(
                            type=MessageType.PEER_LEFT.value,
                            origin=self._hub.id,
                            data={"peer": peer.to_dict()},
                        ),
                        exclude=(peer.id,),
                    )

    async def _handshake(self, connection: ServerConnection) -> Peer:
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=HELLO_TIMEOUT)
        except TimeoutError as exc:
            raise ProtocolError("hello_timeout", "no hello frame received") from exc

        message = protocol.decode(raw)
        if message.type != MessageType.HELLO.value:
            raise ProtocolError("expected_hello", f"first frame was {message.type!r}")

        client_id = message.data.get("client_id")
        if not isinstance(client_id, str) or not _PEER_ID_RE.match(client_id):
            raise ProtocolError("bad_client_id", "client_id must match [A-Za-z0-9_-]{1,64}")

        name = _clean_label(message.data.get("name")) or "Android phone"
        platform_name = _clean_label(message.data.get("platform")) or "android"

        peer = Peer(
            id=client_id,
            name=name,
            platform=platform_name,
            connection=connection,
        )
        replaced = await self._hub.add(peer)
        if replaced is not None:
            logger.info("peer %s reconnected, closing its previous socket", client_id)
            await _safe_close(replaced.connection, 4000, "replaced by a newer connection")

        await peer.send(
            Message(
                type=MessageType.WELCOME.value,
                origin=self._hub.id,
                data={
                    "server_id": self._hub.id,
                    "name": self._settings.device_name,
                    "platform": "windows",
                    "protocol": protocol.PROTOCOL_VERSION,
                    "capabilities": sorted(protocol.SUPPORTED_CONTENT_KINDS),
                    "peers": self._hub.roster(),
                    "share_on_connect": self._settings.share_on_connect,
                },
            )
        )
        logger.info("phone connected: %s (%s) from %s", name, client_id, _remote(connection))

        await self._hub.broadcast(
            Message(
                type=MessageType.PEER_JOINED.value,
                origin=self._hub.id,
                data={"peer": peer.to_dict()},
            ),
            exclude=(peer.id,),
        )

        if self._settings.share_on_connect:
            clip = await self._read_local_clipboard()
            if clip is not None:
                await peer.send(protocol.make_clip_snapshot(self._hub.id, clip))
                logger.debug("sent current desktop clipboard (%s) to %s", clip.describe(), peer.name)

        return peer

    async def _dispatch(self, peer: Peer, raw: str | bytes) -> None:
        try:
            message = protocol.decode(raw)
        except ProtocolError as exc:
            logger.info("bad frame from %s: %s", peer.name, exc)
            await peer.send(protocol.make_error(self._hub.id, exc.code, exc.detail))
            return

        if not peer.seen(message.id):
            return  # replay / duplicate delivery

        try:
            if message.type == MessageType.CLIPBOARD_UPDATE.value:
                await self._handle_clipboard_update(peer, message)
            elif message.type == MessageType.CLIPBOARD_SHARE.value:
                await self._handle_share_request(peer, message)
            elif message.type == MessageType.PING.value:
                await peer.send(
                    Message(
                        type=MessageType.PONG.value,
                        origin=self._hub.id,
                        data={"n": message.data.get("n")},
                    )
                )
            elif message.type in (
                MessageType.ACK.value,
                MessageType.PONG.value,
                MessageType.ERROR.value,
            ):
                logger.debug("client %s sent %s", peer.name, message.type)
            else:
                raise ProtocolError("unsupported_type", f"type {message.type!r} is not handled")
        except ProtocolError as exc:
            logger.info("refused %s from %s: %s", message.type, peer.name, exc)
            await peer.send(protocol.make_error(self._hub.id, exc.code, exc.detail, message.id))

    async def _handle_clipboard_update(self, peer: Peer, message: Message) -> None:
        clip = protocol.extract_clip(message)
        # Never trust the origin supplied by a client; stamp the authenticated id.
        stamped = message.with_origin(peer.id)

        try:
            changed = await self._on_remote_clip(peer, clip)
        except Exception as exc:
            logger.warning("could not apply clipboard from %s: %s", peer.name, exc)
            await peer.send(
                protocol.make_error(self._hub.id, "apply_failed", str(exc), message.id)
            )
            changed = False

        delivered = await self._hub.broadcast(stamped, exclude=(peer.id,))
        logger.info(
            "clipboard from %s: %s -> %d other device(s)%s",
            peer.name,
            clip.describe(),
            delivered,
            "" if changed else " (local clipboard unchanged)",
        )
        await peer.send(protocol.make_ack(self._hub.id, stamped))

    async def _handle_share_request(self, peer: Peer, message: Message) -> None:
        clip = await self._read_local_clipboard()
        if clip is None:
            await peer.send(
                protocol.make_error(
                    self._hub.id, "empty_clipboard", "desktop clipboard is empty", message.id
                )
            )
            return
        await peer.send(protocol.make_clip_snapshot(self._hub.id, clip))
        logger.info("sent desktop clipboard (%s) to %s on request", clip.describe(), peer.name)

    # -- misc --------------------------------------------------------------

    def _log_banner(self, port: int) -> None:
        logger.info("CrossCopy desktop '%s' listening on %s:%d", self._settings.device_name, self._settings.host, port)
        if self._settings.host in ("0.0.0.0", ""):
            addresses = lan_addresses()
            if addresses:
                for address in addresses:
                    suffix = f"?token={self._settings.token}" if self._settings.token else ""
                    logger.info("  phone URL: ws://%s:%d%s", address, port, suffix)
            else:
                logger.warning("  no LAN IPv4 address found; is Wi-Fi connected?")
        else:
            suffix = f"?token={self._settings.token}" if self._settings.token else ""
            logger.info("  phone URL: ws://%s:%d%s", self._settings.host, port, suffix)
        if not self._settings.token:
            logger.warning("no --token set: any device on this Wi-Fi can talk to this server")
        if not self._settings.share_on_connect:
            logger.info("share-on-connect disabled (--no-share-on-connect)")


def _plain_response(status: int, reason: str, body: str) -> Response:
    return Response(
        status,
        reason,
        Headers(
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body.encode("utf-8")))),
                ("Connection", "close"),
            ]
        ),
        body.encode("utf-8"),
    )


async def _safe_close(connection: ServerConnection, code: int, reason: str) -> None:
    try:
        await connection.close(code, reason)
    except Exception:
        pass


def _remote(connection: ServerConnection) -> str:
    address = connection.remote_address
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return str(address)


def _clean_label(value: object, limit: int = 64) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char for char in value if char.isprintable()).strip()
    return cleaned[:limit]
