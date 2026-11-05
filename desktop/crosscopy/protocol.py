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

#: Hard cap for a single websocket frame, including JSON overhead.
MAX_FRAME_BYTES = 256 * 1024

#: Supported ``data["kind"]`` values. "files" arrives in Phase 2.
CONTENT_TEXT = "text"
SUPPORTED_CONTENT_KINDS = frozenset({CONTENT_TEXT})


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


def make_snapshot(origin: str, text: str) -> Message:
    message = make_text_update(origin, text)
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
