import 'dart:convert';
import 'dart:typed_data';

import 'package:crosscopy/protocol.dart';
import 'package:crosscopy/services/clipboard_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// 1x1 red PNG; the protocol layer only hashes the bytes.
final Uint8List png = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
);

class FakeClipboard implements ClipboardBackend {
  FakeClipboard({this.text, this.image});

  String? text;
  ImageClip? image;
  bool failWrites = false;

  @override
  Future<String?> readText() async => text;

  @override
  Future<void> writeText(String value) async {
    if (failWrites) throw StateError('clipboard locked');
    text = value;
    image = null;
  }

  @override
  Future<ImageClip?> readImage() async => image;

  @override
  Future<void> writeImage(ImageClip clip) async {
    if (failWrites) throw StateError('clipboard locked');
    image = clip;
    text = null;
  }
}

class FakeClock {
  DateTime now = DateTime.utc(2026, 1, 1, 12);

  DateTime call() => now;

  void advance(Duration duration) => now = now.add(duration);
}

void main() {
  group('protocol', () {
    test('image frames round-trip with digest intact', () {
      final message = SyncMessage.clipboardImage(origin: 'android-1', png: png, width: 1, height: 1);
      final decoded = SyncMessage.decode(message.encode());
      final clip = decoded.clipboardContent;

      expect(clip, isA<ImageClip>());
      clip as ImageClip;
      expect(clip.png, png);
      expect(clip.width, 1);
      expect(clip.height, 1);
      expect(clip.digest, imageDigest(png));
    });

    test('text frames still decode as text', () {
      final decoded = SyncMessage.decode(
        SyncMessage.clipboardUpdate(origin: 'android-1', text: 'hi').encode(),
      );
      expect(decoded.clipboardContent, isA<TextClip>());
    });

    test('a tampered digest is rejected', () {
      final message = SyncMessage.clipboardImage(origin: 'android-1', png: png);
      final tampered = SyncMessage(
        type: message.type,
        origin: message.origin,
        data: <String, dynamic>{...message.data, 'digest': '0' * 64},
      );
      expect(tampered.clipboardContent, isNull);
    });

    test('broken base64 is rejected', () {
      final message = SyncMessage(
        type: MessageTypes.clipboardUpdate,
        origin: 'desktop',
        data: <String, dynamic>{'kind': imageContentKind, 'data': 'not base64!!'},
      );
      expect(message.clipboardContent, isNull);
    });

    test('oversized images are rejected', () {
      final message = SyncMessage(
        type: MessageTypes.clipboardUpdate,
        origin: 'desktop',
        data: <String, dynamic>{
          'kind': imageContentKind,
          'data': base64Encode(List<int>.filled(maxImageBytes + 1, 7)),
        },
      );
      expect(message.clipboardContent, isNull);
    });
  });

  group('image echo cancellation', () {
    late FakeClipboard clipboard;
    late FakeClock clock;
    late ClipboardSyncCore core;

    setUp(() async {
      clipboard = FakeClipboard(text: 'initial');
      clock = FakeClock();
      core = ClipboardSyncCore(backend: clipboard, clock: clock.call);
      await core.prime();
    });

    test('an image from the desktop is written once and not echoed', () async {
      final clip = ImageClip(png, width: 1, height: 1);
      expect(await core.applyRemoteClip(clip), isTrue);
      expect(clipboard.image, isNotNull);
      expect(core.observeLocalClip(clipboard.image), isNull);
    });

    test('the same image twice is a no-op', () async {
      await core.applyRemoteClip(ImageClip(png));
      expect(await core.applyRemoteClip(ImageClip(png)), isFalse);
    });

    test('switching between text and images propagates', () async {
      await core.applyRemoteClip(ImageClip(png));
      expect(core.observeLocal('typed after the image'), 'typed after the image');

      await core.applyRemote('remote text');
      expect(core.observeLocal('remote text'), isNull);

      final other = ImageClip(Uint8List.fromList([...png, 2]));
      expect(await core.applyRemoteClip(other), isTrue);
      expect(core.observeLocalClip(other), isNull);
    });

    test('a primed image is not announced', () async {
      final seeded = FakeClipboard(image: ImageClip(png));
      final other = ClipboardSyncCore(backend: seeded, clock: clock.call);
      await other.prime();
      expect(other.observeLocalClip(seeded.image), isNull);
    });

    test('copying the same image again later propagates', () async {
      await core.applyRemoteClip(ImageClip(png));
      expect(core.observeLocalClip(ImageClip(png)), isNull);

      clock.advance(const Duration(seconds: 30));
      core.observeLocal('something else');
      expect(core.observeLocalClip(ImageClip(png)), isNotNull);
    });

    test('images are read before text when both are present', () async {
      final seeded = FakeClipboard(text: 'caption', image: ImageClip(png));
      final other = ClipboardSyncCore(backend: seeded, clock: clock.call);
      expect(await other.readCurrentClip(), isA<ImageClip>());
    });

    test('a failed image write rolls back', () async {
      clipboard.failWrites = true;
      await expectLater(core.applyRemoteClip(ImageClip(png)), throwsA(isA<StateError>()));
      clipboard.failWrites = false;
      expect(core.observeLocal('initial'), isNull);
    });
  });
}
