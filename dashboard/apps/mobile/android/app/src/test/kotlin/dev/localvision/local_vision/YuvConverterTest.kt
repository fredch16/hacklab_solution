package dev.localvision.local_vision

import java.nio.ByteBuffer
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class YuvConverterTest {
    private fun bytes(b: ByteBuffer, off: Int, n: Int): ByteArray {
        val d = b.duplicate()
        d.position(off)
        return ByteArray(n).also { d.get(it) }
    }

    @Test
    fun copiesYWithRowPaddingDropped() {
        // 4x2 image, rowStride 6 (2 bytes of padding per row)
        val src = ByteBuffer.wrap(byteArrayOf(1, 2, 3, 4, 99, 99, 5, 6, 7, 8))
        val dst = ByteBuffer.allocateDirect(8)
        YuvConverter.copyPlane(Plane(src, 6, 1), 4, 2, dst, 4)
        assertArrayEquals(byteArrayOf(1, 2, 3, 4, 5, 6, 7, 8), bytes(dst, 0, 8))
    }

    @Test
    fun deinterleavesSemiPlanarChroma() {
        // 4x2 luma, chroma 2x1 with pixelStride 2 (NV12-style: U at even, V at odd offsets)
        val uv = byteArrayOf(10, 20, 11, 21)
        val u = Plane(ByteBuffer.wrap(uv), 4, 2)
        val v = Plane(ByteBuffer.wrap(uv, 1, 3).slice(), 4, 2)
        val y = Plane(ByteBuffer.wrap(ByteArray(8) { it.toByte() }), 4, 1)
        val dY = ByteBuffer.allocateDirect(8)
        val dU = ByteBuffer.allocateDirect(2)
        val dV = ByteBuffer.allocateDirect(2)
        YuvConverter.toI420(4, 2, y, u, v, dY, 4, dU, 2, dV, 2)
        assertArrayEquals(byteArrayOf(10, 11), bytes(dU, 0, 2))
        assertArrayEquals(byteArrayOf(20, 21), bytes(dV, 0, 2))
        assertEquals(7, dY.get(7).toInt())
    }

    @Test
    fun odd_dimensions_round_chroma_up() {
        val w = 5; val h = 3
        val y = Plane(ByteBuffer.wrap(ByteArray(w * h) { 1 }), w, 1)
        val c = Plane(ByteBuffer.wrap(ByteArray(3 * 2) { 2 }), 3, 1)
        val dY = ByteBuffer.allocateDirect(w * h)
        val dU = ByteBuffer.allocateDirect(6)
        val dV = ByteBuffer.allocateDirect(6)
        YuvConverter.toI420(w, h, y, c, c, dY, w, dU, 3, dV, 3)
        assertEquals(2, dU.get(5).toInt())
    }
}
