package dev.localvision.local_vision

import java.nio.ByteBuffer

/** One plane of a YUV_420_888 image (strides as reported by ImageProxy). */
class Plane(val buffer: ByteBuffer, val rowStride: Int, val pixelStride: Int)

/** Stride-aware YUV_420_888 -> planar I420 copy. Pure JVM so it is unit-testable. */
object YuvConverter {
    fun copyPlane(src: Plane, width: Int, height: Int, dst: ByteBuffer, dstStride: Int) {
        val s = src.buffer.duplicate()
        val d = dst.duplicate()
        val row = ByteArray(width)
        for (r in 0 until height) {
            val base = r * src.rowStride
            if (src.pixelStride == 1) {
                s.position(base)
                s.get(row, 0, width)
            } else {
                for (x in 0 until width) row[x] = s.get(base + x * src.pixelStride)
            }
            d.position(r * dstStride)
            d.put(row, 0, width)
        }
    }

    fun toI420(
        width: Int, height: Int,
        y: Plane, u: Plane, v: Plane,
        dstY: ByteBuffer, strideY: Int,
        dstU: ByteBuffer, strideU: Int,
        dstV: ByteBuffer, strideV: Int,
    ) {
        val cw = (width + 1) / 2
        val ch = (height + 1) / 2
        copyPlane(y, width, height, dstY, strideY)
        copyPlane(u, cw, ch, dstU, strideU)
        copyPlane(v, cw, ch, dstV, strideV)
    }
}
