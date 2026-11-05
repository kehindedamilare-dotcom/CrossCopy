from __future__ import annotations

import json
import unittest

from crosscopy import protocol
from crosscopy.protocol import Message, MessageType, ProtocolError


class CanonicalTextTests(unittest.TestCase):
    def test_windows_and_unix_line_endings_share_a_digest(self) -> None:
        self.assertEqual(protocol.text_digest("one\ntwo"), protocol.text_digest("one\r\ntwo"))
        self.assertEqual(protocol.canonical_text("a\r\nb\rc"), "a\nb\nc")

    def test_digest_distinguishes_content(self) -> None:
        self.assertNotEqual(protocol.text_digest("hello"), protocol.text_digest("Hello"))

    def test_text_without_carriage_returns_is_untouched(self) -> None:
        text = "plain text"
        self.assertIs(protocol.canonical_text(text), text)


class FrameTests(unittest.TestCase):
    def test_round_trip_preserves_fields(self) -> None:
        message = protocol.make_text_update("desktop-1", "héllo 🌍")
        decoded = protocol.decode(message.to_json())
        self.assertEqual(decoded.type, MessageType.CLIPBOARD_UPDATE.value)
        self.assertEqual(decoded.origin, "desktop-1")
        self.assertEqual(protocol.extract_text(decoded), "héllo 🌍")
        self.assertEqual(decoded.id, message.id)

    def test_accepts_bytes(self) -> None:
        message = Message(type=MessageType.HELLO.value, data={"client_id": "abc"})
        self.assertEqual(protocol.decode(message.to_json().encode()).type, "hello")

    def test_rejects_wrong_protocol_version(self) -> None:
        raw = json.dumps({"v": 99, "type": "hello", "data": {}})
        with self.assertRaises(ProtocolError) as ctx:
            protocol.decode(raw)
        self.assertEqual(ctx.exception.code, "version_mismatch")

    def test_rejects_invalid_json_and_non_objects(self) -> None:
        with self.assertRaises(ProtocolError):
            protocol.decode("{nope")
        with self.assertRaises(ProtocolError):
            protocol.decode("[1, 2, 3]")

    def test_rejects_unknown_content_kind(self) -> None:
        message = Message(
            type=MessageType.CLIPBOARD_UPDATE.value,
            data={"kind": "files", "text": "x"},
        )
        with self.assertRaises(ProtocolError) as ctx:
            protocol.extract_text(message)
        self.assertEqual(ctx.exception.code, "unsupported_kind")

    def test_rejects_oversized_text(self) -> None:
        message = protocol.make_text_update("phone", "x" * 100)
        with self.assertRaises(ProtocolError) as ctx:
            protocol.extract_text(message, max_bytes=10)
        self.assertEqual(ctx.exception.code, "text_too_large")

    def test_rejects_oversized_frames(self) -> None:
        raw = json.dumps({"v": 1, "type": "hello", "data": {"padding": "x" * protocol.MAX_FRAME_BYTES}})
        with self.assertRaises(ProtocolError) as ctx:
            protocol.decode(raw)
        self.assertEqual(ctx.exception.code, "frame_too_large")

    def test_origin_can_be_restamped(self) -> None:
        message = protocol.make_text_update("spoofed", "text")
        restamped = message.with_origin("trusted")
        self.assertEqual(restamped.origin, "trusted")
        self.assertEqual(message.origin, "spoofed")


if __name__ == "__main__":
    unittest.main()
