import 'dart:async';
import 'dart:collection';
import 'dart:math';

import 'package:flutter/widgets.dart';

import 'protocol.dart';
import 'services/clipboard_service.dart';
import 'services/connection_service.dart';

class LogEntry {
  LogEntry(this.message, {this.isError = false}) : at = DateTime.now();

  final DateTime at;
  final String message;
  final bool isError;
}

/// Glue between the websocket, the clipboard and the UI.
///
/// This is where the phone side of loop prevention lives: every remote write
/// goes through [ClipboardSyncCore], and the poller asks that same object
/// whether a poll result is genuinely new.
class SyncController extends ChangeNotifier with WidgetsBindingObserver {
  SyncController({ClipboardSyncCore? clipboard})
      : _clipboard = clipboard ?? ClipboardSyncCore() {
    _connection = ConnectionService(
      clientId: _clientId,
      deviceName: 'Android phone',
      onLog: _log,
    );
    _messageSubscription = _connection.messages.listen(_handleMessage);
    _statusSubscription = _connection.statuses.listen(_handleStatus);
    WidgetsBinding.instance.addObserver(this);
  }

  static const Duration _pollInterval = Duration(milliseconds: 600);
  static const int _maxLogEntries = 60;
  static const int _maxSeenIds = 128;

  final ClipboardSyncCore _clipboard;
  late final ConnectionService _connection;
  StreamSubscription<SyncMessage>? _messageSubscription;
  StreamSubscription<SyncStatus>? _statusSubscription;
  Timer? _pollTimer;

  final String _clientId = _randomClientId();
  final LinkedHashSet<String> _seenIds = LinkedHashSet<String>();
  final List<LogEntry> _logEntries = <LogEntry>[];

  SyncStatus _status = SyncStatus.disconnected;
  bool _monitoring = true;
  bool _polling = false;
  bool _disposed = false;
  String? _serverName;
  ClipContent? _lastClip;
  String? _lastSource;
  DateTime? _lastAt;
  String _connectedHost = '';
  int _connectedPort = 8765;

  SyncStatus get status => _status;
  String? get serverName => _serverName;
  ClipContent? get lastClip => _lastClip;
  String? get lastText => _lastClip is TextClip ? (_lastClip! as TextClip).text : null;
  String? get lastSource => _lastSource;
  DateTime? get lastAt => _lastAt;
  Duration? get latency => _connection.latency;
  String get target => _connectedHost.isEmpty ? '' : '$_connectedHost:$_connectedPort';
  bool get isConnected => _status == SyncStatus.connected;
  List<LogEntry> get logEntries => List<LogEntry>.unmodifiable(_logEntries);
  bool get isMonitoring => _monitoring;

  Future<void> connect({
    required String host,
    required String port,
    required String token,
  }) async {
    final trimmedHost = host.trim();
    final portNumber = int.tryParse(port.trim());
    final trimmedToken = token.trim();

    if (trimmedHost.isEmpty) {
      _log('Enter the laptop IP address first', isError: true);
      return;
    }
    if (portNumber == null || portNumber < 1 || portNumber > 65535) {
      _log('Port must be a number between 1 and 65535', isError: true);
      return;
    }

    _connectedHost = trimmedHost;
    _connectedPort = portNumber;

    final uri = Uri(
      scheme: 'ws',
      host: trimmedHost,
      port: portNumber,
      queryParameters: trimmedToken.isEmpty ? null : {'token': trimmedToken},
    );
    await _connection.connect(uri);
  }

  Future<void> disconnect() async {
    _stopPolling();
    await _connection.disconnect();
    _serverName = null;
    _notify();
  }

  /// Manual push, for content copied while the phone was disconnected.
  Future<void> pushCurrentClipboard() async {
    if (!isConnected) {
      _log('Not connected', isError: true);
      return;
    }

    final clip = await _clipboard.readCurrentClip();
    if (clip == null) {
      _log('Clipboard is empty or unreadable', isError: true);
      return;
    }

    _connection.send(SyncMessage.clipboardContentUpdate(origin: _clientId, clip: clip));
    _clipboard.observeLocalClip(clip);
    _remember(clip, 'phone');
    _log('Pushed ${clip.describe()} to desktop');
  }

  // -- websocket events --------------------------------------------------

  void _handleStatus(SyncStatus status) {
    _status = status;
    if (status == SyncStatus.connected) {
      unawaited(_startPolling());
    } else {
      _stopPolling();
    }
    _notify();
  }

  Future<void> _handleMessage(SyncMessage message) async {
    switch (message.type) {
      case MessageTypes.welcome:
        final name = message.data['name'];
        _serverName = name is String ? name : null;
        _notify();
      case MessageTypes.clipboardUpdate:
      case MessageTypes.clipboardSnapshot:
        await _applyRemote(message);
      case MessageTypes.error:
        final detail = message.data['detail'] ?? message.data['code'] ?? 'unknown error';
        _log('Desktop error: $detail', isError: true);
      case MessageTypes.pong:
        _notify(); // refresh the latency badge
      case MessageTypes.peerJoined:
      case MessageTypes.peerLeft:
        _log(message.type == MessageTypes.peerJoined
            ? 'Another device joined'
            : 'Another device left');
      default:
        break;
    }
  }

  Future<void> _applyRemote(SyncMessage message) async {
    if (message.origin == _clientId) return; // our own event bounced back

    if (!_seenIds.add(message.id)) return;
    if (_seenIds.length > _maxSeenIds) {
      _seenIds.remove(_seenIds.first);
    }

    final clip = message.clipboardContent;
    if (clip == null) {
      _log('Ignored an unsupported clipboard payload', isError: true);
      return;
    }

    try {
      final applied = await _clipboard.applyRemoteClip(clip);
      if (!applied) return;
      _remember(clip, 'pc');
      _log('Received ${_preview(clip)}');
    } catch (error) {
      _log('Could not update the clipboard: $error', isError: true);
    }
  }

  // -- clipboard polling -------------------------------------------------

  Future<void> _startPolling() async {
    if (_pollTimer != null) return;

    // Whatever is on the clipboard right now is not news; the desktop's
    // connect snapshot may be about to replace it.
    await _clipboard.prime();
    if (!isConnected || _disposed) return;

    _pollTimer = Timer.periodic(_pollInterval, (_) => unawaited(_pollOnce()));
    _notify();
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  Future<void> _pollOnce() async {
    if (!isConnected || !_monitoring || _polling || _disposed) return;

    _polling = true;
    try {
      final clip = await _clipboard.readCurrentClip();
      if (clip == null) return;

      final fresh = _clipboard.observeLocalClip(clip);
      if (fresh == null) return; // unchanged, or the echo of a remote write

      _connection.send(SyncMessage.clipboardContentUpdate(origin: _clientId, clip: fresh));
      _remember(fresh, 'phone');
      _log('Sent ${_preview(fresh)}');
    } finally {
      _polling = false;
    }
  }

  // -- Android lifecycle -------------------------------------------------

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final resumed = state == AppLifecycleState.resumed;
    if (resumed == _monitoring) return;
    _monitoring = resumed;
    _log(resumed
        ? 'Foreground: watching the clipboard'
        : 'Backgrounded: Android blocks clipboard reads until you return',
        isError: !resumed);
    _notify();
  }

  // -- helpers -----------------------------------------------------------

  void _remember(ClipContent clip, String source) {
    _lastClip = clip;
    _lastSource = source;
    _lastAt = DateTime.now();
    _notify();
  }

  void _log(String message, {bool isError = false}) {
    _logEntries.insert(0, LogEntry(message, isError: isError));
    if (_logEntries.length > _maxLogEntries) {
      _logEntries.removeRange(_maxLogEntries, _logEntries.length);
    }
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  static String _preview(ClipContent clip) {
    if (clip is! TextClip) return clip.describe();
    final compact = clip.text.replaceAll(RegExp(r'\s+'), ' ').trim();
    final short = compact.length <= 40 ? compact : '${compact.substring(0, 40)}...';
    return '${clip.text.length} chars: $short';
  }

  static String _randomClientId() {
    final random = Random.secure();
    return 'android-${List<int>.generate(6, (_) => random.nextInt(256)).map((b) => b.toRadixString(16).padLeft(2, '0')).join()}';
  }

  @override
  void dispose() {
    _disposed = true;
    WidgetsBinding.instance.removeObserver(this);
    _stopPolling();
    unawaited(_messageSubscription?.cancel());
    unawaited(_statusSubscription?.cancel());
    unawaited(_connection.dispose());
    super.dispose();
  }
}
