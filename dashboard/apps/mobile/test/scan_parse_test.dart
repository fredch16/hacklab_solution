import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/ui/scan_page.dart';

void main() {
  test('parsePairQr accepts only lvc-pair:1:<22 char id>', () {
    const id = 'AbCdEfGhIjKlMnOpQrStUv';
    expect(parsePairQr('lvc-pair:1:$id'), id);
    expect(parsePairQr('lvc-pair:2:$id'), isNull);
    expect(parsePairQr('https://10.0.0.1:8443'), isNull);
    expect(parsePairQr('lvc-pair:1:short'), isNull);
    expect(parsePairQr('lvc-pair:1:$id/extra'), isNull);
    expect(parsePairQr(''), isNull);
  });
}
