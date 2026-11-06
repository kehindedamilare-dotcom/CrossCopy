import 'package:clipboard/clipboard.dart';
import 'package:flutter/services.dart';

import '../protocol.dart';

/// The clipboard surface used by the sync core.
///
/// Phase 2 can add `readFiles`/`writeFiles` here without changing anything
/// else in the app.
abstract interface class ClipboardBackend {
  Future<String?> readText();

  Future<void> writeText(String text);

  /// The clipboard image as PNG, or null when there is no image.
  Future<ImageClip?> readImage();

  Future<void> writeImage(ImageClip image);
}

/// Real Android clipboard: text through the `clipboard` package, images through
/// the platform channel implemented in `MainActivity.kt`.
class SystemClipboardBackend implements ClipboardBackend {
  const SystemClipboardBackend();

  static const MethodChannel _images = MethodChannel('crosscopy/clipboard_image');

  @override
  Future<String?> readText() async {
    String? text;
    try {
      text = await FlutterClipboard.paste();
    } catch (_) {
      // Android 10+ denies clipboard reads to apps that are not in front, and
      // the platform channel can also throw if it is not registered.
      final data = await Clipboard.getData(Clipboard.kTextPlain);
      text = data?.text;
    }
    if (text == null || text.isEmpty) return null;
    return text;
  }

  @override
  Future<void> writeText(String text) async {
    if (text.isEmpty) return;
    try {
      await FlutterClipboard.copy(text);
    } catch (_) {
      await Clipboard.setData(ClipboardData(text: text));
    }
  }

  @override
  Future<ImageClip?> readImage() async {
    try {
      final result = await _images.invokeMapMethod<String, Object?>('read');
      if (result == null) return null;
      final bytes = result['bytes'];
      if (bytes is! Uint8List || bytes.isEmpty) return null;
      final width = result['width'];
      final height = result['height'];
      return ImageClip(
        bytes,
        width: width is int ? width : 0,
        height: height is int ? height : 0,
      );
    } on MissingPluginException {
      return null; // tests, or a build without the native handler
    } catch (_) {
      return null;
    }
  }

  @override
  Future<void> writeImage(ImageClip image) async {
    await _images.invokeMethod<void>('write', <String, Object?>{
      'bytes': image.png,
    });
  }
}

/// Decides what is a real local copy and what is the echo of our own write.
///
/// Two independent guards, matching the desktop implementation:
///
/// 1. [_lastObserved] is the digest of the item the clipboard is believed to
///    hold; a poll returning it again is not a change.
/// 2. [_suppressed] holds digests of content we wrote because the network asked
///    us to. They are recorded *before* the write, so a poll that sneaks in
///    between write and state update is still recognized.
///
/// Suppression expires, so deliberately copying the same content again later
/// still propagates. Both text and images use the same machinery.
class ClipboardSyncCore {
  ClipboardSyncCore({
    ClipboardBackend backend = const SystemClipboardBackend(),
    this.suppressTtl = const Duration(seconds: 5),
    this.staleTtl = const Duration(milliseconds: 1500),
    this.maxBytes = maxTextBytes,
    this.maxImageSize = maxImageBytes,
    DateTime Function()? clock,
  })  : _backend = backend,
        _clock = clock ?? DateTime.now;

  final ClipboardBackend _backend;
  final Duration suppressTtl;
  final Duration staleTtl;
  final int maxBytes;
  final int maxImageSize;
  final DateTime Function() _clock;

  static const int _maxSuppressedEntries = 64;

  String? _lastObserved;
  final Map<String, DateTime> _suppressed = <String, DateTime>{};

  /// Adopt the clipboard as already-known state without announcing it.
  Future<void> prime() async {
    final clip = await readCurrentClip();
    _lastObserved = clip?.digest;
  }

  /// Returns [clip] when it is a genuine local change, otherwise null.
  ClipContent? observeLocalClip(ClipContent? clip) {
    if (clip == null) return null;
    if (clip is TextClip && clip.text.isEmpty) return null;
    if (clip is TextClip && clip.size > maxBytes) return null;
    if (clip is ImageClip && clip.size > maxImageSize) return null;

    final now = _clock();
    _prune(now);

    final digest = clip.digest;
    if (digest == _lastObserved) return null;

    final suppressedUntil = _suppressed.remove(digest);
    if (suppressedUntil != null && suppressedUntil.isAfter(now)) {
      _lastObserved = digest;
      return null;
    }

    _lastObserved = digest;
    return clip;
  }

  /// Writes remote content locally. Returns true when the clipboard changed.
  Future<bool> applyRemoteClip(ClipContent clip) async {
    if (clip is TextClip && clip.size > maxBytes) {
      throw ProtocolException('text_too_large', 'remote text exceeds $maxBytes bytes');
    }
    if (clip is ImageClip && clip.size > maxImageSize) {
      throw ProtocolException('image_too_large', 'remote image exceeds $maxImageSize bytes');
    }

    final now = _clock();
    _prune(now);

    final digest = clip.digest;
    if (digest == _lastObserved) return false;

    final previous = _lastObserved;
    _suppress(digest, now.add(suppressTtl));
    if (previous != null) {
      _suppress(previous, now.add(staleTtl));
    }
    _lastObserved = digest;

    try {
      switch (clip) {
        case TextClip(:final text):
          await _backend.writeText(text);
        case ImageClip():
          await _backend.writeImage(clip);
      }
    } catch (_) {
      // The clipboard still holds the old value: treat it as unchanged.
      _suppressed.remove(digest);
      _lastObserved = previous;
      rethrow;
    }
    return true;
  }

  /// Text-only shim kept for callers that never deal with images.
  String? observeLocal(String text) {
    if (text.isEmpty) return null;
    final clip = TextClip(canonicalizeText(text));
    final result = observeLocalClip(clip);
    return result is TextClip ? result.text : null;
  }

  Future<bool> applyRemote(String text) =>
      applyRemoteClip(TextClip(canonicalizeText(text)));

  /// Current clipboard item (image preferred), or null when unreadable/empty.
  Future<ClipContent?> readCurrentClip() async {
    try {
      final image = await _backend.readImage();
      if (image != null) return image;

      final text = await _backend.readText();
      if (text == null || text.isEmpty) return null;
      return TextClip(canonicalizeText(text));
    } catch (_) {
      return null;
    }
  }

  /// Current clipboard text, or null when it holds an image or nothing.
  Future<String?> readCurrent() async {
    final clip = await readCurrentClip();
    return clip is TextClip ? clip.text : null;
  }

  void _suppress(String digest, DateTime until) {
    if (_suppressed.length >= _maxSuppressedEntries) {
      final soonest = _suppressed.entries.reduce((a, b) => a.value.isBefore(b.value) ? a : b);
      _suppressed.remove(soonest.key);
    }
    _suppressed[digest] = until;
  }

  void _prune(DateTime now) {
    _suppressed.removeWhere((_, expiry) => !expiry.isAfter(now));
  }
}
