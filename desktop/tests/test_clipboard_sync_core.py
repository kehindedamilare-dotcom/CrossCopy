"""Unit tests for echo cancellation (the heart of loop prevention)."""

from __future__ import annotations

import unittest

from crosscopy.clipboard import ClipboardError, ClipboardSyncCore


class FakeClipboard:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.writes: list[str] = []
        self.fail_writes = False

    def read_text(self) -> str | None:
        return self.value

    def write_text(self, text: str) -> None:
        if self.fail_writes:
            raise ClipboardError("clipboard locked")
        self.writes.append(text)
        self.value = text


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SyncCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clipboard = FakeClipboard("initial")
        self.clock = FakeClock()
        self.core = ClipboardSyncCore(
            self.clipboard, suppress_ttl=5.0, stale_ttl=1.5, clock=self.clock
        )
        self.core.prime()

    def test_primed_clipboard_is_not_broadcast(self) -> None:
        self.assertIsNone(self.core.observe_local("initial"))

    def test_genuine_local_change_is_returned_once(self) -> None:
        self.assertEqual(self.core.observe_local("copied on PC"), "copied on PC")
        self.assertIsNone(self.core.observe_local("copied on PC"))

    def test_remote_text_is_never_echoed_back(self) -> None:
        self.assertTrue(self.core.apply_remote("from the phone"))
        self.assertEqual(self.clipboard.value, "from the phone")
        # The watcher polls immediately afterwards: this must look like our own write.
        self.assertIsNone(self.core.observe_local("from the phone"))

    def test_crlf_echo_is_suppressed(self) -> None:
        self.assertTrue(self.core.apply_remote("line one\nline two"))
        self.assertIsNone(self.core.observe_local("line one\r\nline two"))

    def test_stale_read_from_before_the_remote_write_is_suppressed(self) -> None:
        # The watcher read the old value, the remote write landed, then the read arrived.
        self.assertTrue(self.core.apply_remote("new value"))
        self.assertIsNone(self.core.observe_local("initial"))

    def test_reapplying_identical_remote_text_is_a_noop(self) -> None:
        self.assertTrue(self.core.apply_remote("same"))
        self.assertFalse(self.core.apply_remote("same"))
        self.assertEqual(len(self.clipboard.writes), 1)

    def test_same_text_can_be_copied_again_after_the_window(self) -> None:
        self.core.apply_remote("shared text")
        self.clock.advance(6.0)
        # Something else is copied, then the same string is copied again later on.
        self.assertEqual(self.core.observe_local("intermediate"), "intermediate")
        self.assertEqual(self.core.observe_local("shared text"), "shared text")

    def test_text_equal_to_current_clipboard_is_not_rebroadcast(self) -> None:
        self.core.observe_local("repeat me")
        self.assertIsNone(self.core.observe_local("repeat me"))

    def test_oversized_local_text_is_ignored(self) -> None:
        core = ClipboardSyncCore(FakeClipboard(), max_text_bytes=8, clock=self.clock)
        self.assertIsNone(core.observe_local("0123456789"))

    def test_failed_write_rolls_back_state(self) -> None:
        self.clipboard.fail_writes = True
        with self.assertRaises(ClipboardError):
            self.core.apply_remote("cannot apply")

        # Clipboard still holds the old value, so it must not be treated as new.
        self.clipboard.fail_writes = False
        self.assertIsNone(self.core.observe_local("initial"))
        self.assertEqual(self.core.observe_local("local copy"), "local copy")

    def test_empty_text_is_ignored(self) -> None:
        self.assertIsNone(self.core.observe_local(""))

    def test_snapshot_does_not_mutate_state(self) -> None:
        self.assertEqual(self.core.snapshot(), "initial")
        self.assertEqual(self.core.observe_local("fresh"), "fresh")


if __name__ == "__main__":
    unittest.main()
