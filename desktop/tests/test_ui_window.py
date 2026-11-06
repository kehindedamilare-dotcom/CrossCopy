"""Smoke test for the settings window.

Opens a real Tk window, so it is opt-in: run with ``CROSSCOPY_UI_TEST=1``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crosscopy import settings_store, ui
from crosscopy.config import Settings


class FakeApp:
    """The slice of CrossCopyApp the window talks to."""

    def __init__(self) -> None:
        self.settings = Settings(host="0.0.0.0", port=8765, token="abc123", device_name="Ghost")
        self.applied: list[Settings] = []
        self.copied: list[str] = []
        self.peers: list[str] = []
        self.last_error: str | None = None

    def status(self) -> dict[str, object]:
        suffix = f"?token={self.settings.token}" if self.settings.token else ""
        return {
            "peers": list(self.peers),
            "url": f"ws://192.168.1.5:{self.settings.port}{suffix}",
            "port": self.settings.port,
            "token": self.settings.token,
            "device_name": self.settings.device_name,
            "error": self.last_error,
            "tray": True,
        }

    def apply_settings(self, settings: Settings) -> None:
        self.applied.append(settings)
        self.settings = settings

    def copy_to_clipboard(self, text: str) -> bool:
        self.copied.append(text)
        return True


@unittest.skipUnless(
    os.environ.get("CROSSCOPY_UI_TEST") == "1",
    "set CROSSCOPY_UI_TEST=1 to build a real window",
)
class WindowSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        if ui.tk is None:  # pragma: no cover - Python built without Tk
            self.skipTest("tkinter is unavailable")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"CROSSCOPY_CONFIG": str(Path(directory.name) / "config.json")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.app = FakeApp()
        self.root = ui.tk.Tk()
        self.addCleanup(self._destroy)
        self.window = ui.Window(self.app, self.root)
        self.window.build()
        self.root.update()

    def _destroy(self) -> None:
        try:
            self.window.close()
        except Exception:  # pragma: no cover
            pass
        try:
            self.root.destroy()
        except Exception:  # pragma: no cover
            pass

    def test_window_shows_status_and_pairing_link(self) -> None:
        self.assertEqual(self.root.title(), "CrossCopy")
        self.assertEqual(self.window.status_label.cget("text"), "Waiting for a phone")
        self.assertEqual(self.window.link_field.get(), "ws://192.168.1.5:8765?token=abc123")
        self.assertEqual(self.window.port_field.get(), "8765")
        self.assertEqual(self.window.name_field.get(), "Ghost")

    def test_window_follows_connected_phones(self) -> None:
        self.app.peers = ["Pixel 8"]
        self.window.refresh()
        self.root.update()
        self.assertEqual(self.window.status_label.cget("text"), "Connected to 1 phone")
        self.assertEqual(self.window.detail_label.cget("text"), "Pixel 8")

        self.app.peers = ["Pixel 8", "Tablet"]
        self.window.refresh()
        self.root.update()
        self.assertEqual(self.window.status_label.cget("text"), "Connected to 2 phones")

    def test_copy_reads_the_link_field(self) -> None:
        self.window.copy_link()
        self.assertEqual(self.app.copied, ["ws://192.168.1.5:8765?token=abc123"])
        self.assertIn("copied", self.window.message.cget("text"))

    def test_bad_port_is_refused_before_anything_is_saved(self) -> None:
        self.window.port_field.set("nope")
        self.window.save()
        self.assertEqual(self.app.applied, [])
        self.assertIn("port", str(self.window.message.cget("text")).lower())

    def test_saving_applies_and_persists(self) -> None:
        self.window.token_field.set("new-token")
        self.window.port_field.set("8790")
        self.window.name_field.set("Desk PC")
        self.window.save()

        self.assertEqual(len(self.app.applied), 1)
        applied = self.app.applied[0]
        self.assertEqual((applied.token, applied.port, applied.device_name), ("new-token", 8790, "Desk PC"))

        stored = settings_store.load()
        self.assertEqual((stored.token, stored.port, stored.device_name), ("new-token", 8790, "Desk PC"))
        self.assertIn("Saved", str(self.window.message.cget("text")))

    def test_show_toggles_the_token_masking(self) -> None:
        self.assertEqual(self.window.token_field.entry.cget("show"), "\u2022")
        self.window.toggle_token_visibility()
        self.assertEqual(self.window.token_field.entry.cget("show"), "")
        self.assertEqual(self.window.show_label.cget("text"), "Hide")


if __name__ == "__main__":
    unittest.main()
