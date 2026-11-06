# CrossCopy

A universal clipboard for one Windows laptop and one or more Android phones, over
the local Wi-Fi network. Copy text **or an image** on the laptop and it appears on
the phone; copy on the phone and it appears on the laptop. Runs in the system
tray, can start with Windows, and needs no cloud, account or pairing service.

**Current scope:** text and PNG images. The wire format, clipboard abstraction
and UI are built so file sync can be added as a new content kind without
touching the transport or the echo-suppression logic (see
[Extending to Phase 2](#extending-to-phase-2-files)).

---

## 1. Architecture

```
┌──────────────────────────── Windows laptop ────────────────────────────┐
│                                                                        │
│  text (pyperclip) / images (Win32) ←→ ClipboardSyncCore ←→ outbox → Hub│
│                        ↑                                   │           │
│            ClipboardWatcher thread (poll every 0.35 s)     │           │
│  system tray ← status, copy link, autostart, quit          │           │
│                                                WebSocket server :8765  │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │  ws://192.168.x.y:8765?token=…
                                     │  (plain LAN, no cloud)
┌────────────────────────────────────┴───────────────────────────────────┐
│                            Android phone                               │
│                                                                        │
│  clipboard pkg ←→ ClipboardSyncCore ←→ SyncController ←→ ConnectionSvc │
│                          ↑                                  (ws +      │
│              Timer poll every 0.6 s (foreground only)        backoff)  │
└────────────────────────────────────────────────────────────────────────┘
```

The laptop is the **server/hub**; the phone is a **client**. The phone connects
to the laptop's LAN IP, so there is no port-forwarding and no discovery
protocol to go wrong. The hub accepts several phones and fans every update out
to all of them except the sender.

### Desktop modules (`desktop/crosscopy/`)

| File | Responsibility |
| --- | --- |
| `protocol.py` | Frame envelope, message types, `Clip` (text or PNG), canonicalization, digests, validation and size limits. |
| `clipboard.py` | `ClipboardBackend` interface, `SystemClipboard` (pyperclip + Win32 images), `ClipboardSyncCore` (echo cancellation), `ClipboardWatcher` (background polling thread). |
| `imageclip.py` | Raw Win32 clipboard access for images: reads the `PNG` format byte-exactly, falls back to `CF_DIB`, writes both formats. |
| `hub.py` | Connected-peer registry, per-peer send lock, broadcast, replay window. |
| `server.py` | WebSocket server, token auth, handshake, routing, acks/errors, graceful shutdown. |
| `app.py` | Wiring: watcher thread → asyncio queue → broadcast, remote content → clipboard, tray lifecycle, signal handling. |
| `tray.py` | System-tray icon: status, copy pairing link, open log, start-with-Windows toggle, quit. |
| `startup.py` | Per-user `HKCU\…\Run` autostart entry (no admin, no console flash). |
| `logfile.py` | `%LOCALAPPDATA%\CrossCopy\crosscopy.log` logging shared by CLI and tray. |
| `config.py` | CLI flags, LAN address discovery for the startup banner and pairing URL. |

### Mobile modules (`mobile/lib/`)

| File | Responsibility |
| --- | --- |
| `protocol.dart` | Mirrors the Python envelope exactly (same version, same validation, text or PNG `ClipContent`). |
| `services/clipboard_service.dart` | `ClipboardBackend`, `SystemClipboardBackend` (text + image channel), `ClipboardSyncCore`. |
| `services/connection_service.dart` | Socket lifecycle, `hello`/`welcome`, heartbeat, reconnect with exponential backoff + jitter. |
| `sync_controller.dart` | Glue: polling, applying remote content, dedupe, lifecycle handling, activity log. |
| `main.dart` | Material 3 UI: pairing form, last-synced preview (text or image), manual push, activity list. |
| `MainActivity.kt` (Android) | `crosscopy/clipboard_image` channel: reads/writes clipboard images through a `FileProvider`, normalising to PNG. |

### Wire protocol (v1)

One JSON object per WebSocket text frame:

```json
{
  "v": 1,
  "type": "clipboard.update",
  "id": "1f0ac9…",
  "origin": "android-3f9c1a2b4d6e",
  "ts": 1762345678.123,
  "data": {
    "kind": "text",
    "text": "copied on the phone",
    "digest": "sha256…",
    "bytes": 20
  }
}
```

An image uses the same envelope with a different `data.kind`:

```json
{
  "v": 1,
  "type": "clipboard.update",
  "id": "8c21…",
  "origin": "android-3f9c1a2b4d6e",
  "ts": 1762345678.900,
  "data": {
    "kind": "image",
    "mime": "image/png",
    "data": "iVBORw0KGgo…",
    "digest": "sha256…",
    "bytes": 48213,
    "width": 1080,
    "height": 1920
  }
}
```

| `data.kind` | Payload | Limits |
| --- | --- | --- |
| `text` | `text` (string) | 64 KiB UTF-8 |
| `image` | `data` (base64 PNG), `mime`, optional `width`/`height` | 8 MiB PNG |

| Type | Direction | Meaning |
| --- | --- | --- |
| `hello` | phone → PC | Client id, display name, platform. Must be the first frame. |
| `welcome` | PC → phone | Server id/name, protocol version, capabilities, peer list. |
| `clipboard.update` | both | New clipboard content. |
| `clipboard.snapshot` | PC → phone | Current desktop clipboard (on connect, or on request). |
| `clipboard.share` | phone → PC | "Send me your current clipboard." |
| `ack` / `error` | PC → phone | Result of an update. |
| `ping` / `pong` | both | App-level heartbeat and RTT measurement. |
| `peer.joined` / `peer.left` | PC → phone | Roster changes. |

The envelope is deliberately generic: `data.kind` is a discriminator, and the
desktop validates it against a capability set that the phone receives in
`welcome`.

---

## 2. Loop prevention (echo cancellation)

This is the part that decides whether the tool is usable or melts the network,
so it is handled with several independent layers.

### The failure mode

1. Phone copies `"hi"` → sends `clipboard.update`.
2. Desktop writes `"hi"` to its clipboard.
3. Desktop's watcher polls the clipboard, sees `"hi"`, and would push it back.
4. Phone applies it, its poller sees `"hi"`, and pushes again… forever.

The same loop applies to an image copied on either side. Because the machinery
works on a content digest rather than on strings, it needed no changes for
images: `Clip.digest` is the SHA-256 of the PNG bytes (which round-trip
byte-for-byte on both platforms) instead of the canonical text.

### The five guards

1. **One canonical form per text.** `canonicalizeText()` converts `\r\n` and
   `\r` to `\n` on both sides. This matters because `pyperclip` returns CRLF on
   Windows while Android uses LF; without it, every applied write would look
   like brand-new content to the sender.
2. **Known-state tracking.** Each side remembers the digest of the item it
   believes is on the clipboard (`_lastObserved`). A poll that returns that same
   content is not a change, so it is never emitted.
3. **Suppression by content digest, written *before* the clipboard write.** When
   remote content is applied, its SHA-256 digest is recorded with a 5-second TTL
   *before* the clipboard is written. Any poll that races in between the write
   and the state update is recognized as an echo. The previous clipboard value
   is suppressed briefly (1.5 s) too, which swallows reads that were already in
   flight when the remote update landed. Entries expire, so deliberately copying
   the same content again later still propagates.
4. **Origin stamping and dedupe.** Every frame carries `origin` and a unique
   `id`. The server never forwards a message to the peer it came from, and the
   phone drops any frame whose `origin` is its own id. Both sides keep a
   bounded replay window of message ids, so a duplicated frame cannot cause a
   second write/broadcast. The server also overwrites `origin` with the
   authenticated peer id, so a client cannot impersonate another device and
   suppress its updates.
5. **Connect snapshots use the same path.** When the phone connects, the
   desktop sends its current clipboard as `clipboard.snapshot`. The phone
   applies it through `applyRemote()`, so guard 3 suppresses the resulting poll.
   The phone also `prime()`s its own clipboard on connect, so the text that was
   already on it is not announced as a fresh copy.

### Trace of a phone copy

```
phone: timer reads "hi"
       core.observeLocal("hi") → "hi"        (different from _lastObserved)
       → send clipboard.update
PC:    server receives, stamps origin = phone id
       core.applyRemote("hi")
         suppress(digest("hi"), 5 s)
         suppress(digest(previous), 1.5 s)
         _lastObserved = "hi"
         pyperclip.copy("hi")
       broadcast to *other* peers (never back to the phone)
       ack → phone
PC:    watcher polls "hi"
       core.observeLocal("hi") → None        (equals _lastObserved)
phone: receives no frame at all
```

The desktop's own copies follow the mirror image of this path, and the tests
below assert that a second phone receives exactly one update and then silence
while the watcher keeps polling at 50 ms.

---

## 3. Running it

### 3.1 Desktop (Windows)

```bat
cd desktop
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m crosscopy --token "choose-a-secret"
```

The app prints the exact URL to type into the phone, then shows a **system tray
icon** (look in the notification area, possibly under the `^` overflow):

```
14:03:11  INFO    CrossCopy desktop 'DESKTOP-ABC' listening on 0.0.0.0:8765
14:03:11  INFO      phone URL: ws://192.168.100.15:8765?token=choose-a-secret
14:03:11  INFO    watching the clipboard every 0.35s
14:03:11  INFO    tray icon visible
```

**Tray menu**

| Item | What it does |
| --- | --- |
| *Waiting for a phone* / *Connected to 1 phone* | Live status; the line under it names the phone. |
| *Copy pairing link* | Puts `ws://<lan-ip>:<port>?token=…` on the clipboard (double-click the icon too). |
| *Open log* | Opens `%LOCALAPPDATA%\CrossCopy\crosscopy.log`. |
| *Start with Windows* (checkbox) | Adds/removes the per-user autostart entry. |
| *Quit CrossCopy* | Stops the server and exits. |

Closing the terminal window also stops it; there is no window to close.

Notes:

- The first run triggers a Windows Defender Firewall prompt. Allow access on
  **Private networks**; the phone cannot reach the laptop otherwise.
- Find the IP manually with `ipconfig` (the `Wi-Fi` adapter's IPv4 address)
  if the printed one is from a virtual adapter.
- Flags: `--host`, `--port`, `--token` (or `CROSSCOPY_TOKEN`), `--name`,
  `--poll-interval`, `--no-share-on-connect`, `--verbose`, `--no-tray`,
  `--install-startup`, `--uninstall-startup`, `--startup-status`.
- The token is optional, but without it any device on the Wi-Fi can read or
  replace your clipboard. The server warns at startup when it is missing.
- If a run seems to do nothing, read the log file: every start, connection and
  error is recorded there, including when a second instance is refused.

### 3.1a Build a silent `CrossCopy.exe` and start it with Windows

A packaged build is the right way to get seamless startup: `python -m crosscopy`
always needs a console window, and a `.cmd`/shortcut in the Startup folder opens
one. PyInstaller's `--windowed` build has no console at all, and the app logs to
`%LOCALAPPDATA%\CrossCopy\crosscopy.log` instead.

```bat
cd desktop
.venv\Scripts\activate
pip install pyinstaller
pyinstaller --noconfirm CrossCopy.spec
```

`CrossCopy.spec` already embeds the tray icon and marks the exe as windowed. The
result is a single file:

```
desktop\dist\CrossCopy.exe
```

Register it for login (per-user, no administrator prompt):

```bat
desktop\dist\CrossCopy.exe --install-startup
```

Verify / remove:

```bat
desktop\dist\CrossCopy.exe --startup-status
:: enabled
::   "C:\…\desktop\dist\CrossCopy.exe"

desktop\dist\CrossCopy.exe --uninstall-startup
```

The entry is a plain `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
value, so it is visible in Task Manager → Startup and can be toggled from the
tray checkbox. If you move the exe, re-run `--install-startup`.

> **Why a build is needed:** Windows can start `pythonw.exe launcher.py` at
> login without any extra package, but that depends on this folder and the venv
> staying in place, and any import error is invisible. The exe is self-contained
> and is what the tests below were run against.
> A windowed exe starts in ~6 s on the first launch (PyInstaller unpacks it and
> Defender scans it); later launches are faster.

> The `--windowed` flag is what suppresses the console. If you ever see a black
> window, you built `CrossCopyDebug.spec` instead — that variant is deliberately
> console-enabled for reading tracebacks.

**Faster login start (one folder instead of one file).** A onefile exe unpacks
itself on every launch (≈6 s warm, longer while Defender scans a fresh build).
If that matters, build the folder variant and register that exe instead:

```bat
cd desktop
.venv\Scripts\pyinstaller --noconfirm --windowed --onedir --name CrossCopy ^
  --icon assets\crosscopy.ico --add-data "assets;assets" ^
  --hidden-import pystray._win32 launcher.py

dist\CrossCopy\CrossCopy.exe --install-startup
```

It is still a silent, windowless app; it just starts in about a second. Keep the
whole `dist\CrossCopy` folder together when you move it.

### 3.2 Phone (Android)

Flutter's Android build needs a JDK 10 or newer (Java 8 is *not* sufficient because
Gradle's compileOptions/targetCompatibility will reject it). Android Studio's
bundled JBR works fine. Point Flutter at it once:

```bat
flutter config --jdk-dir="C:\Program Files\Android\Android Studio\jbr"
flutter doctor -v
```

The project's `android/app/build.gradle.kts` was also updated to `JavaVersion.VERSION_10`
instead of `VERSION_17` so the Gradle task accepts Java 10+ right away.

Then:

```bat
cd mobile
flutter pub get
flutter run          :: phone connected over USB, or an emulator running
```

Then in the app:

1. Enter the laptop IP (`192.168.100.15` in the example) and port `8765`.
2. Enter the same pairing token, if you set one.
3. Tap **Connect**. The pill in the app bar turns green.
4. Copy text on either device.

Useful extras:

- **Emulator:** the host machine is `10.0.2.2`, so enter `10.0.2.2` as the IP.
- **Release APK:** `flutter build apk --release`, output in
  `mobile/build/app/outputs/flutter-apk/app-release.apk`.
- **Prefill for repeated runs:**
  `flutter run --dart-define=CROSSCOPY_HOST=192.168.100.15 --dart-define=CROSSCOPY_TOKEN=choose-a-secret`
- **Push clipboard** sends what is currently on the phone even if the app was
  disconnected when it was copied (connect-time sync is always desktop → phone,
  by design, so stale phone content can never clobber the laptop).

The app needs `INTERNET` and cleartext WebSocket traffic, both already set in
`mobile/android/app/src/main/AndroidManifest.xml`.

### 3.3 Optional: try it without a phone

```bat
cd desktop
.venv\Scripts\python -m unittest discover -s tests -t . -v
```

`tests/test_server.py` starts a real server on a random port with a fake
clipboard and drives it with real WebSocket clients, including the
"no echo, ever" case with two phones.

```bat
cd mobile
flutter test
```

---

## 4. Known limitations (Phase 1)

- **Android background restriction.** Since Android 10, an app may only read the
  clipboard while it has focus (or is the default input method). CrossCopy polls
  while its UI is in the foreground and pauses when backgrounded; incoming text
  from the desktop is still written immediately. There is no reliable way around
  this without an accessibility/IME integration, which is out of scope.
- **Text and images only.** HTML, rich text and files are ignored (the desktop
  replies with an `unsupported_kind` error). An image that is larger than 8 MiB
  as PNG is skipped with a warning rather than blocking the connection.
- **No encryption.** Traffic is unencrypted WebSocket on your LAN. Use the token,
  trust your network, and never expose the port to the internet.
- **One desktop per phone.** The app connects to a single laptop; the hub
  supports multiple phones.
- **Android may show its "pasted from clipboard" notice** once per new item,
  because the app has to read the clipboard to detect changes.
- **Change latency** is bounded by polling: ~0.35 s on Windows, ~0.6 s on
  Android.
- Clipboard history and global hotkeys are not implemented (the tray icon is).

---

## 5. Extending to Phase 2 (files)

The design keeps transport and echo-suppression independent of content:

1. **Protocol.** Add `CONTENT_FILES = "files"` to `SUPPORTED_CONTENT_KINDS`
   (Python) and a matching `FilesClip` in `ClipContent` (Dart). The capability
   list in `welcome` already travels from `SUPPORTED_CONTENT_KINDS`, so the
   phone only offers what the desktop supports.
2. **Clipboard layer.** Add a `files` variant to `Clip`, `read_item()` and
   `write_item()`; `ClipboardSyncCore` needs no changes because it already works
   on content digests rather than strings. Keep digests over content, not over
   data that changes per transfer.
3. **Chunking.** Send file payloads as `file.offer` → `file.chunk` →
   `file.complete` messages using the same `id` for correlation, or stream
   binary frames on the same socket. The envelope, replay window and
   suppression still apply; chunks are exempt from text size limits but need
   their own caps.
4. **UI.** The activity list and preview widget already take arbitrary payload
   metadata; add a file list card next to "Last synced".

No transport, auth, roster or reconnect code needs to change.
