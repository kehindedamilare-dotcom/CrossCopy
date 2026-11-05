import 'package:crosscopy/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('renders the pairing screen at phone size without overflow', (tester) async {
    await tester.binding.setSurfaceSize(const Size(360, 720));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(const CrossCopyApp());
    await tester.pump();

    expect(find.text('CrossCopy'), findsOneWidget);
    expect(find.text('Laptop IP'), findsOneWidget);
    expect(find.text('Pairing token (optional)'), findsOneWidget);
    expect(find.text('Connect'), findsOneWidget);
    expect(find.text('Push my clipboard'), findsOneWidget);
    expect(find.text('Nothing synced yet.\nCopy text on either device.'), findsOneWidget);

    expect(tester.takeException(), isNull);
  });
}
