"""Image clipboard support: protocol validation and echo cancellation."""

from __future__ import annotations

import base64
import unittest

from crosscopy import protocol
from crosscopy.clipboard import ClipboardError, ClipboardSyncCore
from crosscopy.protocol import Clip, ProtocolError, image_clip, text_clip

#: 1x1 red PNG — the protocol layer never decodes, it only hashes the bytes.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeItemClipboard:
    """Item-capable clipboard stand-in."""

    def __init__(self, item: Clip | None = None) -> None:
        self.item = item
        self.writes: list[Clip] = []
        self.fail_writes = False

    def read_item(self) -> Clip | None:
        return self.item

    def write_item(self, clip: Clip) -> None:
        if self.fail_writes:
            raise ClipboardError("clipboard locked")
        self.item = clip
        self.writes.append(clip)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ImageProtocolTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        message = protocol.make_image_update("desktop-1", PNG, 1, 1)
        decoded = protocol.decode(message.to_json())
        clip = protocol.extract_clip(decoded)
        self.assertTrue(clip.is_image)
        self.assertEqual(clip.png, PNG)
        self.assertEqual(clip.width, 1)
        self.assertEqual(clip.height, 1)
        self.assertEqual(clip.digest, protocol.image_digest(PNG))

    def test_frame_fits_inside_the_transport_limit(self) -> None:
        message = protocol.make_image_update("desktop-1", PNG)
        self.assertLess(len(message.to_json().encode()), protocol.MAX_FRAME_BYTES)

    def test_digest_mismatch_is_rejected(self) -> None:
        message = protocol.make_image_update("desktop-1", PNG)
        tampered = protocol.Message(
            type=message.type,
            origin=message.origin,
            data={**message.data, "digest": "0" * 64},
        )
        with self.assertRaises(ProtocolError) as ctx:
            protocol.extract_clip(tampered)
        self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_bad_base64_is_rejected(self) -> None:
        message = protocol.Message(
            type="clipboard.update",
            origin="phone",
            data={"kind": "image", "data": "not base64!!"},
        )
        with self.assertRaises(ProtocolError) as ctx:
            protocol.extract_clip(message)
        self.assertEqual(ctx.exception.code, "invalid_payload")

    def test_oversized_image_is_rejected(self) -> None:
        message = protocol.make_image_update("desktop-1", PNG)
        with self.assertRaises(ProtocolError) as ctx:
            protocol.extract_clip(message, max_image_bytes=10)
        self.assertEqual(ctx.exception.code, "image_too_large")

    def test_receiving_a_truncated_base64_payload_is_rejected(self) -> None:
        message = protocol.make_image_update("desktop-1", PNG)
        broken = protocol.Message(
            type=message.type,
            origin="phone",
            data={**message.data, "data": message.data["data"][:-8]},
        )
        with self.assertRaises(ProtocolError):
            protocol.extract_clip(broken)

    def test_text_updates_still_pass_through(self) -> None:
        decoded = protocol.decode(protocol.make_text_update("d", "hi").to_json())
        self.assertTrue(protocol.extract_clip(decoded).is_text)


class ImageEchoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clipboard = FakeItemClipboard(text_clip("initial"))
        self.clock = FakeClock()
        self.core = ClipboardSyncCore(
            self.clipboard, suppress_ttl=5.0, stale_ttl=1.5, clock=self.clock
        )
        self.core.prime()

    def test_remote_image_is_applied_once_and_never_echoed(self) -> None:
        self.assertTrue(self.core.apply_item(image_clip(PNG)))
        self.assertTrue(self.clipboard.item.is_image)
        self.assertIsNone(self.core.observe_item(self.clipboard.item))

    def test_reapplying_the_same_image_is_a_no_op(self) -> None:
        self.core.apply_item(image_clip(PNG))
        self.assertFalse(self.core.apply_item(image_clip(PNG)))

    def test_switching_between_text_and_images_propagates(self) -> None:
        self.core.apply_item(image_clip(PNG))
        self.assertEqual(self.core.observe_local("typed after the image"), "typed after the image")

        self.core.apply_remote("remote text")
        self.assertIsNone(self.core.observe_local("remote text"))

        image_two = image_clip(PNG + b"2")
        self.assertTrue(self.core.apply_item(image_two))
        self.assertIsNone(self.core.observe_item(image_two))

    def test_primed_image_is_not_broadcast(self) -> None:
        clipboard = FakeItemClipboard(image_clip(PNG))
        core = ClipboardSyncCore(clipboard, clock=self.clock)
        core.prime()
        self.assertIsNone(core.observe_item(clipboard.item))

    def test_same_image_copied_again_later_propagates(self) -> None:
        self.core.apply_item(image_clip(PNG))
        self.assertIsNone(self.core.observe_item(image_clip(PNG)))
        self.clock.advance(30)
        self.core.observe_local("something else")
        self.assertIsNotNone(self.core.observe_item(image_clip(PNG)))

    def test_failed_write_rolls_back(self) -> None:
        self.clipboard.fail_writes = True
        with self.assertRaises(ClipboardError):
            self.core.apply_item(image_clip(PNG))
        self.clipboard.fail_writes = False
        # The clipboard still holds the primed text, so it is not news.
        self.assertIsNone(self.core.observe_local("initial"))


if __name__ == "__main__":
    unittest.main()
