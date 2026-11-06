"""Saved settings file and field validation (no registry, no network)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crosscopy import config, settings_store


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "config.json"
        patcher = mock.patch.dict(os.environ, {"CROSSCOPY_CONFIG": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_file_means_defaults(self) -> None:
        stored = settings_store.load()
        self.assertIsNone(stored.token)
        self.assertEqual(stored.port, settings_store.DEFAULT_PORT)
        self.assertEqual(stored.device_name, "")

    def test_save_then_load_round_trips(self) -> None:
        settings_store.save("s3cret-token", 8790, "Desk PC")
        stored = settings_store.load()
        self.assertEqual(stored.token, "s3cret-token")
        self.assertEqual(stored.port, 8790)
        self.assertEqual(stored.device_name, "Desk PC")
        self.assertTrue(self.path.exists())

    def test_save_strips_and_normalises(self) -> None:
        settings_store.save("  spaced  ", 8790, "  Desk PC  ")
        stored = settings_store.load()
        self.assertEqual(stored.token, "spaced")
        self.assertEqual(stored.device_name, "Desk PC")

    def test_broken_file_is_ignored(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(settings_store.load(), settings_store.StoredSettings())

    def test_out_of_range_values_fall_back(self) -> None:
        self.path.write_text(
            json.dumps({"token": "", "port": 99999, "device_name": "x" * 100}),
            encoding="utf-8",
        )
        stored = settings_store.load()
        self.assertIsNone(stored.token)
        self.assertEqual(stored.port, settings_store.DEFAULT_PORT)
        self.assertEqual(len(stored.device_name), settings_store.MAX_NAME_LENGTH)

    def test_saved_file_is_readable_json(self) -> None:
        settings_store.save("abc", 8765)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"token": "abc", "port": 8765, "device_name": ""})


class ValidateFieldsTests(unittest.TestCase):
    def test_accepts_reasonable_values(self) -> None:
        values, errors = settings_store.validate_fields("abc123", "8765", "Ghost")
        self.assertEqual(errors, [])
        self.assertEqual(values, {"token": "abc123", "port": 8765, "device_name": "Ghost"})

    def test_empty_token_is_allowed(self) -> None:
        values, errors = settings_store.validate_fields("   ", "8765", "")
        self.assertEqual(errors, [])
        self.assertIsNone(values["token"])

    def test_space_in_token_is_rejected(self) -> None:
        _, errors = settings_store.validate_fields("two words", "8765", "")
        self.assertEqual(len(errors), 1)
        self.assertIn("spaces", errors[0])

    def test_port_must_be_a_number_in_range(self) -> None:
        for port in ("", "http", "80", "70000"):
            _, errors = settings_store.validate_fields("abc", port, "")
            self.assertTrue(errors, port)
            self.assertIn("port", errors[0].lower())

    def test_device_name_length_is_capped(self) -> None:
        _, errors = settings_store.validate_fields("abc", "8765", "n" * 60)
        self.assertTrue(errors)
        self.assertIn("device name", errors[0].lower())


class PrecedenceTests(unittest.TestCase):
    """CLI flag > saved file > CROSSCOPY_TOKEN, so the window's value survives."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "config.json"
        patcher = mock.patch.dict(
            os.environ, {"CROSSCOPY_CONFIG": str(self.path), "CROSSCOPY_TOKEN": "from-env"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, *argv: str) -> config.Settings:
        return config.settings_from_args(config.build_parser().parse_args(list(argv)))

    def test_environment_is_used_when_nothing_is_saved(self) -> None:
        settings = self.resolve()
        self.assertEqual(settings.token, "from-env")
        self.assertEqual(settings.port, 8765)

    def test_saved_settings_beat_the_environment(self) -> None:
        settings_store.save("from-file", 8790, "Saved PC")
        settings = self.resolve()
        self.assertEqual(settings.token, "from-file")
        self.assertEqual(settings.port, 8790)
        self.assertEqual(settings.device_name, "Saved PC")

    def test_flags_beat_the_saved_settings(self) -> None:
        settings_store.save("from-file", 8790, "Saved PC")
        settings = self.resolve("--token", "from-flag", "--port", "8791", "--name", "Flag PC")
        self.assertEqual(settings.token, "from-flag")
        self.assertEqual(settings.port, 8791)
        self.assertEqual(settings.device_name, "Flag PC")

    def test_empty_flag_token_clears_the_token(self) -> None:
        settings_store.save("from-file", 8790)
        self.assertIsNone(self.resolve("--token", "").token)


if __name__ == "__main__":
    unittest.main()
