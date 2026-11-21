package dev.localvision.local_vision

import android.os.Handler
import android.os.Looper
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

/** Thin Flutter bridge for CameraSession + VisionStream. */
class CameraChannel(private val activity: FlutterActivity, engine: FlutterEngine) {
    private val main = Handler(Looper.getMainLooper())
    private var sink: EventChannel.EventSink? = null
    private val emit: (Map<String, Any?>) -> Unit = { e -> main.post { sink?.success(e) } }
    private val vision = VisionStream(activity.applicationContext, emit)
    private val camera = CameraSession(activity, activity, engine.renderer, vision, emit)
    private var ticking = true

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
        val messenger = engine.dartExecutor.binaryMessenger
        MethodChannel(messenger, "dev.localvision/camera").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> camera.start { id -> result.success(mapOf("textureId" to id)) }
                "stop" -> { vision.stop("camera_stopped"); camera.stop(); result.success(null) }
                "pause" -> { camera.pause(); result.success(null) }
                "resume" -> { camera.resume(); result.success(null) }
                "webrtcOffer" -> {
                    val sdp = call.argument<String>("sdp")
                    if (sdp == null) result.error("BAD_REQUEST", "missing sdp", null)
                    else if (!camera.running) result.error("CAMERA_UNAVAILABLE", "camera is not running", null)
                    else vision.offer(sdp) { answer, code, msg ->
                        if (answer != null) result.success(mapOf("sdp" to answer))
                        else result.error(code ?: "NEGOTIATION_FAILED", msg, null)
                    }
                }
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

    fun dispose() {
        ticking = false
        main.removeCallbacks(tick)
        vision.dispose()
        camera.stop()
    }
}
