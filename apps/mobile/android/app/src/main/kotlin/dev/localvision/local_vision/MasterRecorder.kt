package dev.localvision.local_vision

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaMuxer
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.ParcelFileDescriptor
import android.os.PowerManager
import android.os.StatFs
import android.os.SystemClock
import android.provider.MediaStore
import android.util.Log
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Master recording: camera (encoder surface) + AudioCaptureBus -> AAC ->
 * MediaMuxer -> MediaStore Movies/LocalVision. Fixed profile 1080p60, HEVC
 * (AVC when no HEVC encoder does 1080p60). The item stays IS_PENDING until the
 * muxer stopped cleanly. States: idle|starting|recording|stopping|finalizing|available|failed.
 */
class MasterRecorder(
    private val ctx: Context,
    private val camera: CameraSession,
    private val audio: AudioCaptureBus,
    private val store: RecordingStore,
    private val emit: (Map<String, Any?>) -> Unit,
) {
    companion object {
        const val W = 1920; const val H = 1080; const val FPS = 60
        const val MIN_START_FREE = 2L * 1024 * 1024 * 1024
        const val STOP_FREE = 500L * 1024 * 1024
        private const val TAG = "LVC"
    }

    private val main = Handler(Looper.getMainLooper())
    private val lock = Object()

    var state = "idle"; private set
    private var id: String? = null
    private var startedUtc: String? = null
    private var error: String? = null
    private var warning: String? = null
    private var codecName = "hevc"
    private var bitrate = 0
    private var bytes = 0L
    private var startRealtimeMs = 0L
    private var audioRate = 0
    private var audioCh = 0

    private var video: VideoEncoder? = null
    private var aac: AacEncoder? = null
    private var muxer: MediaMuxer? = null
    private var pfd: ParcelFileDescriptor? = null
    private var uri: Uri? = null
    private var filename = ""
    private var vTrack = -1; private var aTrack = -1
    private var vFormat: MediaFormat? = null; private var aFormat: MediaFormat? = null
    private var muxStarted = false
    private val pending = ArrayList<Triple<Boolean, ByteArray, MediaCodec.BufferInfo>>()
    private var t0Us = -1L
    private var lastV = -1L; private var lastA = -1L
    private var vEnded = false; private var aEnded = false
    private var videoSamples = 0
    private var stopReason: String? = null
    private var rtOffsetUs = 0L

    fun snapshot(): Map<String, Any?> {
        val running = state == "recording"
        return mapOf(
            "state" to state, "id" to id, "started_utc" to startedUtc,
            "elapsed_ms" to (if (running || state == "stopping") SystemClock.elapsedRealtime() - startRealtimeMs else null),
            "bytes" to (if (state == "idle") null else bytes),
            "profile" to (if (state == "idle" || id == null) null else mapOf("lens_id" to "back", "width" to W, "height" to H, "fps" to FPS, "codec" to codecName, "hdr" to false)),
            "audio" to (if (audioRate > 0 && state != "idle") mapOf("sample_rate" to audioRate, "channels" to audioCh) else null),
            "error" to error, "warning" to warning,
        )
    }

    private fun publish() { emit(mapOf("event" to "recording") + ("recording" to snapshot())) }

    private fun freeBytes(): Long = try { StatFs(Environment.getExternalStorageDirectory().path).availableBytes } catch (_: Throwable) { Long.MAX_VALUE }

    private fun pickCodec(): Pair<String, String>? {
        val list = MediaCodecList(MediaCodecList.REGULAR_CODECS)
        for ((mime, name) in listOf("video/hevc" to "hevc", "video/avc" to "avc")) {
            val ok = list.codecInfos.any { ci ->
                ci.isEncoder && ci.supportedTypes.contains(mime) &&
                    try { ci.getCapabilitiesForType(mime).videoCapabilities?.areSizeAndRateSupported(W, H, FPS.toDouble()) == true } catch (_: Throwable) { false }
            }
            if (ok) return mime to name
        }
        return null
    }

    /** [done] gets null on success or an error code (nothing is left running on failure). */
    fun start(done: (String?) -> Unit) {
        if (state in listOf("starting", "recording", "stopping", "finalizing")) { done("ALREADY_RECORDING"); return }
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) { done("PERMISSION_MIC"); return }
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) { done("CAMERA_FAILED"); return }
        if (freeBytes() < MIN_START_FREE) { done("LOW_STORAGE"); return }
        val (mime, cname) = pickCodec() ?: run { done("PROFILE_UNSUPPORTED"); return }
        if (!camera.supportsFps(FPS)) { done("PROFILE_UNSUPPORTED"); return }
        Log.i(TAG, "record: ${W}x$H@$FPS $mime")
        codecName = cname
        bitrate = if (cname == "hevc") 20_000_000 else 30_000_000
        reset()
        state = "starting"; error = null; warning = null
        id = "rec-" + System.currentTimeMillis()
        publish()
        camera.start("record") { tex ->
            if (tex == null) { fail("CAMERA_FAILED", done); return@start }
            try {
                prepare(mime)
            } catch (e: Throwable) {
                Log.w(TAG, "record prepare failed: ${e.javaClass.simpleName} ${e.message}")
                cleanupFailed("PROFILE_UNSUPPORTED", done); return@start
            }
            camera.onVideoBindFailed = { code -> main.post { cleanupFailed(code, done) } }
            val spec = VideoSpec(mime, W, H, FPS, bitrate)
            val v = video!!
            camera.setRecordSurface(v.inputSurface, spec)
            v.start()
            // First encoded frame proves the camera session accepted the 3-output combination.
            main.postDelayed({
                if (state == "starting") cleanupFailed("PROFILE_UNSUPPORTED", done)
            }, 6000)
            startDone = done
        }
    }

    private var startDone: ((String?) -> Unit)? = null

    private fun reset() {
        bytes = 0; vTrack = -1; aTrack = -1; vFormat = null; aFormat = null; muxStarted = false
        pending.clear(); t0Us = -1; lastV = -1; lastA = -1; vEnded = false; aEnded = false
        videoSamples = 0; stopReason = null; audioRate = 0; audioCh = 0; startDone = null
    }

    private fun prepare(mime: String) {
        val ts = SimpleDateFormat("yyyyMMdd'T'HHmmss'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date())
        filename = "local-$ts.mp4"
        startedUtc = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date())
        val cv = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, filename)
            put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
            put(MediaStore.Video.Media.RELATIVE_PATH, Environment.DIRECTORY_MOVIES + "/LocalVision")
            put(MediaStore.Video.Media.IS_PENDING, 1)
        }
        val u = ctx.contentResolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv) ?: error("MediaStore insert failed")
        uri = u
        val fd = ctx.contentResolver.openFileDescriptor(u, "rw") ?: error("open failed")
        pfd = fd
        val m = MediaMuxer(fd.fileDescriptor, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        m.setOrientationHint(camera.orientationHint())
        muxer = m
        rtOffsetUs = if (camera.timestampIsRealtime()) 0L
        else (SystemClock.elapsedRealtimeNanos() / 1000) - SystemClock.uptimeMillis() * 1000
        video = VideoEncoder(VideoSpec(mime, W, H, FPS, bitrate), videoSink)
    }

    private val videoSink = object : VideoSink {
        override fun onFormat(format: MediaFormat) { synchronized(lock) { vFormat = format; tryStartMuxer() } }
        override fun onSample(data: ByteBuffer, info: MediaCodec.BufferInfo, ptsUs: Long) {
            synchronized(lock) {
                if (t0Us < 0) {
                    t0Us = ptsUs + rtOffsetUs
                    main.post { onFirstFrame() }
                }
                var p = ptsUs + rtOffsetUs - t0Us
                if (p <= lastV) p = lastV + 1
                lastV = p
                videoSamples++
                write(true, data, info, p)
            }
        }
        override fun onEnded() { synchronized(lock) { vEnded = true }; main.post { maybeFinalize() } }
        override fun onError(code: String, t: Throwable?) { Log.e(TAG, "video error $code $t"); main.post { abnormalStop("RECORDING_FAILED") } }
    }

    private val audioSink = object : AudioSink {
        override fun onFormat(format: MediaFormat) { synchronized(lock) { aFormat = format; tryStartMuxer() } }
        override fun onSample(data: ByteBuffer, info: MediaCodec.BufferInfo, ptsUs: Long) {
            synchronized(lock) {
                if (t0Us < 0) return // before the first video frame
                var p = ptsUs - t0Us
                if (p < 0) return
                if (p <= lastA) p = lastA + 1
                lastA = p
                write(false, data, info, p)
            }
        }
        override fun onEnded() { synchronized(lock) { aEnded = true }; main.post { maybeFinalize() } }
        override fun onError(t: Throwable) { Log.e(TAG, "audio error $t"); main.post { abnormalStop("RECORDING_FAILED") } }
    }

    /** Both tracks known: start the muxer and flush what waited. Holds [lock]. */
    private fun tryStartMuxer() {
        val m = muxer ?: return
        val vf = vFormat ?: return
        val af = aFormat ?: return
        if (muxStarted) return
        vTrack = m.addTrack(vf); aTrack = m.addTrack(af)
        m.start(); muxStarted = true
        for ((isV, bytesArr, info) in pending) {
            m.writeSampleData(if (isV) vTrack else aTrack, ByteBuffer.wrap(bytesArr), info)
            this.bytes += info.size
        }
        pending.clear()
    }

    /** Holds [lock]. */
    private fun write(isVideo: Boolean, data: ByteBuffer, info: MediaCodec.BufferInfo, ptsUs: Long) {
        val i = MediaCodec.BufferInfo().apply { set(0, info.size, ptsUs, info.flags) }
        data.position(info.offset); data.limit(info.offset + info.size)
        if (!muxStarted) {
            if (pending.size > 900) return
            val copy = ByteArray(info.size); data.get(copy)
            pending.add(Triple(isVideo, copy, i)); return
        }
        try {
            muxer!!.writeSampleData(if (isVideo) vTrack else aTrack, data, i)
            bytes += info.size
        } catch (t: Throwable) {
            Log.e(TAG, "muxer write failed: $t")
            main.post { abnormalStop("RECORDING_FAILED") }
        }
    }

    private fun onFirstFrame() {
        if (state != "starting") return
        // microphone: recording always has audio
        val enc = try {
            if (!audio.subscribe("record", object : PcmSubscriber {
                    override fun onPcm(data: ByteArray, size: Int, ptsUs: Long) { aac?.onPcm(data, size, ptsUs) }
                })) null else AacEncoder(audio.sampleRate, audio.channels, audioSink)
        } catch (t: Throwable) { Log.w(TAG, "aac init failed $t"); null }
        if (enc == null) { audio.unsubscribe("record"); cleanupFailed("PERMISSION_MIC", startDone); return }
        aac = enc
        audioRate = audio.sampleRate; audioCh = audio.channels
        startRealtimeMs = SystemClock.elapsedRealtime()
        state = "recording"
        Log.i(TAG, "recording started id=$id")
        startDone?.invoke(null); startDone = null
        publish()
        main.postDelayed(ticker, 1000)
    }

    private val ticker = object : Runnable {
        override fun run() {
            if (state != "recording") return
            val free = freeBytes()
            warning = null
            val bytesPerSec = bitrate / 8L + 24_000
            if (free / bytesPerSec < 600) warning = "LOW_STORAGE"
            if (Build.VERSION.SDK_INT >= 29) {
                val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
                if (pm.currentThermalStatus >= PowerManager.THERMAL_STATUS_SEVERE) warning = warning ?: "THERMAL"
            }
            publish()
            if (free < STOP_FREE) { Log.w(TAG, "storage below 500 MB: stopping"); stop("storage") }
            else main.postDelayed(this, 1000)
        }
    }

    /** User/browser stop (or storage/background). Error code when not recording. */
    fun stop(reason: String = "user"): String? {
        if (state != "recording") return "NOT_RECORDING"
        stopReason = reason
        state = "stopping"
        main.removeCallbacks(ticker)
        publish()
        audio.unsubscribe("record")
        aac?.finish()
        video?.finish()
        return null
    }

    private fun abnormalStop(code: String) {
        if (state != "recording" && state != "starting") return
        error = code
        if (state == "starting") { cleanupFailed(code, startDone); return }
        stop("error")
    }

    private fun maybeFinalize() {
        synchronized(lock) { if (!(vEnded && aEnded) || state == "finalizing" || state == "available" || state == "failed" || state == "idle") return }
        state = "finalizing"
        publish()
        Thread({ finalizeFile() }, "lvc-finalize").start()
    }

    private fun finalizeFile() {
        var ok = true
        val m = muxer
        try {
            synchronized(lock) { if (muxStarted) m?.stop() else ok = false }
        } catch (t: Throwable) { Log.e(TAG, "muxer stop failed: $t"); ok = false }
        try { m?.release() } catch (_: Throwable) {}
        try { pfd?.close() } catch (_: Throwable) {}
        val u = uri
        var probe: MediaFileProbe.Result? = null
        if (ok && u != null) {
            ctx.contentResolver.update(u, ContentValues().apply { put(MediaStore.Video.Media.IS_PENDING, 0) }, null, null)
            val size = ctx.contentResolver.openFileDescriptor(u, "r")?.use { it.statSize } ?: 0L
            probe = MediaFileProbe.probe(ctx, u, size)
            MediaFileProbe.log(TAG, probe)
            store.add(JSONObject().apply {
                put("id", id); put("uri", u.toString()); put("filename", filename)
                put("created_utc", startedUtc); put("duration_ms", (probe?.durationUs ?: 0L) / 1000)
                put("size_bytes", size); put("width", probe?.video?.width ?: W); put("height", probe?.video?.height ?: H)
                put("fps_measured", probe?.videoFps ?: JSONObject.NULL); put("codec", codecName)
            })
        } else if (u != null) {
            try { ctx.contentResolver.delete(u, null, null) } catch (_: Throwable) {}
        }
        main.post {
            if (ok) { state = if (error != null) "failed" else "available" } else { state = "failed"; error = "RECORDING_FINALIZE_FAILED" }
            warning = if (stopReason == "storage") "LOW_STORAGE" else null
            probe?.let { bytes = it.sizeBytes }
            publish()
            emit(mapOf("event" to "recordings", "items" to store.list()))
            Log.i(TAG, "recording finalized state=$state error=$error reason=$stopReason")
            video = null; aac = null; muxer = null; pfd = null
            camera.setRecordSurface(null, null)
            camera.stop("record")
        }
    }

    private fun fail(code: String, done: ((String?) -> Unit)?) {
        error = code; state = "failed"; publish(); done?.invoke(code)
        camera.stop("record")
    }

    /** Start failed before recording began: undo everything, remove the pending item. */
    private fun cleanupFailed(code: String, done: ((String?) -> Unit)?) {
        if (state != "starting") return
        startDone = null
        audio.unsubscribe("record")
        aac?.finish()
        video?.abort()
        try { muxer?.release() } catch (_: Throwable) {}
        try { pfd?.close() } catch (_: Throwable) {}
        uri?.let { try { ctx.contentResolver.delete(it, null, null) } catch (_: Throwable) {} }
        video = null; aac = null; muxer = null; pfd = null; uri = null
        camera.setRecordSurface(null, null)
        camera.stop("record")
        error = code; state = "failed"; id = null
        publish()
        done?.invoke(code)
    }

    /** App background / session end while recording: finalize what we have. */
    fun stopForLifecycle(reason: String) { if (state == "recording") stop(reason) }
}
