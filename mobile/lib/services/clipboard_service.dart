import 'dart:convert';

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
}

/// Real Android clipboard, via the `clipboard` package with a Flutter fallback.
class SystemClipboardBackend implements ClipboardBackend {
  const SystemClipboardBackend();

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
}

/// Decides what is a real local copy and what is the echo of our own write.
///
/// Two independent guards, matching the desktop implementation:
///
/// 1. [_lastObserved] is the canonical text the clipboard is believed to hold;
///    a poll returning it again is not a change.
/// 2. [_suppressed] holds digests of text we wrote because the network asked
///    us to. They are recorded *before* the write, so a poll that sneaks in
///    between write and state update is still recognized.
///
/// Suppression expires, so deliberately copying the same text again later
/// still propagates.
class ClipboardSyncCore {
  ClipboardSyncCore({
    ClipboardBackend backend = const SystemClipboardBackend(),
    this.suppressTtl = const Duration(seconds: 5),
    this.staleTtl = const Duration(milliseconds: 1500),
    this.maxBytes = maxTextBytes,
    DateTime Function()? clock,
  })  : _backend = backend,
        _clock = clock ?? DateTime.now;

  final ClipboardBackend _backend;
  final Duration suppressTtl;
  final Duration staleTtl;
  final int maxBytes;
  final DateTime Function() _clock;

  static const int _maxSuppressedEntries = 64;

  String? _lastObserved;
  final Map<String, DateTime> _suppressed = <String, DateTime>{};

  /// Adopt the clipboard as already-known state without announcing it.
  Future<void> prime() async {
    final text = await readCurrent();
    _lastObserved = text == null ? null : canonicalizeText(text);
  }

  /// Returns [text] when it is a genuine local change, otherwise null.
  String? observeLocal(String text) {
    if (text.isEmpty) return null;

    final canonical = canonicalizeText(text);
    if (utf8.encode(canonical).length > maxBytes) return null;

    final now = _clock();
    _prune(now);

    if (canonical == _lastObserved) return null;

    final digest = textDigest(canonical);
    final suppressedUntil = _suppressed.remove(digest);
    if (suppressedUntil != null && suppressedUntil.isAfter(now)) {
      _lastObserved = canonical;
      return null;
    }

    _lastObserved = canonical;
    return text;
  }

  /// Writes remote text locally. Returns true when the clipboard changed.
  Future<bool> applyRemote(String text) async {
    final canonical = canonicalizeText(text);
    if (utf8.encode(canonical).length > maxBytes) {
      throw ProtocolException('text_too_large', 'remote text exceeds $maxBytes bytes');
    }

    final now = _clock();
    _prune(now);

    if (canonical == _lastObserved) return false;

    final previous = _lastObserved;
    _suppress(canonical, now.add(suppressTtl));
    if (previous != null) {
      _suppress(previous, now.add(staleTtl));
    }
    _lastObserved = canonical;

    try {
      await _backend.writeText(canonical);
    } catch (_) {
      // The clipboard still holds the old value: treat it as unchanged.
      _suppressed.remove(textDigest(canonical));
      _lastObserved = previous;
      rethrow;
    }
    return true;
  }

  /// Current clipboard text, or null when it cannot be read.
  Future<String?> readCurrent() async {
    try {
      return await _backend.readText();
    } catch (_) {
      return null;
    }
  }

  void _suppress(String text, DateTime until) {
    if (_suppressed.length >= _maxSuppressedEntries) {
      final soonest = _suppressed.entries.reduce((a, b) => a.value.isBefore(b.value) ? a : b);
      _suppressed.remove(soonest.key);
    }
    _suppressed[textDigest(text)] = until;
  }

  void _prune(DateTime now) {
    _suppressed.removeWhere((_, expiry) => !expiry.isAfter(now));
  }
}
