# CrossCopy — Android client

Flutter app that connects to the CrossCopy desktop WebSocket server and keeps the
phone clipboard in sync with the laptop.

See the [project README](../README.md) for the architecture, protocol, loop
prevention and full run instructions.

## Layout

| Path | Purpose |
| --- | --- |
| `lib/main.dart` | Material 3 UI: pairing form, status, last synced text, activity log. |
| `lib/protocol.dart` | Wire protocol v1 (envelope, validation, canonical text, digests). |
| `lib/services/clipboard_service.dart` | Clipboard backend plus echo-cancelling `ClipboardSyncCore`. |
| `lib/services/connection_service.dart` | WebSocket lifecycle, handshake, heartbeat, reconnect with backoff. |
| `lib/sync_controller.dart` | Polling, applying remote text, replay dedupe, app lifecycle. |

## Run

```bat
flutter pub get
flutter run
```

Enter the desktop IP, port (default `8765`) and the pairing token printed by the
desktop app, then tap **Connect**.

The Android build needs a JDK 17+ (Android Studio's bundled JBR works):

```bat
flutter config --jdk-dir="C:\Program Files\Android\Android Studio\jbr"
```

## Test

```bat
flutter test
```
