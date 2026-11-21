package dev.localvision.local_vision

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.util.Log
import android.view.Surface
import java.nio.ByteBuffer
import java.util.concurrent.Executors

class VideoSpec(
    val mime: String, // video/hevc | video/avc
    val width: Int,
    val height: Int,
    val fps: Int,
    val bitrate: Int,
)

/** Receives encoded video. Called on the encoder drain thread. */
interface VideoSink {
    fun onFormat(format: MediaFormat)
    /** [ptsUs] is on the elapsedRealtime clock (microseconds): camera sensor time. */
    fun onSample(data: ByteBuffer, info: MediaCodec.BufferInfo, ptsUs: Long)
    fun onEnded()
    fun onError(code: String, t: Throwable?)
}

/** MediaCodec surface encoder; the camera session draws into [inputSurface]. */
class VideoEncoder(private val spec: VideoSpec, private val sink: VideoSink) {
    private val codec: MediaCodec = MediaCodec.createEncoderByType(spec.mime)
    val inputSurface: Surface
    private val drain = Executors.newSingleThreadExecutor()
    @Volatile private var stopping = false

    init {
        val f = MediaFormat.createVideoFormat(spec.mime, spec.width, spec.height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, spec.bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, spec.fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
        }
        try {
            codec.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            inputSurface = codec.createInputSurface()
        } catch (e: Throwable) {
            codec.release()
            throw e
        }
    }

    fun start() {
        codec.start()
        drain.execute { drainLoop() }
    }

    private fun drainLoop() {
        val info = MediaCodec.BufferInfo()
        var first = true
        try {
            while (true) {
                val i = codec.dequeueOutputBuffer(info, 10_000)
                when {
                    i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> sink.onFormat(codec.outputFormat)
                    i >= 0 -> {
                        val buf = codec.getOutputBuffer(i)!!
                        val eos = info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                        if (info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                            if (first) { first = false; Log.i("LVC", "first video pts=${info.presentationTimeUs}us") }
                            // The camera's sensor timestamps are REALTIME (boottime) on this device class;
                            // CameraSession verifies SENSOR_INFO_TIMESTAMP_SOURCE before recording.
                            sink.onSample(buf, info, info.presentationTimeUs)
                        }
                        codec.releaseOutputBuffer(i, false)
                        if (eos) break
                    }
                }
            }
        } catch (e: Throwable) {
            if (!stopping) sink.onError("RECORDING_FAILED", e)
        } finally {
            try { codec.stop() } catch (_: Throwable) {}
            try { codec.release() } catch (_: Throwable) {}
            try { inputSurface.release() } catch (_: Throwable) {}
            drain.shutdown()
            sink.onEnded()
        }
    }

    /** End of input; the drain loop ends after the encoder flushes. */
    fun finish() {
        stopping = true
        try { codec.signalEndOfInputStream() } catch (_: Throwable) {}
    }

    /** Abort without waiting (failed start). */
    fun abort() {
        stopping = true
        try { codec.signalEndOfInputStream() } catch (_: Throwable) {}
    }
}
