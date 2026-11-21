package dev.localvision.local_vision

import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer

/** QR-only ZXing decode of a tightly packed luma (Y) plane. Pure JVM. */
class QrScanner {
    private val reader = MultiFormatReader().apply {
        setHints(
            mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
                DecodeHintType.TRY_HARDER to true,
            )
        )
    }

    /** Returns the decoded text or null. QR finder patterns are rotation invariant. */
    fun decode(luma: ByteArray, width: Int, height: Int): String? {
        return try {
            val src = PlanarYUVLuminanceSource(luma, width, height, 0, 0, width, height, false)
            reader.decodeWithState(BinaryBitmap(HybridBinarizer(src))).text
        } catch (_: Exception) {
            null
        } finally {
            reader.reset()
        }
    }

    companion object {
        /** Copies the Y plane dropping row padding (rowStride >= width, pixelStride 1). */
        fun tightLuma(buf: java.nio.ByteBuffer, rowStride: Int, width: Int, height: Int): ByteArray {
            val out = ByteArray(width * height)
            val b = buf.duplicate()
            for (r in 0 until height) {
                b.position(r * rowStride)
                b.get(out, r * width, width)
            }
            return out
        }
    }
}
