"""CrossCopy wire protocol (v1).

Every frame is one JSON object::

    {
      "v": 1,
      "type": "clipboard.update",
      "id": "6f1c0e...",           # unique per event, used to drop replays
      "origin": "desktop-1a2b3c4d",  # device that produced the event
      "ts": 1762345678.123,        # wall clock, display only
      "data": { ... }              # type specific payload
    }

Phase 2 (file sync) only needs a new content ``kind`` plus a handler for it.
The envelope, routing, replay protection and echo suppression stay untouched.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1

#: Largest clipboard text we are willing to relay (UTF-8 bytes, after decoding).
MAX_TEXT_BYTES = 64 * 1024

#: Largest clipboard image we relay (PNG bytes). 8 MiB covers 4K screenshots.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: Hard cap for a single websocket frame: base64 inflates images by ~4/3.
MAX_FRAME_BYTES = MAX_IMAGE_BYTES * 4 // 3 + 512 * 1024

#: Supported ``data["kind"]`` values. "files" arrives in Phase 2.
CONTENT_TEXT = "text"
CONTENT_IMAGE = "image"
SUPPORTED_CONTENT_KINDS = frozenset({CONTENT_TEXT, CONTENT_IMAGE})
CONTENT_MIME = {CONTENT_TEXT: "text/plain", CONTENT_IMAGE: "image/png"}


class MessageType(str, Enum):
    # handshake
    HELLO = "hello"
    WELCOME = "welcome"
    # clipboard traffic
    CLIPBOARD_UPDATE = "clipboard.update"
    CLIPBOARD_SNAPSHOT = "clipboard.snapshot"
    CLIPBOARD_SHARE = "clipboard.share"  # "send me your current clipboard"
    # housekeeping
    ACK = "ack"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"
    PEER_JOINED = "peer.joined"
    PEER_LEFT = "peer.left"


class ProtocolError(ValueError):
    """A peer sent something we cannot process."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def canonical_text(text: str) -> str:
    """Normalize line endings so Windows and Android agree on equality.

    pyperclip returns ``\\r\\n`` on Windows while Android uses ``\\n``. Without
    this normalization every remote write would look like a fresh local copy
    and echo straight back to the sender.
    """
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_digest(text: str) -> str:
    """Stable content fingerprint of the *canonical* form of ``text``."""
    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def text_size(text: str) -> int:
    return len(text.encode("utf-8"))


def new_id() -> str:
    return uuid.uuid4().hex


def image_digest(png: bytes) -> str:
    """Content fingerprint of PNG bytes.

    Both ends store and re-read the *same* PNG bytes, so the digest survives a
    clipboard round-trip; that is what lets echo suppression recognise our own
    write without decoding pixels.
    """
    return hashlib.sha256(png).hexdigest()


@dataclass(frozen=True, slots=True)
class Clip:
    """One clipboard item: either text or a PNG image."""

    kind: str
    text: str | None = None
    png: bytes | None = None
    width: int = 0
    height: int = 0

    @property
    def is_text(self) -> bool:
        return self.kind == CONTENT_TEXT

    @property
    def is_image(self) -> bool:
        return self.kind == CONTENT_IMAGE

    @property
    def digest(self) -> str:
        if self.kind == CONTENT_IMAGE:
            return image_digest(self.png or b"")
        return text_digest(self.text or "")

    @property
    def size(self) -> int:
        if self.kind == CONTENT_IMAGE:
            return len(self.png or b"")
        return text_size(self.text or "")

    def describe(self) -> str:
        if self.kind == CONTENT_IMAGE:
            kib = self.size / 1024
            size_text = f"{kib:.0f} KB" if kib >= 10 else f"{kib:.1f} KB"
            if self.width and self.height:
                return f"image {self.width}x{self.height} ({size_text})"
            return f"image ({size_text})"
        return f"text {self.size} chars"


def text_clip(text: str) -> Clip:
    return Clip(kind=CONTENT_TEXT, text=canonical_text(text))


def image_clip(png: bytes, width: int = 0, height: int = 0) -> Clip:
    return Clip(kind=CONTENT_IMAGE, png=png, width=width, height=height)


@dataclass(frozen=True, slots=True)
class Message:
    type: str
    id: str = field(default_factory=new_id)
    origin: str = ""
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "type": self.type,
                "id": self.id,
                "origin": self.origin,
                "ts": self.ts,
                "data": self.data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def with_origin(self, origin: str) -> Message:
        """Return a copy stamped with an authenticated device id.

        The server never trusts ``origin`` as received: a peer could otherwise
        impersonate the desktop and make others drop the update as an echo of
        their own event.
        """
        return Message(type=self.type, id=self.id, origin=origin, ts=self.ts, data=self.data)


def decode(raw: str | bytes) -> Message:
    """Parse and validate one inbound frame."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("bad_encoding", "frame is not valid UTF-8") from exc

    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large", f"frame exceeds {MAX_FRAME_BYTES} bytes")

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("bad_json", "frame is not valid JSON") from exc

    if not isinstance(document, dict):
        raise ProtocolError("bad_frame", "frame must be a JSON object")

    version = document.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            "version_mismatch",
            f"server speaks protocol v{PROTOCOL_VERSION}, client sent {version!r}",
        )

    message_type = document.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ProtocolError("bad_frame", "missing 'type'")

    message_id = document.get("id")
    origin = document.get("origin")
    ts = document.get("ts")
    data = document.get("data", {})

    if not isinstance(data, dict):
        raise ProtocolError("bad_frame", "'data' must be an object")

    return Message(
        type=message_type,
        id=message_id if isinstance(message_id, str) and message_id else new_id(),
        origin=origin if isinstance(origin, str) else "",
        ts=float(ts) if isinstance(ts, (int, float)) else time.time(),
        data=data,
    )


def extract_clip(
    message: Message,
    *,
    max_text_bytes: int = MAX_TEXT_BYTES,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> Clip:
    """Validate a ``clipboard.update`` payload and return the clipboard item."""
    kind = message.data.get("kind")
    if kind == CONTENT_TEXT:
        return text_clip(extract_text(message, max_text_bytes))

    if kind != CONTENT_IMAGE:
        raise ProtocolError("unsupported_kind", f"content kind {kind!r} is not supported")

    encoded = message.data.get("data")
    if not isinstance(encoded, str):
        raise ProtocolError("invalid_payload", "'data' must be a base64 string")

    try:
        png = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("invalid_payload", "image data is not valid base64") from exc

    if not png:
        raise ProtocolError("invalid_payload", "image data is empty")
    if len(png) > max_image_bytes:
        raise ProtocolError(
            "image_too_large", f"{len(png)} bytes exceeds the {max_image_bytes} byte limit"
        )

    claimed = message.data.get("digest")
    if isinstance(claimed, str) and claimed and claimed != image_digest(png):
        raise ProtocolError("digest_mismatch", "image bytes do not match their digest")

    width = message.data.get("width")
    height = message.data.get("height")
    return image_clip(
        png,
        width=width if isinstance(width, int) and width > 0 else 0,
        height=height if isinstance(height, int) and height > 0 else 0,
    )


def extract_text(message: Message, max_bytes: int = MAX_TEXT_BYTES) -> str:
    """Pull clipboard text out of a message or explain why we cannot."""
    kind = message.data.get("kind")
    if kind not in SUPPORTED_CONTENT_KINDS:
        raise ProtocolError("unsupported_kind", f"content kind {kind!r} is not supported")

    text = message.data.get("text")
    if not isinstance(text, str):
        raise ProtocolError("invalid_payload", "'text' must be a string")

    size = text_size(text)
    if size > max_bytes:
        raise ProtocolError("text_too_large", f"{size} bytes exceeds the {max_bytes} byte limit")

    return text


def make_text_update(origin: str, text: str) -> Message:
    return Message(
        type=MessageType.CLIPBOARD_UPDATE.value,
        origin=origin,
        data={
            "kind": CONTENT_TEXT,
            "text": text,
            "digest": text_digest(text),
            "bytes": text_size(text),
        },
    )


def make_image_update(origin: str, png: bytes, width: int = 0, height: int = 0) -> Message:
    if len(png) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    data: dict[str, Any] = {
        "kind": CONTENT_IMAGE,
        "mime": CONTENT_MIME[CONTENT_IMAGE],
        "data": base64.b64encode(png).decode("ascii"),
        "digest": image_digest(png),
        "bytes": len(png),
    }
    if width and height:
        data["width"] = width
        data["height"] = height
    return Message(type=MessageType.CLIPBOARD_UPDATE.value, origin=origin, data=data)


def make_clip_update(origin: str, clip: Clip) -> Message:
    if clip.is_image:
        return make_image_update(origin, clip.png or b"", clip.width, clip.height)
    return make_text_update(origin, clip.text or "")


def make_snapshot(origin: str, text: str) -> Message:
    return make_clip_snapshot(origin, text_clip(text))


def make_clip_snapshot(origin: str, clip: Clip) -> Message:
    message = make_clip_update(origin, clip)
    return Message(
        type=MessageType.CLIPBOARD_SNAPSHOT.value,
        id=message.id,
        origin=origin,
        ts=message.ts,
        data=message.data,
    )


def make_error(origin: str, code: str, detail: str, reply_to: str | None = None) -> Message:
    data: dict[str, Any] = {"code": code, "detail": detail}
    if reply_to:
        data["reply_to"] = reply_to
    return Message(type=MessageType.ERROR.value, origin=origin, data=data)


def make_ack(origin: str, message: Message) -> Message:
    return Message(
        type=MessageType.ACK.value,
        origin=origin,
        data={"reply_to": message.id, "status": "applied"},
    )
