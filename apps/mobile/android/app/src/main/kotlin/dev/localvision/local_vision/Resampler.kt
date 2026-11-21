package dev.localvision.local_vision

import kotlin.math.PI
import kotlin.math.sin

/**
 * Streaming 48 kHz -> 16 kHz mono: PCM16 LE interleaved in, float [-1,1] out.
 * Downmix, 31-tap Hann-windowed sinc low-pass (7 kHz), keep every 3rd sample.
 */
class Resampler(private val channels: Int, private val factor: Int = 3) {
    private val taps = 31
    private val h = FloatArray(taps).also { k ->
        val fc = 7000.0 / 48000.0
        val m = taps - 1
        var sum = 0.0
        for (i in 0 until taps) {
            val x = i - m / 2.0
            val sinc = if (x == 0.0) 2 * fc else sin(2 * PI * fc * x) / (PI * x)
            val w = 0.5 - 0.5 * kotlin.math.cos(2 * PI * i / m)
            k[i] = (sinc * w).toFloat(); sum += k[i]
        }
        for (i in 0 until taps) k[i] = (k[i] / sum).toFloat() // unity DC gain
    }
    private val hist = FloatArray(taps)
    private var pos = 0
    private var phase = 0

    fun process(pcm: ByteArray, size: Int): FloatArray {
        val frames = size / (2 * channels)
        val out = FloatArray(frames / factor + 1)
        var n = 0
        for (f in 0 until frames) {
            var acc = 0
            for (c in 0 until channels) {
                val i = (f * channels + c) * 2
                acc += ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xff)).toShort().toInt()
            }
            hist[pos] = acc / (channels * 32768f)
            pos = (pos + 1) % taps
            if (++phase == factor) {
                phase = 0
                var y = 0f
                for (k in 0 until taps) y += h[k] * hist[(pos + taps - 1 - k) % taps]
                out[n++] = y
            }
        }
        return out.copyOf(n)
    }
}
