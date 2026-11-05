import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';

/// Must match `PROTOCOL_VERSION` in the Python desktop app.
const int protocolVersion = 1;

/// Largest clipboard text frame the desktop accepts.
const int maxTextBytes = 64 * 1024;

/// Hard cap for one websocket frame (mirrors the desktop).
const int maxFrameBytes = 256 * 1024;

const String textContentKind = 'text';

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

  factory SyncMessage.shareRequest() => SyncMessage(type: MessageTypes.clipboardShare);

  factory SyncMessage.ping(int sequence) =>
      SyncMessage(type: MessageTypes.ping, data: {'n': sequence});

  /// The text carried by a clipboard message, or null if this payload kind is
  /// not understood (for example Phase 2 file payloads).
  String? get clipboardText {
    if (data['kind'] != textContentKind) return null;
    final text = data['text'];
    if (text is! String) return null;
    if (textSize(text) > maxTextBytes) return null;
    return text;
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
