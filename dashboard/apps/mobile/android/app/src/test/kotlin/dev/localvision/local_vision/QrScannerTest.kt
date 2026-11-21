package dev.localvision.local_vision

import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import java.nio.ByteBuffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class QrScannerTest {
    private fun render(text: String, size: Int): ByteArray {
        val m = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size)
        return ByteArray(size * size) { i -> if (m.get(i % size, i / size)) 0 else 255.toByte() }
    }

    @Test
    fun decodesPairingQr() {
        val text = "lvc-pair:1:AbCdEfGhIjKlMnOpQrStUv"
        assertEquals(text, QrScanner().decode(render(text, 300), 300, 300))
    }

    @Test
    fun decodesThroughRowPadding() {
        val text = "lvc-pair:1:AbCdEfGhIjKlMnOpQrStUv"
        val s = 300
        val tight = render(text, s)
        val stride = s + 20
        val padded = ByteArray(stride * s)
        for (r in 0 until s) System.arraycopy(tight, r * s, padded, r * stride, s)
        val out = QrScanner.tightLuma(ByteBuffer.wrap(padded), stride, s, s)
        assertEquals(text, QrScanner().decode(out, s, s))
    }

    @Test
    fun returnsNullWhenNoCode() {
        assertNull(QrScanner().decode(ByteArray(100 * 100) { 128.toByte() }, 100, 100))
    }
}
