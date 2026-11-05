import 'dart:async';
import 'dart:math';

import 'package:web_socket_channel/status.dart' as ws_status;
import 'package:web_socket_channel/web_socket_channel.dart';

import '../protocol.dart';

enum SyncStatus { disconnected, connecting, connected, reconnecting }

/// Owns the websocket: handshake, heartbeat and reconnect with backoff.
class ConnectionService {
  ConnectionService({
    required this.clientId,
    required this.deviceName,
    required this.onLog,
  });

  final String clientId;
  final String deviceName;
  final void Function(String message, {bool isError}) onLog;

  static const Duration _handshakeTimeout = Duration(seconds: 8);
  static const Duration _heartbeatInterval = Duration(seconds: 15);
  static const Duration _silenceLimit = Duration(seconds: 45);
  static const Duration _connectTimeout = Duration(seconds: 8);
  static const Duration _teardownGrace = Duration(seconds: 2);

  final StreamController<SyncMessage> _messages = StreamController<SyncMessage>.broadcast();
  final StreamController<SyncStatus> _statuses = StreamController<SyncStatus>.broadcast();

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _subscription;
  Timer? _reconnectTimer;
  Timer? _heartbeatTimer;
  Timer? _handshakeTimer;
  Uri? _uri;
  int _attempt = 0;
  int _generation = 0;
  int _pingSequence = 0;
  DateTime? _lastInboundAt;
  DateTime? _pingSentAt;
  Duration? _latency;
  bool _shouldReconnect = false;
  bool _disposed = false;
  SyncStatus _status = SyncStatus.disconnected;

  SyncStatus get status => _status;
  bool get isConnected => _status == SyncStatus.connected;
  Duration? get latency => _latency;
  Stream<SyncMessage> get messages => _messages.stream;
  Stream<SyncStatus> get statuses => _statuses.stream;

  Future<void> connect(Uri uri) async {
    _uri = uri;
    _shouldReconnect = true;
    _attempt = 0;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _generation++;
    await _teardown();
    await _open();
  }

  Future<void> disconnect() async {
    _shouldReconnect = false;
    _generation++;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    await _teardown();
    _setStatus(SyncStatus.disconnected);
    onLog('Disconnected');
  }

  void send(SyncMessage message) {
    final channel = _channel;
    if (channel == null) return;
    try {
      channel.sink.add(message.encode());
    } catch (error) {
      onLog('Could not send: $error', isError: true);
    }
  }

  // -- connection lifecycle ---------------------------------------------

  Future<void> _open() async {
    final uri = _uri;
    if (uri == null || !_shouldReconnect || _disposed) return;

    final generation = ++_generation;
    _setStatus(_attempt == 0 ? SyncStatus.connecting : SyncStatus.reconnecting);
    onLog('Connecting to $uri');

    final channel = WebSocketChannel.connect(uri);
    _channel = channel;

    try {
      await channel.ready.timeout(_connectTimeout);
    } catch (error) {
      if (generation != _generation) return;
      onLog('Connection failed: ${_describe(error)}', isError: true);
      await _teardown();
      _scheduleReconnect();
      return;
    }

    if (generation != _generation || !_shouldReconnect) {
      // A newer connect()/disconnect() superseded this attempt.
      try {
        await channel.sink.close(ws_status.normalClosure);
      } catch (_) {}
      return;
    }

    _lastInboundAt = DateTime.now();
    _subscription = channel.stream.listen(
      _handleFrame,
      onError: (Object error) => _handleSocketClosed('Connection error: ${_describe(error)}'),
      onDone: () => _handleSocketClosed('Connection closed by the desktop'),
      cancelOnError: true,
    );

    _handshakeTimer?.cancel();
    _handshakeTimer = Timer(_handshakeTimeout, () {
      _handleSocketClosed('Handshake timed out (is the token correct?)');
    });

    _startHeartbeat();
    send(SyncMessage.hello(clientId: clientId, name: deviceName));
  }

  void _handleFrame(Object? raw) {
    _lastInboundAt = DateTime.now();

    final SyncMessage message;
    try {
      message = SyncMessage.decode(raw);
    } on ProtocolException catch (error) {
      onLog('Ignoring malformed frame (${error.code})', isError: true);
      return;
    }

    if (message.type == MessageTypes.welcome) {
      _handshakeTimer?.cancel();
      _handshakeTimer = null;
      _attempt = 0;
      _setStatus(SyncStatus.connected);
      final name = message.data['name'];
      onLog('Handshake complete${name is String ? ' with $name' : ''}');
    } else if (message.type == MessageTypes.pong) {
      final sentAt = _pingSentAt;
      if (sentAt != null) {
        _latency = DateTime.now().difference(sentAt);
        _pingSentAt = null;
      }
    }

    if (!_messages.isClosed) _messages.add(message);
  }

  Future<void> _handleSocketClosed(String reason) async {
    if (!_shouldReconnect || _disposed) return;
    onLog(reason, isError: true);
    await _teardown();
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (!_shouldReconnect || _disposed || _reconnectTimer != null) return;

    _attempt++;
    final seconds = min(30, pow(1.6, min(_attempt, 8)).round());
    final jitter = Random().nextInt(500);
    final delay = Duration(milliseconds: seconds * 1000 + jitter);

    _setStatus(SyncStatus.reconnecting);
    onLog('Retrying in ${delay.inMilliseconds / 1000}s (attempt $_attempt)');

    _reconnectTimer = Timer(delay, () {
      _reconnectTimer = null;
      unawaited(_open());
    });
  }

  void _startHeartbeat() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer = Timer.periodic(_heartbeatInterval, (_) {
      final lastInbound = _lastInboundAt;
      if (lastInbound == null) return;

      if (DateTime.now().difference(lastInbound) > _silenceLimit) {
        onLog('Desktop stopped responding; reconnecting', isError: true);
        unawaited(_handleSocketClosed('Heartbeat timeout'));
        return;
      }

      _pingSentAt = DateTime.now();
      send(SyncMessage.ping(_pingSequence++));
    });
  }

  Future<void> _teardown() async {
    _handshakeTimer?.cancel();
    _handshakeTimer = null;
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;

    final subscription = _subscription;
    _subscription = null;
    try {
      await subscription?.cancel().timeout(_teardownGrace);
    } catch (_) {
      // Already gone, or a socket that never connected never answers.
    }

    final channel = _channel;
    _channel = null;
    if (channel != null) {
      try {
        await channel.sink.close(ws_status.normalClosure).timeout(_teardownGrace);
      } catch (_) {
        // Closing a socket that never completed its handshake can hang forever.
      }
    }
  }

  void _setStatus(SyncStatus next) {
    if (_status == next || _disposed) return;
    _status = next;
    if (!_statuses.isClosed) _statuses.add(next);
  }

  Future<void> dispose() async {
    _disposed = true;
    _shouldReconnect = false;
    _reconnectTimer?.cancel();
    await _teardown();
    await _messages.close();
    await _statuses.close();
  }

  static String _describe(Object error) {
    final text = error.toString();
    if (text.contains('401')) return 'Unauthorized - check the pairing token';
    if (text.contains('Connection refused')) return 'Connection refused - is the desktop app running?';
    if (text.contains('not upgraded to websocket')) {
      return 'Desktop refused the connection - check the token and that the desktop app is running';
    }
    return text;
  }
}
