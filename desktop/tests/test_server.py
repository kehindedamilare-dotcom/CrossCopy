"""End-to-end tests against a real websocket server and fake OS clipboard."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import time
import unittest
import uuid
from dataclasses import replace

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from crosscopy import protocol
from crosscopy.app import CrossCopyApp
from crosscopy.config import Settings

TOKEN = "test-token"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeClipboard:
    """Stands in for the Windows clipboard so tests never touch the real one."""

    def __init__(self) -> None:
        self.item: protocol.Clip | None = None
        self.writes: list[protocol.Clip] = []

    @property
    def value(self) -> str | None:
        return self.item.text if self.item is not None and self.item.is_text else None

    @value.setter
    def value(self, text: str | None) -> None:
        self.item = protocol.text_clip(text) if text else None

    def read_item(self) -> protocol.Clip | None:
        return self.item

    def write_item(self, clip: protocol.Clip) -> None:
        self.item = clip
        self.writes.append(clip)


def image_frame(png: bytes, *, origin: str = "phone-a") -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "clipboard.update",
            "id": uuid.uuid4().hex,
            "origin": origin,
            "ts": time.time(),
            "data": {
                "kind": "image",
                "mime": "image/png",
                "data": base64.b64encode(png).decode("ascii"),
                "digest": hashlib.sha256(png).hexdigest(),
                "bytes": len(png),
            },
        }
    )


def hello_frame(client_id: str, name: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "hello",
            "id": uuid.uuid4().hex,
            "origin": client_id,
            "ts": time.time(),
            "data": {"client_id": client_id, "name": name, "platform": "android"},
        }
    )


def update_frame(text: str, *, message_id: str | None = None, origin: str = "phone-a") -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "clipboard.update",
            "id": message_id or uuid.uuid4().hex,
            "origin": origin,
            "ts": time.time(),
            "data": {
                "kind": "text",
                "text": text,
                "digest": hashlib.sha256(text.encode()).hexdigest(),
                "bytes": len(text.encode()),
            },
        }
    )


def share_frame() -> str:
    return json.dumps(
        {"v": 1, "type": "clipboard.share", "id": uuid.uuid4().hex, "ts": time.time(), "data": {}}
    )


async def recv_type(socket, message_type: str, timeout: float = 3.0) -> dict:
    """Return the next frame of ``message_type``, skipping unrelated ones."""

    async def _pump() -> dict:
        while True:
            raw = await socket.recv()
            frame = json.loads(raw)
            if frame.get("type") == message_type:
                return frame

    return await asyncio.wait_for(_pump(), timeout=timeout)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def drain(socket, seconds: float) -> list[dict]:
    """Collect every frame that arrives within ``seconds``."""
    frames: list[dict] = []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return frames
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
        except TimeoutError:
            return frames
        frames.append(json.loads(raw))


class SyncServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clipboard = FakeClipboard()
        self.app, self.task = await self.start_app()

    async def asyncTearDown(self) -> None:
        self.app.server.stop()
        await asyncio.wait_for(self.task, timeout=5)
        for socket in getattr(self, "_sockets", []):
            await socket.close()

    async def start_app(self, **overrides) -> tuple[CrossCopyApp, asyncio.Task]:
        settings = Settings(
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            poll_interval=0.05,
            share_on_connect=overrides.get("share_on_connect", False),
            device_name="Test PC",
            tray=False,
        )
        app = CrossCopyApp(settings, backend=self.clipboard)
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(app.server.started.wait(), timeout=5)
        return app, task

    async def connect_phone(
        self, client_id: str = "phone-a", name: str = "Phone A", app: CrossCopyApp | None = None
    ):
        server = (app or self.app).server
        socket = await connect(f"ws://127.0.0.1:{server.port}/?token={TOKEN}")
        self._sockets = getattr(self, "_sockets", []) + [socket]
        await socket.send(hello_frame(client_id, name))
        welcome = await recv_type(socket, "welcome")
        return socket, welcome

    # -- handshake ---------------------------------------------------------

    async def test_welcome_describes_the_desktop(self) -> None:
        _, welcome = await self.connect_phone()
        data = welcome["data"]
        self.assertEqual(data["platform"], "windows")
        self.assertEqual(data["protocol"], 1)
        self.assertEqual(data["capabilities"], ["image", "text"])
        self.assertEqual(data["name"], "Test PC")

    async def test_missing_token_is_rejected(self) -> None:
        with self.assertRaises(InvalidStatus) as ctx:
            await connect(f"ws://127.0.0.1:{self.app.server.port}/")
        self.assertEqual(ctx.exception.response.status_code, 401)

    async def test_non_hello_first_frame_closes_the_socket(self) -> None:
        socket = await connect(f"ws://127.0.0.1:{self.app.server.port}/?token={TOKEN}")
        self._sockets = getattr(self, "_sockets", []) + [socket]
        await socket.send(update_frame("sneaky"))
        with self.assertRaises(Exception):
            await asyncio.wait_for(socket.recv(), timeout=3)

    # -- phone -> desktop --------------------------------------------------

    async def test_phone_update_reaches_the_desktop_clipboard(self) -> None:
        socket, _ = await self.connect_phone()
        await socket.send(update_frame("hello from the phone"))

        ack = await recv_type(socket, "ack")
        self.assertEqual(ack["data"]["status"], "applied")
        self.assertEqual(self.clipboard.value, "hello from the phone")

    async def test_phone_update_is_not_echoed_back_to_the_sender(self) -> None:
        socket, _ = await self.connect_phone()
        await socket.send(update_frame("no echo please"))

        frames = await drain(socket, 0.8)
        types = [frame["type"] for frame in frames]
        self.assertIn("ack", types)
        self.assertNotIn("clipboard.update", types)
        self.assertNotIn("clipboard.snapshot", types)

    async def test_update_from_one_phone_reaches_the_other_and_never_comes_back(self) -> None:
        first, _ = await self.connect_phone("phone-a", "Phone A")
        second, _ = await self.connect_phone("phone-b", "Phone B")
        await asyncio.sleep(0.1)
        await drain(first, 0.1)
        await drain(second, 0.1)

        await first.send(update_frame("shared text"))
        delivered = await recv_type(second, "clipboard.update")
        self.assertEqual(delivered["data"]["text"], "shared text")
        self.assertEqual(delivered["origin"], "phone-a")

        # The desktop applied the text; its watcher polls every 50 ms. Nothing new may appear.
        quiet = await drain(second, 0.8)
        self.assertEqual([f for f in quiet if f["type"].startswith("clipboard.")], [])
        self.assertEqual(self.clipboard.value, "shared text")

    async def test_image_from_one_phone_reaches_the_other_and_never_comes_back(self) -> None:
        first, _ = await self.connect_phone("phone-a", "Phone A")
        second, _ = await self.connect_phone("phone-b", "Phone B")
        await asyncio.sleep(0.1)
        await drain(first, 0.1)
        await drain(second, 0.1)

        await first.send(image_frame(PNG))
        delivered = await recv_type(second, "clipboard.update")
        self.assertEqual(delivered["data"]["kind"], "image")
        self.assertEqual(delivered["data"]["data"], base64.b64encode(PNG).decode())
        self.assertEqual(delivered["origin"], "phone-a")

        quiet = await drain(second, 0.8)
        self.assertEqual([f for f in quiet if f["type"].startswith("clipboard.")], [])
        self.assertIsNotNone(self.clipboard.item)
        self.assertTrue(self.clipboard.item.is_image)
        self.assertEqual(self.clipboard.item.png, PNG)

    async def test_tampered_image_is_refused(self) -> None:
        socket, _ = await self.connect_phone()
        frame = json.loads(image_frame(PNG))
        frame["data"]["digest"] = "0" * 64
        await socket.send(json.dumps(frame))

        error = await recv_type(socket, "error")
        self.assertEqual(error["data"]["code"], "digest_mismatch")
        self.assertIsNone(self.clipboard.item)

    async def test_desktop_image_is_shared_on_connect(self) -> None:
        app, task = await self.start_app(share_on_connect=True)
        try:
            app.core.apply_item(protocol.image_clip(PNG, 1, 1))
            phone, _ = await self.connect_phone(app=app)
            snapshot = await recv_type(phone, "clipboard.snapshot")
            self.assertEqual(snapshot["data"]["kind"], "image")
            self.assertEqual(snapshot["data"]["data"], base64.b64encode(PNG).decode())
        finally:
            app.server.stop()
            await asyncio.wait_for(task, timeout=5)

    async def test_replayed_message_is_ignored(self) -> None:
        first, _ = await self.connect_phone("phone-a", "Phone A")
        second, _ = await self.connect_phone("phone-b", "Phone B")
        await asyncio.sleep(0.1)
        await drain(second, 0.1)

        message_id = uuid.uuid4().hex
        await first.send(update_frame("only once", message_id=message_id))
        await recv_type(second, "clipboard.update")
        await drain(second, 0.15)

        await first.send(update_frame("only once", message_id=message_id))
        extra = await drain(second, 0.6)
        self.assertEqual([f for f in extra if f["type"] == "clipboard.update"], [])

    async def test_oversized_text_is_refused(self) -> None:
        socket, _ = await self.connect_phone()
        await socket.send(update_frame("x" * 70_000))

        error = await recv_type(socket, "error")
        self.assertEqual(error["data"]["code"], "text_too_large")
        self.assertIsNone(self.clipboard.value)

    # -- desktop -> phone --------------------------------------------------

    async def test_desktop_copy_is_pushed_to_connected_phones(self) -> None:
        first, _ = await self.connect_phone("phone-a", "Phone A")
        second, _ = await self.connect_phone("phone-b", "Phone B")
        await asyncio.sleep(0.1)
        await drain(first, 0.1)
        await drain(second, 0.1)

        self.clipboard.value = "copied on the laptop"

        for socket in (first, second):
            frame = await recv_type(socket, "clipboard.update")
            self.assertEqual(frame["data"]["text"], "copied on the laptop")
            self.assertTrue(frame["origin"].startswith("desktop-"))

        # Applying is not involved, so nothing may bounce back from the phones.
        quiet = await drain(first, 0.4)
        self.assertEqual([f for f in quiet if f["type"] == "clipboard.update"], [])

    async def test_share_request_returns_a_snapshot(self) -> None:
        self.clipboard.value = "desktop clipboard"
        socket, _ = await self.connect_phone()
        await socket.send(share_frame())

        snapshot = await recv_type(socket, "clipboard.snapshot")
        self.assertEqual(snapshot["data"]["text"], "desktop clipboard")

    async def test_connect_snapshot_when_enabled(self) -> None:
        self.clipboard.value = "already on the laptop"
        app, task = await self.start_app(share_on_connect=True)
        try:
            socket, _ = await self.connect_phone(app=app)
            snapshot = await recv_type(socket, "clipboard.snapshot")
            self.assertEqual(snapshot["data"]["text"], "already on the laptop")
        finally:
            app.server.stop()
            await asyncio.wait_for(task, timeout=5)

    async def test_crlf_from_windows_does_not_echo_back(self) -> None:
        socket, _ = await self.connect_phone()
        await socket.send(update_frame("line one\nline two"))
        await recv_type(socket, "ack")
        self.assertEqual(self.clipboard.value, "line one\nline two")

        frames = await drain(socket, 0.8)
        self.assertEqual([f for f in frames if f["type"] == "clipboard.update"], [])


class SettingsReloadTests(unittest.IsolatedAsyncioTestCase):
    """Saving new settings in the window must rebind the listener, not restart the app."""

    async def asyncSetUp(self) -> None:
        self.clipboard = FakeClipboard()
        self.app = CrossCopyApp(
            Settings(
                host="127.0.0.1",
                port=0,
                token="first-token",
                poll_interval=0.05,
                share_on_connect=False,
                device_name="Test PC",
                tray=False,
            ),
            backend=self.clipboard,
        )
        self.task = asyncio.create_task(self.app.run())
        await asyncio.wait_for(self.app.server.started.wait(), timeout=5)

    async def asyncTearDown(self) -> None:
        self.app.server.stop()
        await asyncio.wait_for(self.task, timeout=5)

    async def wait_for_server(self, previous, *, exclude_port: int | None = None) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            server = self.app.server
            if server is not previous and server.started.is_set():
                if exclude_port is None or server.port != exclude_port:
                    return
            await asyncio.sleep(0.02)
        self.fail("the server never rebound")

    async def test_new_port_and_token_take_effect_without_restarting(self) -> None:
        previous = self.app.server
        port = _free_port()
        self.app.apply_settings(replace(self.app.settings, port=port, token="second-token"))
        await self.wait_for_server(previous)

        self.assertEqual(self.app.server.port, port)
        self.assertEqual(self.app.status()["port"], port)
        self.assertIn("second-token", str(self.app.status()["url"]))

        phone = await connect(f"ws://127.0.0.1:{port}/?token=second-token")
        await phone.send(hello_frame("phone-z", "Phone Z"))
        welcome = await recv_type(phone, "welcome")
        self.assertEqual(welcome["data"]["name"], "Test PC")
        await phone.close()

        with self.assertRaises(InvalidStatus):
            await connect(f"ws://127.0.0.1:{port}/?token=first-token")

    async def test_busy_port_keeps_the_app_serving(self) -> None:
        previous = self.app.server
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = int(blocker.getsockname()[1])
        self.addCleanup(blocker.close)

        self.app.apply_settings(replace(self.app.settings, port=busy))
        await self.wait_for_server(previous, exclude_port=busy)

        self.assertIsNotNone(self.app.last_error)
        self.assertNotEqual(self.app.server.port, busy)

        # The old token still works because the previous settings were restored.
        port = self.app.server.port
        phone = await connect(f"ws://127.0.0.1:{port}/?token=first-token")
        await phone.send(hello_frame("phone-y", "Phone Y"))
        await recv_type(phone, "welcome")
        await phone.close()


if __name__ == "__main__":
    unittest.main()
