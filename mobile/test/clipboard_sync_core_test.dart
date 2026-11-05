import 'package:crosscopy/protocol.dart';
import 'package:crosscopy/services/clipboard_service.dart';
import 'package:flutter_test/flutter_test.dart';

class FakeClipboard implements ClipboardBackend {
  FakeClipboard([this.value]);

  String? value;
  final List<String> writes = <String>[];
  bool failWrites = false;

  @override
  Future<String?> readText() async => value;

  @override
  Future<void> writeText(String text) async {
    if (failWrites) throw StateError('clipboard locked');
    writes.add(text);
    value = text;
  }
}

class FakeClock {
  DateTime now = DateTime.utc(2026, 1, 1, 12);

  DateTime call() => now;

  void advance(Duration duration) => now = now.add(duration);
}

void main() {
  late FakeClipboard clipboard;
  late FakeClock clock;
  late ClipboardSyncCore core;

  setUp(() async {
    clipboard = FakeClipboard('initial');
    clock = FakeClock();
    core = ClipboardSyncCore(backend: clipboard, clock: clock.call);
    await core.prime();
  });

  group('echo cancellation', () {
    test('primed clipboard is not announced', () {
      expect(core.observeLocal('initial'), isNull);
    });

    test('a genuine local copy is returned exactly once', () {
      expect(core.observeLocal('copied on the phone'), 'copied on the phone');
      expect(core.observeLocal('copied on the phone'), isNull);
    });

    test('text applied from the desktop is never pushed back', () async {
      expect(await core.applyRemote('from the desktop'), isTrue);
      expect(clipboard.value, 'from the desktop');
      expect(core.observeLocal('from the desktop'), isNull);
    });

    test('CRLF and LF versions are the same content', () async {
      await core.applyRemote('line one\nline two');
      expect(core.observeLocal('line one\r\nline two'), isNull);
    });

    test('a poll captured before the remote write is dropped', () async {
      // The timer read the old value, the remote write landed, poll result arrives.
      final staleRead = clipboard.value;
      await core.applyRemote('new value');
      expect(staleRead, 'initial');
      expect(core.observeLocal(staleRead!), isNull);
    });

    test('applying the same text twice is a no-op', () async {
      expect(await core.applyRemote('same'), isTrue);
      expect(await core.applyRemote('same'), isFalse);
      expect(clipboard.writes, hasLength(1));
    });

    test('the same text can be copied again after the window expires', () async {
      await core.applyRemote('shared text');
      clock.advance(const Duration(seconds: 6));
      expect(core.observeLocal('intermediate'), 'intermediate');
      expect(core.observeLocal('shared text'), 'shared text');
    });

    test('a failed write leaves state consistent', () async {
      clipboard.failWrites = true;
      await expectLater(core.applyRemote('cannot apply'), throwsA(isA<StateError>()));

      clipboard.failWrites = false;
      expect(core.observeLocal('initial'), isNull);
      expect(core.observeLocal('local copy'), 'local copy');
    });

    test('oversized text is ignored', () {
      final small = ClipboardSyncCore(backend: clipboard, maxBytes: 8);
      expect(small.observeLocal('0123456789'), isNull);
    });

    test('empty clipboard content is ignored', () {
      expect(core.observeLocal(''), isNull);
    });
  });

  group('protocol', () {
    test('digest ignores line ending differences', () {
      expect(textDigest('a\r\nb'), textDigest('a\nb'));
      expect(textDigest('a'), isNot(textDigest('A')));
    });

    test('round-trips a clipboard update', () {
      final message = SyncMessage.clipboardUpdate(origin: 'android-1', text: 'héllo');
      final decoded = SyncMessage.decode(message.encode());
      expect(decoded.type, MessageTypes.clipboardUpdate);
      expect(decoded.origin, 'android-1');
      expect(decoded.clipboardText, 'héllo');
      expect(decoded.id, message.id);
    });

    test('rejects a different protocol version', () {
      expect(
        () => SyncMessage.decode('{"v":2,"type":"hello","data":{}}'),
        throwsA(isA<ProtocolException>()),
      );
    });

    test('rejects frames that are not objects', () {
      expect(() => SyncMessage.decode('[1,2,3]'), throwsA(isA<ProtocolException>()));
      expect(() => SyncMessage.decode('not json'), throwsA(isA<ProtocolException>()));
    });

    test('unknown clipboard kinds are not mistaken for text', () {
      final message = SyncMessage(
        type: MessageTypes.clipboardUpdate,
        data: {'kind': 'files', 'text': 'ignored'},
      );
      expect(message.clipboardText, isNull);
    });
  });
}
