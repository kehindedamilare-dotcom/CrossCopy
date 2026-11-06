import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';

/// Must match `PROTOCOL_VERSION` in the Python desktop app.
const int protocolVersion = 1;

/// Largest clipboard text frame the desktop accepts.
const int maxTextBytes = 64 * 1024;

/// Largest clipboard image the desktop accepts (PNG bytes).
const int maxImageBytes = 8 * 1024 * 1024;

/// Hard cap for one websocket frame: base64 inflates images by ~4/3.
const int maxFrameBytes = maxImageBytes * 4 ~/ 3 + 512 * 1024;

const String textContentKind = 'text';
const String imageContentKind = 'image';

/// Message types shared with the desktop. Phase 2 adds `files` here.
abstract final class MessageTypes {
  static const String hello = 'hello';
  static const String welcome = 'welcome';
  static const String clipboardUpdate = 'clipboard.update';
  static const String clipboardSnapshot = 'clipboard.snapshot';
  static const String clipboardShare = 'clipboard.share';
  static const String ack = 'ack';
  static const String error = 'error';
  static const String ping = 'ping';
  static const String pong = 'pong';
  static const String peerJoined = 'peer.joined';
  static const String peerLeft = 'peer.left';
}

/// Normalizes line endings so Windows (`\r\n`) and Android (`\n`) agree.
///
/// Without this, text applied from the desktop would look like a brand new
/// local copy and be pushed straight back.
String canonicalizeText(String text) {
  if (!text.contains('\r')) return text;
  return text.replaceAll('\r\n', '\n').replaceAll('\r', '\n');
}

String textDigest(String text) =>
    sha256.convert(utf8.encode(canonicalizeText(text))).toString();

int textSize(String text) => utf8.encode(text).length;

/// Content fingerprint of PNG bytes; identical bytes round-trip unchanged.
String imageDigest(Uint8List png) => sha256.convert(png).toString();

/// One clipboard item: either text or a PNG image.
sealed class ClipContent {
  const ClipContent();

  String get digest;

  int get size;

  String describe();
}

final class TextClip extends ClipContent {
  const TextClip(this.text);

  final String text;

  @override
  String get digest => textDigest(text);

  @override
  int get size => textSize(text);

  @override
  String describe() => '$size chars';
}

final class ImageClip extends ClipContent {
  const ImageClip(this.png, {this.width = 0, this.height = 0});

  final Uint8List png;
  final int width;
  final int height;

  @override
  String get digest => imageDigest(png);

  @override
  int get size => png.length;

  @override
  String describe() {
    final kib = size / 1024;
    final sizeText = kib >= 10 ? '${kib.round()} KB' : '${kib.toStringAsFixed(1)} KB';
    return width > 0 && height > 0
        ? 'image ${width}x$height ($sizeText)'
        : 'image ($sizeText)';
  }
}

String _newMessageId() {
  final random = Random.secure();
  return List<int>.generate(16, (_) => random.nextInt(256))
      .map((byte) => byte.toRadixString(16).padLeft(2, '0'))
      .join();
}

class ProtocolException implements Exception {
  ProtocolException(this.code, this.detail);

  final String code;
  final String detail;

  @override
  String toString() => 'ProtocolException($code: $detail)';
}

/// One protocol frame.
class SyncMessage {
  SyncMessage({
    required this.type,
    String? id,
    this.origin = '',
    double? ts,
    Map<String, dynamic>? data,
  })  : id = id ?? _newMessageId(),
        ts = ts ?? DateTime.now().millisecondsSinceEpoch / 1000,
        data = data ?? <String, dynamic>{};

  final String type;
  final String id;
  final String origin;
  final double ts;
  final Map<String, dynamic> data;

  factory SyncMessage.hello({
    required String clientId,
    required String name,
    String platform = 'android',
  }) =>
      SyncMessage(
        type: MessageTypes.hello,
        origin: clientId,
        data: {'client_id': clientId, 'name': name, 'platform': platform},
      );

  factory SyncMessage.clipboardUpdate({required String origin, required String text}) =>
      SyncMessage(
        type: MessageTypes.clipboardUpdate,
        origin: origin,
        data: {
          'kind': textContentKind,
          'text': text,
          'digest': textDigest(text),
          'bytes': textSize(text),
        },
      );

  factory SyncMessage.clipboardImage({
    required String origin,
    required Uint8List png,
    int width = 0,
    int height = 0,
  }) =>
      SyncMessage(
        type: MessageTypes.clipboardUpdate,
        origin: origin,
        data: {
          'kind': imageContentKind,
          'mime': 'image/png',
          'data': base64Encode(png),
          'digest': imageDigest(png),
          'bytes': png.length,
          if (width > 0 && height > 0) 'width': width,
          if (width > 0 && height > 0) 'height': height,
        },
      );

  factory SyncMessage.clipboardContentUpdate({
    required String origin,
    required ClipContent clip,
  }) =>
      switch (clip) {
        TextClip(:final text) => SyncMessage.clipboardUpdate(origin: origin, text: text),
        ImageClip(:final png, :final width, :final height) =>
          SyncMessage.clipboardImage(origin: origin, png: png, width: width, height: height),
      };

  factory SyncMessage.shareRequest() => SyncMessage(type: MessageTypes.clipboardShare);

  factory SyncMessage.ping(int sequence) =>
      SyncMessage(type: MessageTypes.ping, data: {'n': sequence});

  /// The clipboard item carried by a message, or null when the payload kind is
  /// not understood (for example Phase 2 file payloads).
  ClipContent? get clipboardContent {
    final kind = data['kind'];

    if (kind == textContentKind) {
      final text = data['text'];
      if (text is! String) return null;
      if (textSize(text) > maxTextBytes) return null;
      return TextClip(canonicalizeText(text));
    }

    if (kind == imageContentKind) {
      final encoded = data['data'];
      if (encoded is! String) return null;

      final Uint8List png;
      try {
        png = base64Decode(encoded);
      } catch (_) {
        return null;
      }
      if (png.isEmpty || png.length > maxImageBytes) return null;

      final claimed = data['digest'];
      if (claimed is String && claimed.isNotEmpty && claimed != imageDigest(png)) {
        return null;
      }

      final width = data['width'];
      final height = data['height'];
      return ImageClip(
        png,
        width: width is int && width > 0 ? width : 0,
        height: height is int && height > 0 ? height : 0,
      );
    }

    return null;
  }

  /// The text carried by a clipboard message, or null for images and unknown
  /// payload kinds.
  String? get clipboardText {
    final content = clipboardContent;
    return content is TextClip ? content.text : null;
  }

  String encode() => jsonEncode({
        'v': protocolVersion,
        'type': type,
        'id': id,
        'origin': origin,
        'ts': ts,
        'data': data,
      });

  static SyncMessage decode(Object? raw) {
    final String document;
    if (raw is String) {
      document = raw;
    } else if (raw is List<int>) {
      try {
        document = utf8.decode(raw);
      } catch (_) {
        throw ProtocolException('bad_encoding', 'frame is not valid UTF-8');
      }
    } else {
      throw ProtocolException('bad_frame', 'unsupported frame type ${raw.runtimeType}');
    }

    if (utf8.encode(document).length > maxFrameBytes) {
      throw ProtocolException('frame_too_large', 'frame exceeds $maxFrameBytes bytes');
    }

    final Object? parsed;
    try {
      parsed = jsonDecode(document);
    } catch (_) {
      throw ProtocolException('bad_json', 'frame is not valid JSON');
    }

    if (parsed is! Map) {
      throw ProtocolException('bad_frame', 'frame must be a JSON object');
    }
    final map = Map<String, dynamic>.from(parsed);

    if (map['v'] != protocolVersion) {
      throw ProtocolException(
        'version_mismatch',
        'desktop speaks v${map['v']}, app speaks v$protocolVersion',
      );
    }

    final type = map['type'];
    if (type is! String || type.isEmpty) {
      throw ProtocolException('bad_frame', "missing 'type'");
    }

    final data = map['data'];
    final ts = map['ts'];

    return SyncMessage(
      type: type,
      id: map['id'] is String && (map['id'] as String).isNotEmpty ? map['id'] as String : null,
      origin: map['origin'] is String ? map['origin'] as String : '',
      ts: ts is num ? ts.toDouble() : null,
      data: data is Map ? Map<String, dynamic>.from(data) : <String, dynamic>{},
    );
  }
}
