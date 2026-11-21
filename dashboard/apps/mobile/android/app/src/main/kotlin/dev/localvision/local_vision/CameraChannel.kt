package dev.localvision.local_vision

import android.os.Handler
import android.os.Looper
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

/** Thin Flutter bridge for CameraSession, VisionStream and MasterRecorder. */
class CameraChannel(activity: FlutterActivity, engine: FlutterEngine) {
    private val main = Handler(Looper.getMainLooper())
    private var sink: EventChannel.EventSink? = null
    private val emit: (Map<String, Any?>) -> Unit = { e -> main.post { sink?.success(e) } }
    private lateinit var camera: CameraSession
    private val vision = VisionStream(activity.applicationContext, emit) { camera.stop("stream") }
    private val audio = AudioCaptureBus()
    private val store = RecordingStore(activity)
    private lateinit var recorder: MasterRecorder
    private val voice = VoiceEngine(activity.applicationContext, audio, emit)
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
        recorder = MasterRecorder(activity, camera, audio, store, emit)
        camera.onBackgrounded = { recorder.stopForLifecycle("background") }
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
                "recordStart" -> recorder.start { err -> result.success(mapOf("ok" to (err == null), "error" to err)) }
                "recordStop" -> {
                    val err = recorder.stop(call.argument<String>("reason") ?: "user")
                    result.success(mapOf("ok" to (err == null), "error" to err))
                }
                "voiceStart" -> { voice.start(); result.success(null) }
                "voiceStop" -> { voice.stop(call.argument<Boolean>("clear") == true); result.success(null) }
                "voiceSetPhrase" -> {
                    val err = voice.setPhrase(call.argument<String>("phrase"))
                    result.success(mapOf("ok" to (err == null), "error" to err))
                }
                "voiceMute" -> {
                    voice.setMuted(call.argument<Boolean>("muted") == true)
                    result.success(mapOf("ok" to true, "error" to null))
                }
                "voiceState" -> result.success(voice.snapshot())
                "recordState" -> result.success(mapOf("recording" to recorder.snapshot(), "recordings" to store.list()))
                else -> result.notImplemented()
            }
        }
        EventChannel(messenger, "dev.localvision/camera_events").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(args: Any?, s: EventChannel.EventSink) {
                sink = s
                s.success(mapOf("event" to "voice", "voice" to voice.snapshot()))
            }
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

    fun dispose() {
        ticking = false
        main.removeCallbacks(tick)
        recorder.stopForLifecycle("session_ended")
        voice.dispose()
        vision.dispose()
        camera.shutdown()
    }
}
