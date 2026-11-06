"""Tray menu content and Windows autostart helpers (no registry writes)."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

from crosscopy import startup, tray


class FakeApp:
    def __init__(self) -> None:
        self.hub = SimpleNamespace(peers=lambda: self._peers)
        self._peers: list[SimpleNamespace] = []
        self.server = SimpleNamespace(port=8765)
        self.settings = SimpleNamespace(host="0.0.0.0", token="abc")
        self.stopped = False
        self.copied: str | None = None
        self.window_opened = False
        self.ui = SimpleNamespace(open=self._open_window)

    def _open_window(self) -> None:
        self.window_opened = True

    def pairing_url(self) -> str:
        return "ws://192.168.1.5:8765?token=abc"

    def copy_to_clipboard(self, text: str) -> bool:
        self.copied = text
        return True

    def request_stop(self) -> None:
        self.stopped = True


class TrayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FakeApp()
        self.tray = tray.TrayController(self.app)

    def test_status_reflects_the_number_of_phones(self) -> None:
        self.assertEqual(self.tray._status_label(), "Waiting for a phone")

        self.app._peers = [SimpleNamespace(name="Pixel 8")]
        self.assertEqual(self.tray._status_label(), "Connected to 1 phone")
        self.assertEqual(self.tray._detail_label(), "Pixel 8")

        self.app._peers = [SimpleNamespace(name="Pixel 8"), SimpleNamespace(name="Tablet")]
        self.assertEqual(self.tray._status_label(), "Connected to 2 phones")
        self.assertEqual(self.tray._detail_label(), "Pixel 8, Tablet")

    def test_detail_falls_back_to_the_pairing_link(self) -> None:
        self.assertEqual(self.tray._detail_label(), "ws://192.168.1.5:8765?token=abc")

    def test_pairing_link_is_long_enough_to_be_trimmed(self) -> None:
        self.app.pairing_url = lambda: "ws://192.168.100.155:8765?token=" + "x" * 40
        label = self.tray._detail_label()
        self.assertEqual(len(label), 60)
        self.assertTrue(label.endswith("..."))

    @unittest.skipUnless(tray.Menu is not None, "pystray is not installed")
    def test_menu_exposes_the_expected_actions(self) -> None:
        labels = [str(item) for item in self.tray._build_menu()]
        self.assertIn("Open CrossCopy", labels)
        self.assertIn("Copy pairing link", labels)
        self.assertIn("Open log", labels)
        self.assertIn("Start with Windows", labels)
        self.assertIn("Quit CrossCopy", labels)
        self.assertIn("Waiting for a phone", labels)

    def test_menu_opens_the_window(self) -> None:
        self.tray._open_window(None, None)
        self.assertTrue(self.app.window_opened)

    def test_quit_stops_the_app(self) -> None:
        self.tray._quit(None, None)
        self.assertTrue(self.app.stopped)

    def test_copy_action_uses_the_app_clipboard(self) -> None:
        self.tray._copy_link(None, None)
        self.assertEqual(self.app.copied, "ws://192.168.1.5:8765?token=abc")


class StartupTests(unittest.TestCase):
    def test_launch_command_quotes_both_paths(self) -> None:
        command = startup.launch_command()
        self.assertEqual(command.count('"'), 4)
        self.assertIn("launcher.py", command)
        if sys.platform == "win32":
            self.assertIn("python", command.lower())

    def test_unknown_entry_is_not_enabled(self) -> None:
        self.assertFalse(startup.is_enabled("CrossCopy-Definitely-Not-Installed"))
        self.assertIsNone(startup.current_command("CrossCopy-Definitely-Not-Installed"))

    def test_run_key_is_per_user(self) -> None:
        self.assertTrue(startup.RUN_KEY.startswith("Software\\Microsoft\\Windows\\CurrentVersion\\Run"))


if __name__ == "__main__":
    unittest.main()
