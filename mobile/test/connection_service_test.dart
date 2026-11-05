import 'dart:async';
import 'dart:io';

import 'package:crosscopy/protocol.dart';
import 'package:crosscopy/services/connection_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// Minimal desktop stand-in: upgrades, answers `hello` with `welcome`.
class FakeDesktop {
  FakeDesktop._(this._server, this.port);

  final HttpServer _server;
  final int port;
  final List<WebSocket> _sockets = <WebSocket>[];
  int connections = 0;

  static Future<FakeDesktop> start() async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final desktop = FakeDesktop._(server, server.port);

    server.listen((request) async {
      final socket = await WebSocketTransformer.upgrade(request);
      desktop.connections++;
      desktop._sockets.add(socket);

      socket.listen((data) {
        final message = SyncMessage.decode(data);
        if (message.type == MessageTypes.hello) {
          socket.add(
            SyncMessage(
              type: MessageTypes.welcome,
              origin: 'desktop-test',
              data: {'server_id': 'desktop-test', 'name': 'Test PC', 'protocol': 1},
            ).encode(),
          );
        }
      }, onError: (_) {}, cancelOnError: true);
    });

    return desktop;
  }

  Future<void> dropConnections() async {
    for (final socket in List<WebSocket>.of(_sockets)) {
      await socket.close(WebSocketStatus.goingAway, 'test drop');
    }
    _sockets.clear();
  }

  Future<void> close() async {
    await dropConnections();
    await _server.close(force: true);
  }
}

void main() {
  test('connects, handshakes and reconnects after the socket drops', () async {
    final desktop = await FakeDesktop.start();
    final service = ConnectionService(
      clientId: 'android-test',
      deviceName: 'Test phone',
      onLog: (_, {bool isError = false}) {},
    );

    try {
      final firstConnect = Completer<void>();
      service.statuses.listen((status) {
        if (status == SyncStatus.connected && !firstConnect.isCompleted) {
          firstConnect.complete();
        }
      });

      await service.connect(Uri.parse('ws://127.0.0.1:${desktop.port}'));
      await firstConnect.future.timeout(const Duration(seconds: 5));
      expect(service.status, SyncStatus.connected);
      expect(desktop.connections, 1);

      // Kill the server side of the socket: the service must come back by itself.
      final reconnect = Completer<void>();
      service.statuses.listen((status) {
        if (status == SyncStatus.connected && !reconnect.isCompleted) {
          reconnect.complete();
        }
      });
      await desktop.dropConnections();

      await reconnect.future.timeout(const Duration(seconds: 10));
      expect(desktop.connections, greaterThanOrEqualTo(2));
      expect(service.status, SyncStatus.connected);
    } finally {
      await service.dispose();
      await desktop.close();
    }
  }, timeout: const Timeout(Duration(seconds: 30)));

  test('reports a failure instead of throwing when nothing is listening', () async {
    final service = ConnectionService(
      clientId: 'android-test',
      deviceName: 'Test phone',
      onLog: (_, {bool isError = false}) {},
    );

    try {
      // Port 1 is not something a test process can bind, so this always fails.
      await service.connect(Uri.parse('ws://127.0.0.1:1'));
      expect(service.status, isNot(SyncStatus.connected));
      await service.disconnect();
      expect(service.status, SyncStatus.disconnected);
    } finally {
      await service.dispose();
    }
  }, timeout: const Timeout(Duration(seconds: 30)));
}
