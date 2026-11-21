package dev.localvision.local_vision

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaRecorder
import android.os.SystemClock
import android.util.Log

/** Fan-out target for the single microphone owner. Called on the bus thread. */
interface PcmSubscriber {
    /** 16-bit little-endian interleaved PCM; [ptsUs] on the elapsedRealtime clock for the first frame. */
    fun onPcm(data: ByteArray, size: Int, ptsUs: Long)
}

/**
 * The only microphone owner: one AudioRecord (CAMCORDER, 48 kHz, stereo when
 * the device allows, else mono). Runs while at least one subscriber exists.
 * Slice 3 adds a resampled tap here.
 */
class AudioCaptureBus {
    val sampleRate = 48000
    @Volatile var channels = 2
        private set

    private val subs = LinkedHashMap<String, PcmSubscriber>()
    private var rec: AudioRecord? = null
    private var thread: Thread? = null
    @Volatile private var running = false

    /** Starts capture if needed. False when the microphone cannot be opened. */
    @SuppressLint("MissingPermission")
    @Synchronized
    fun subscribe(name: String, sub: PcmSubscriber): Boolean {
        if (rec == null) {
            val r = open(AudioFormat.CHANNEL_IN_STEREO, 2) ?: open(AudioFormat.CHANNEL_IN_MONO, 1)
            if (r == null) return false
            rec = r
            r.startRecording()
            running = true
            thread = Thread({ loop(r) }, "lvc-audio").also { it.start() }
        }
        synchronized(subs) { subs[name] = sub }
        return true
    }

    @Synchronized
    fun unsubscribe(name: String) {
        val empty = synchronized(subs) { subs.remove(name); subs.isEmpty() }
        if (empty) stopCapture()
    }

    @SuppressLint("MissingPermission")
    private fun open(mask: Int, ch: Int): AudioRecord? {
        val min = AudioRecord.getMinBufferSize(sampleRate, mask, AudioFormat.ENCODING_PCM_16BIT)
        if (min <= 0) return null
        val r = AudioRecord(
            MediaRecorder.AudioSource.CAMCORDER, sampleRate, mask,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(min * 4, sampleRate / 5 * 2 * ch),
        )
        if (r.state != AudioRecord.STATE_INITIALIZED) { r.release(); return null }
        channels = ch
        Log.i("LVC", "audio bus open ${sampleRate}Hz ch=$ch source=CAMCORDER")
        return r
    }

    private fun stopCapture() {
        running = false
        thread?.join(500)
        thread = null
        try { rec?.stop() } catch (_: Throwable) {}
        rec?.release()
        rec = null
    }

    private fun loop(r: AudioRecord) {
        val chunk = sampleRate / 50 * 2 * channels // 20 ms
        val buf = ByteArray(chunk)
        val ts = AudioTimestamp()
        var frames = 0L
        while (running) {
            val n = r.read(buf, 0, chunk)
            if (n <= 0) continue
            val nowUs = SystemClock.elapsedRealtimeNanos() / 1000
            val got = n / (2 * channels)
            val ptsUs = if (r.getTimestamp(ts, AudioTimestamp.TIMEBASE_BOOTTIME) == AudioRecord.SUCCESS) {
                (ts.nanoTime + (frames - ts.framePosition) * 1_000_000_000L / sampleRate) / 1000
            } else nowUs - got * 1_000_000L / sampleRate
            frames += got
            val list = synchronized(subs) { subs.values.toList() }
            for (s in list) s.onPcm(buf, n, ptsUs)
        }
    }
}
