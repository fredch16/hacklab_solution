package dev.localvision.local_vision

import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaCodecInfo
import java.nio.ByteBuffer
import java.util.concurrent.Executors

interface AudioSink {
    fun onFormat(format: MediaFormat)
    /** [ptsUs] on the elapsedRealtime clock. */
    fun onSample(data: ByteBuffer, info: MediaCodec.BufferInfo, ptsUs: Long)
    fun onEnded()
    fun onError(t: Throwable)
}

/** AAC-LC encoder fed by the AudioCaptureBus. */
class AacEncoder(private val sampleRate: Int, private val channels: Int, private val sink: AudioSink) : PcmSubscriber {
    private val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
    private val drain = Executors.newSingleThreadExecutor()
    @Volatile private var closed = false
    private var lastPts = 0L

    init {
        val f = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, channels).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, 96_000 * channels)
        }
        codec.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        codec.start()
        drain.execute { drainLoop() }
    }

    override fun onPcm(data: ByteArray, size: Int, ptsUs: Long) {
        if (closed) return
        try {
            var off = 0
            while (off < size && !closed) {
                val i = codec.dequeueInputBuffer(10_000)
                if (i < 0) return // encoder busy: drop (never block the mic thread for long)
                val b = codec.getInputBuffer(i)!!
                b.clear()
                val n = minOf(b.remaining(), size - off)
                b.put(data, off, n)
                codec.queueInputBuffer(i, 0, n, ptsUs + off * 1_000_000L / (sampleRate * 2 * channels), 0)
                off += n
            }
        } catch (t: Throwable) { if (!closed) sink.onError(t) }
    }

    /** Flush: queue EOS; the drain loop ends when the encoder is done. */
    fun finish() {
        if (closed) return
        closed = true
        try {
            val i = codec.dequeueInputBuffer(200_000)
            if (i >= 0) codec.queueInputBuffer(i, 0, 0, lastPts, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
        } catch (_: Throwable) {}
    }

    private fun drainLoop() {
        val info = MediaCodec.BufferInfo()
        try {
            while (true) {
                val i = codec.dequeueOutputBuffer(info, 10_000)
                when {
                    i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> sink.onFormat(codec.outputFormat)
                    i >= 0 -> {
                        val eos = info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                        if (info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                            lastPts = info.presentationTimeUs
                            sink.onSample(codec.getOutputBuffer(i)!!, info, info.presentationTimeUs)
                        }
                        codec.releaseOutputBuffer(i, false)
                        if (eos) break
                    }
                }
            }
        } catch (t: Throwable) {
            if (!closed) sink.onError(t)
        } finally {
            try { codec.stop() } catch (_: Throwable) {}
            try { codec.release() } catch (_: Throwable) {}
            drain.shutdown()
            sink.onEnded()
        }
    }
}
