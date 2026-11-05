import 'package:flutter/material.dart';

import 'services/connection_service.dart';
import 'sync_controller.dart';

const String _initialHost = String.fromEnvironment('CROSSCOPY_HOST');
const String _initialPort = String.fromEnvironment('CROSSCOPY_PORT', defaultValue: '8765');
const String _initialToken = String.fromEnvironment('CROSSCOPY_TOKEN');

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const CrossCopyApp());
}

class CrossCopyApp extends StatelessWidget {
  const CrossCopyApp({super.key});

  static const Color _seed = Color(0xFF4C6FFF);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'CrossCopy',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: _seed),
      ),
      darkTheme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: _seed, brightness: Brightness.dark),
      ),
      home: const HomeScreen(),
    );
  }
}

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  late final SyncController _sync;
  final TextEditingController _host = TextEditingController(text: _initialHost);
  final TextEditingController _port = TextEditingController(text: _initialPort);
  final TextEditingController _token = TextEditingController(text: _initialToken);

  @override
  void initState() {
    super.initState();
    _sync = SyncController()..addListener(_onSyncChanged);
  }

  void _onSyncChanged() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _sync
      ..removeListener(_onSyncChanged)
      ..dispose();
    _host.dispose();
    _port.dispose();
    _token.dispose();
    super.dispose();
  }

  Future<void> _toggleConnection() async {
    if (_sync.isConnected || _sync.status == SyncStatus.connecting) {
      await _sync.disconnect();
    } else {
      await _sync.connect(
        host: _host.text,
        port: _port.text,
        token: _token.text,
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final connected = _sync.isConnected;
    final busy = _sync.status == SyncStatus.connecting;

    return Scaffold(
      appBar: AppBar(
        titleSpacing: 20,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('CrossCopy'),
            Text(
              _sync.serverName ?? 'Universal clipboard',
              style: theme.textTheme.labelMedium?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
          ],
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: _StatusPill(status: _sync.status, latency: _sync.latency),
          ),
        ],
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 28),
          children: [
            _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Pairing', style: theme.textTheme.titleMedium),
                  const SizedBox(height: 4),
                  Text(
                    'Start the desktop app on your laptop, then enter the address it prints.',
                    style: theme.textTheme.bodySmall?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                  const SizedBox(height: 16),
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(
                        flex: 3,
                        child: TextField(
                          controller: _host,
                          enabled: !connected,
                          keyboardType: TextInputType.url,
                          autocorrect: false,
                          decoration: const InputDecoration(
                            labelText: 'Laptop IP',
                            hintText: '192.168.100.15',
                            border: OutlineInputBorder(),
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        flex: 2,
                        child: TextField(
                          controller: _port,
                          enabled: !connected,
                          keyboardType: TextInputType.number,
                          decoration: const InputDecoration(
                            labelText: 'Port',
                            border: OutlineInputBorder(),
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _token,
                    enabled: !connected,
                    autocorrect: false,
                    enableSuggestions: false,
                    decoration: const InputDecoration(
                      labelText: 'Pairing token (optional)',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 16),
                  SizedBox(
                    width: double.infinity,
                    child: connected
                        ? OutlinedButton.icon(
                            onPressed: _toggleConnection,
                            icon: const Icon(Icons.link_off),
                            label: const Text('Disconnect'),
                          )
                        : FilledButton.icon(
                            onPressed: busy ? null : _toggleConnection,
                            icon: busy
                                ? const SizedBox(
                                    width: 18,
                                    height: 18,
                                    child: CircularProgressIndicator(strokeWidth: 2),
                                  )
                                : const Icon(Icons.link),
                            label: Text(busy ? 'Connecting...' : 'Connect'),
                          ),
                  ),
                  if (_sync.target.isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text(
                      'Target: ws://${_sync.target}',
                      style: theme.textTheme.labelSmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                    ),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 16),
            _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Text('Last synced', style: theme.textTheme.titleMedium),
                      const Spacer(),
                      if (_sync.lastSource != null)
                        Chip(
                          visualDensity: VisualDensity.compact,
                          label: Text(_sync.lastSource == 'pc' ? 'from desktop' : 'from phone'),
                        ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  Container(
                    width: double.infinity,
                    constraints: const BoxConstraints(minHeight: 88),
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: theme.colorScheme.surfaceContainerHighest,
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: _sync.lastText == null
                        ? Text(
                            'Nothing synced yet.\nCopy text on either device.',
                            style: theme.textTheme.bodySmall?.copyWith(
                              color: theme.colorScheme.onSurfaceVariant,
                            ),
                          )
                        : SelectableText(
                            _displayText(_sync.lastText!),
                            style: theme.textTheme.bodyMedium,
                          ),
                  ),
                  const SizedBox(height: 10),
                  Text(
                    _sync.lastAt == null
                        ? 'Waiting for the first update'
                        : 'Updated ${_clock(_sync.lastAt!)}',
                    style: theme.textTheme.labelSmall?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                  const SizedBox(height: 6),
                  SizedBox(
                    width: double.infinity,
                    child: OutlinedButton.icon(
                      onPressed: connected ? _sync.pushCurrentClipboard : null,
                      icon: const Icon(Icons.upload, size: 18),
                      label: const Text('Push my clipboard'),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Activity', style: theme.textTheme.titleMedium),
                  const SizedBox(height: 8),
                  if (_sync.logEntries.isEmpty)
                    Text(
                      'No events yet.',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                    )
                  else
                    ..._sync.logEntries.take(8).map(
                          (entry) => Padding(
                            padding: const EdgeInsets.symmetric(vertical: 3),
                            child: Row(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  _clock(entry.at),
                                  style: theme.textTheme.labelSmall?.copyWith(
                                    fontFeatures: const [FontFeature.tabularFigures()],
                                    color: theme.colorScheme.onSurfaceVariant,
                                  ),
                                ),
                                const SizedBox(width: 10),
                                Expanded(
                                  child: Text(
                                    entry.message,
                                    style: theme.textTheme.bodySmall?.copyWith(
                                      color: entry.isError
                                          ? theme.colorScheme.error
                                          : theme.colorScheme.onSurface,
                                    ),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ),
                ],
              ),
            ),
            const SizedBox(height: 20),
            Text(
              'Android only lets apps read the clipboard while they are in the foreground, '
              'so CrossCopy watches for phone copies while this screen is open. '
              'Incoming text from the desktop is still written immediately.',
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
                height: 1.4,
              ),
            ),
          ],
        ),
      ),
    );
  }

  static String _displayText(String text) {
    const limit = 900;
    final trimmed = text.length <= limit ? text : '${text.substring(0, limit)}...';
    return trimmed.replaceAll('\r\n', '\n');
  }

  static String _clock(DateTime time) {
    String two(int value) => value.toString().padLeft(2, '0');
    return '${two(time.hour)}:${two(time.minute)}:${two(time.second)}';
  }
}

class _Card extends StatelessWidget {
  const _Card({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      elevation: 0,
      margin: EdgeInsets.zero,
      color: theme.colorScheme.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(18),
        side: BorderSide(color: theme.colorScheme.outlineVariant),
      ),
      child: Padding(padding: const EdgeInsets.all(16), child: child),
    );
  }
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.status, this.latency});

  final SyncStatus status;
  final Duration? latency;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final (label, dotColor) = switch (status) {
      SyncStatus.connected => ('Connected', const Color(0xFF2E9E5B)),
      SyncStatus.connecting => ('Connecting', const Color(0xFFD08B16)),
      SyncStatus.reconnecting => ('Reconnecting', const Color(0xFFD08B16)),
      SyncStatus.disconnected => ('Offline', theme.colorScheme.outline),
    };

    final detail = status == SyncStatus.connected && latency != null
        ? '${latency!.inMilliseconds} ms'
        : null;

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: dotColor, shape: BoxShape.circle),
          ),
          const SizedBox(width: 8),
          Text(
            detail == null ? label : '$label · $detail',
            style: theme.textTheme.labelSmall,
          ),
        ],
      ),
    );
  }
}
