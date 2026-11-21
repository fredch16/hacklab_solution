package dev.localvision.local_vision

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaMuxer
import android.util.Log
import java.io.File
import java.nio.ByteBuffer
import android.os.Handler
import android.os.Looper
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

/** Thin Flutter bridge for CameraSession + VisionStream. */
class CameraChannel(activity: FlutterActivity, engine: FlutterEngine) {
    private val main = Handler(Looper.getMainLooper())
    private var sink: EventChannel.EventSink? = null
    private val emit: (Map<String, Any?>) -> Unit = { e -> main.post { sink?.success(e) } }
    private lateinit var camera: CameraSession
    private val vision = VisionStream(activity.applicationContext, emit) { camera.stop("stream") }
    private var ticking = true
    private var offerGen = 0

    private val tick = object : Runnable {
        override fun run() {
            if (!ticking) return
            vision.collectStats { stream ->
                emit(mapOf("event" to "stats", "capture" to camera.captureStats(), "stream" to stream))
            }
            main.postDelayed(this, 1000)
        }
    }

    init {
        camera = CameraSession(activity, activity, engine.renderer, vision, emit)
        val messenger = engine.dartExecutor.binaryMessenger
        MethodChannel(messenger, "dev.localvision/camera").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> camera.start(call.argument<String>("user") ?: "scan") { id ->
                    result.success(mapOf("textureId" to id, "ok" to (id != null)))
                }
                "stop" -> { camera.stop(call.argument<String>("user") ?: "scan"); result.success(null) }
                "focus" -> {
                    camera.focus(call.argument<Double>("x") ?: 0.5, call.argument<Double>("y") ?: 0.5)
                    result.success(null)
                }
                "webrtcOffer" -> offer(call.argument<String>("sdp"), result)
                "webrtcStop" -> { vision.stop(call.argument<String>("reason") ?: "browser_stop"); result.success(null) }
                else -> result.notImplemented()
            }
        }
        EventChannel(messenger, "dev.localvision/camera_events").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(args: Any?, s: EventChannel.EventSink) { sink = s }
            override fun onCancel(args: Any?) { sink = null }
        })
        main.postDelayed(tick, 1000)
    }

    /** Browser asked for video: start the camera, wait for frames, then negotiate. */
    private fun offer(sdp: String?, result: MethodChannel.Result) {
        if (sdp == null) { result.error("BAD_REQUEST", "missing sdp", null); return }
        val gen = ++offerGen
        emit(mapOf("event" to "stream", "state" to "connecting", "error" to null))
        camera.start("stream") { id ->
            if (gen != offerGen) { result.error("NEGOTIATION_FAILED", "replaced by a newer offer", null); return@start }
            if (id == null) {
                emit(mapOf("event" to "stream", "state" to "failed", "error" to "CAMERA_UNAVAILABLE"))
                result.error("CAMERA_UNAVAILABLE", camera.lastFailure, null)
                return@start
            }
            vision.offer(sdp) { answer, code, msg ->
                if (answer != null) result.success(mapOf("sdp" to answer))
                else {
                    vision.stop("error", "NEGOTIATION_FAILED") // releases the camera
                    camera.stop("stream")
                    result.error(code ?: "NEGOTIATION_FAILED", msg, null)
                }
            }
        }
    }

    // ---- SPIKE (debug): adb shell am broadcast -a dev.localvision.SPIKE --es mime video/hevc --ei w 3840 --ei h 2160 --ei fps 30 --ei seconds 20
    private val activityRef = activity
    private val spikeReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            runSpike(
                i.getStringExtra("mime") ?: "video/hevc",
                i.getIntExtra("w", 3840), i.getIntExtra("h", 2160),
                i.getIntExtra("fps", 30), i.getIntExtra("seconds", 20),
                i.getIntExtra("mbps", 50),
            )
        }
    }

    init {
        androidx.core.content.ContextCompat.registerReceiver(
            activity, spikeReceiver, IntentFilter("dev.localvision.SPIKE"),
            androidx.core.content.ContextCompat.RECEIVER_EXPORTED,
        )
    }

    private fun runSpike(mime: String, w: Int, h: Int, fps: Int, seconds: Int, mbps: Int) {
        val file = File(activityRef.cacheDir, "spike.mp4").also { it.delete() }
        Log.i("LVC", "SPIKE start $mime ${w}x$h@$fps ${mbps}Mbps ${seconds}s")
        val muxer = MediaMuxer(file.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        var track = -1
        var t0 = -1L
        var frames = 0
        var out: VideoEncoderOutput? = null
        val sink = object : VideoSink {
            override fun onFormat(format: MediaFormat, width: Int, height: Int, rotationDegrees: Int) {
                Log.i("LVC", "SPIKE format $format rot=$rotationDegrees")
                muxer.setOrientationHint(rotationDegrees)
                track = muxer.addTrack(format); muxer.start()
            }
            override fun onSample(data: ByteBuffer, info: MediaCodec.BufferInfo, ptsUs: Long) {
                if (track < 0) return
                if (t0 < 0) t0 = ptsUs
                info.presentationTimeUs = ptsUs - t0
                muxer.writeSampleData(track, data, info); frames++
            }
            override fun onEnded() {
                try { if (track >= 0) muxer.stop() } catch (e: Throwable) { Log.w("LVC", "SPIKE muxer stop: $e") }
                muxer.release()
                main.post {
                    camera.attachVideo(null, null)
                    camera.stop("record")
                    Log.i("LVC", "SPIKE done frames=$frames actual=${out?.actualWidth}x${out?.actualHeight}")
                    MediaFileProbe.log("LVC", MediaFileProbe.probe(file))
                }
            }
            override fun onError(code: String, t: Throwable?) { Log.e("LVC", "SPIKE error $code $t") }
        }
        val spec = VideoSpec(mime, w, h, fps, mbps * 1_000_000)
        camera.onVideoBindFailed = { Log.e("LVC", "SPIKE bind failed $it") }
        camera.start("record") { id ->
            if (id == null) { Log.e("LVC", "SPIKE camera did not start"); return@start }
            val o = VideoEncoderOutput(spec, sink)
            out = o
            camera.attachVideo(o, spec)
            main.postDelayed({ o.finish() }, seconds * 1000L)
        }
    }

    fun dispose() {
        try { activityRef.unregisterReceiver(spikeReceiver) } catch (_: Throwable) {}
        ticking = false
        main.removeCallbacks(tick)
        vision.dispose()
        camera.shutdown()
    }
}
