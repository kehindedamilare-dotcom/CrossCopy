# CrossCopy — Android client

Flutter app that connects to the CrossCopy desktop WebSocket server and keeps the
phone clipboard in sync with the laptop (text and images).

See the [project README](../README.md) for the architecture, protocol, loop
prevention and full run instructions.

## Layout

| Path | Purpose |
| --- | --- |
| `lib/main.dart` | Material 3 UI: pairing form, status, last synced text/image, activity log. |
| `lib/protocol.dart` | Wire protocol v1 (envelope, validation, `TextClip`/`ImageClip`, digests). |
| `lib/services/clipboard_service.dart` | Clipboard backend (text + image channel) plus echo-cancelling `ClipboardSyncCore`. |
| `lib/services/connection_service.dart` | WebSocket lifecycle, handshake, heartbeat, reconnect with backoff. |
| `lib/sync_controller.dart` | Polling, applying remote content, replay dedupe, app lifecycle. |
| `android/.../MainActivity.kt` | `crosscopy/clipboard_image` channel: PNG clipboard images through a `FileProvider`. |

## Run

```bat
flutter pub get
flutter run
```

Enter the desktop IP, port (default `8765`) and the pairing token printed by the
desktop app, then tap **Connect**.

The Android build needs a JDK 10 or newer (Android Studio's bundled JBR works):

```bat
flutter config --jdk-dir="C:\Program Files\Android\Android Studio\jbr"
```

## Icons

The launcher icon is generated from the artwork at `../crosscopy.jpg`; do not
edit the PNGs by hand. Regenerate legacy + adaptive icons with:

```bat
desktop\.venv\Scripts\python tools\make_icons.py
```

## Test

```bat
flutter test
```
