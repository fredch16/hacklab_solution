package dev.localvision.local_vision

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.display.DisplayManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Surface
import androidx.camera.camera2.interop.Camera2CameraControl
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.CaptureRequestOptions
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.core.SurfaceOrientedMeteringPointFactory
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LiveData
import androidx.lifecycle.Observer
import io.flutter.view.TextureRegistry
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The single camera owner: back camera, Preview -> Flutter texture,
 * ImageAnalysis (640x360 YUV) -> [sink] and the QR decoder. Bound to the
 * activity lifecycle. The camera runs while at least one "user" ("stream" =
 * WebRTC peer, "scan" = QR scanner page) holds it.
 */
@androidx.annotation.OptIn(ExperimentalCamera2Interop::class)
class CameraSession(
    private val context: Context,
    private val owner: LifecycleOwner,
    private val textures: TextureRegistry,
    private val sink: FrameSink,
    private val emit: (Map<String, Any?>) -> Unit,
) {
    private val main = Handler(Looper.getMainLooper())
    private val mainExecutor = ContextCompat.getMainExecutor(context)
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val scanExecutor = Executors.newSingleThreadExecutor()
    private val qr = QrScanner()
    private val decoding = AtomicBoolean(false)

    private val users = mutableSetOf<String>()
    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var producer: TextureRegistry.SurfaceProducer? = null
    private var preview: Preview? = null
    private var analysis: ImageAnalysis? = null
    private var stateLive: LiveData<CameraState>? = null
    private var stateObserver: Observer<CameraState>? = null
    private var started = false
    private var previewBound = false
    private var previewW = 0
    private var previewH = 0

    private var framesSeen = false
    private val waiters = mutableListOf<(Boolean) -> Unit>()
    private var waitTimeout: Runnable? = null
    var lastFailure: String = "Phone camera did not start in time"
        private set

    // measured by the analyzer thread
    @Volatile private var capW: Int? = null
    @Volatile private var capH: Int? = null
    @Volatile private var fps: Double? = null
    @Volatile private var scanning = false
    private var windowStart = 0L
    private var windowCount = 0
    @Volatile private var lastFrameNs = 0L
    private var rebindAttempted = false
    private var lastScanNs = 0L
    private var lastText: String? = null
    private var lastTextNs = 0L

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(id: Int) {}
        override fun onDisplayRemoved(id: Int) {}
        override fun onDisplayChanged(id: Int) {
            val r = rotation()
            preview?.targetRotation = r
            analysis?.targetRotation = r
        }
    }

    private val surfaceCallback = object : TextureRegistry.SurfaceProducer.Callback {
        // Flutter recreated the surface: CameraX must draw into the new one.
        override fun onSurfaceAvailable() {
            main.post { if (started && !previewBound) bind() }
        }

        // The surface is going away: stop drawing into it (analysis keeps running).
        override fun onSurfaceCleanup() {
            main.post {
                if (previewBound) {
                    previewBound = false
                    preview?.let { provider?.unbind(it) }
                }
            }
        }
    }

    val running: Boolean get() = started

    private companion object { const val TAG = "LVC" }

    private fun rotation(): Int {
        val d = (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).getDisplay(0)
        return d?.rotation ?: Surface.ROTATION_0
    }

    /**
     * Takes a hold on the camera for [user], starting it if needed. [done] gets
     * the texture id once frames are arriving, or null (after releasing the
     * hold) on permission failure, camera error or a 5 s timeout.
     */
    fun start(user: String, done: (Long?) -> Unit) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            lastFailure = "Camera permission not granted on the phone"
            emit(error("PERMISSION_CAMERA"))
            done(null)
            return
        }
        users.add(user)
        scanning = "scan" in users
        waitForFrames { ok ->
            if (ok) done(producer?.id()) else { stop(user); done(null) }
        }
        if (!started) startInternal()
    }

    /** Drops [user]'s hold; the camera stops when nobody needs it. */
    fun stop(user: String) {
        users.remove(user)
        scanning = "scan" in users
        if (users.isEmpty() && started) stopInternal()
    }

    private fun waitForFrames(cb: (Boolean) -> Unit) {
        if (framesSeen) { cb(true); return }
        waiters.add(cb)
        if (waitTimeout == null) {
            val r = Runnable { lastFailure = "Phone camera did not start in time"; flushWaiters(false) }
            waitTimeout = r
            main.postDelayed(r, 5000)
        }
    }

    private fun flushWaiters(ok: Boolean) {
        waitTimeout?.let { main.removeCallbacks(it) }
        waitTimeout = null
        val w = waiters.toList()
        waiters.clear()
        w.forEach { it(ok) }
    }

    // No frames for 3 s while bound and in the foreground: report, rebind once.
    private val watchdog = object : Runnable {
        override fun run() {
            if (!started) return
            val fg = owner.lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.STARTED)
            if (!fg) lastFrameNs = System.nanoTime() // grace period after returning to the foreground
            val stalled = previewBound && System.nanoTime() - lastFrameNs > 3_000_000_000L
            if (fg && stalled) {
                Log.w(TAG, "no frames for 3 s (rebindAttempted=$rebindAttempted)")
                emit(error("CAMERA_FAILED"))
                framesSeen = false
                if (!rebindAttempted) {
                    rebindAttempted = true
                    bind()
                }
            }
            main.postDelayed(this, 1000)
        }
    }

    private fun startInternal() {
        started = true
        framesSeen = false
        rebindAttempted = false
        lastFrameNs = System.nanoTime()
        main.removeCallbacks(watchdog)
        main.postDelayed(watchdog, 1000)
        emit(state("initializing"))
        val fut = ProcessCameraProvider.getInstance(context)
        fut.addListener({
            if (!started) return@addListener
            try {
                provider = fut.get()
                producer = producer ?: textures.createSurfaceProducer().also { it.setCallback(surfaceCallback) }
                (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager)
                    .registerDisplayListener(displayListener, null)
                bind()
            } catch (e: Throwable) {
                failed("CAMERA_FAILED")
            }
        }, mainExecutor)
    }

    private fun failed(code: String) {
        lastFailure = "Phone camera error: $code"
        emit(error(code))
        flushWaiters(false)
    }

    private fun fpsRange(): Range<Int> = Range(30, 30)

    /** Continuous video AF, auto exposure and white balance. */
    private fun <T> applyQuality(b: androidx.camera.core.ExtendableBuilder<T>) {
        Camera2Interop.Extender(b)
            .setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange())
            .setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
            .setCaptureRequestOption(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_ON)
            .setCaptureRequestOption(CaptureRequest.CONTROL_AWB_MODE, CameraMetadata.CONTROL_AWB_MODE_AUTO)
    }

    private fun bind() {
        val p = provider ?: return
        val prod = producer ?: return
        p.unbindAll()
        stateLive?.let { l -> stateObserver?.let { l.removeObserver(it) } }

        val previewBuilder = Preview.Builder()
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
                    .setResolutionStrategy(ResolutionStrategy(Size(1280, 720), ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER))
                    .build()
            )
        applyQuality(previewBuilder)
        val pv = previewBuilder.build()
        pv.setSurfaceProvider(mainExecutor) { req ->
            val res = req.resolution
            previewW = res.width
            previewH = res.height
            prod.setSize(res.width, res.height)
            req.provideSurface(prod.surface, mainExecutor) {}
            req.setTransformationInfoListener(mainExecutor) { info ->
                emit(mapOf(
                    "event" to "preview",
                    "textureId" to prod.id(),
                    "width" to res.width, "height" to res.height,
                    "rotationDegrees" to info.rotationDegrees,
                ))
            }
        }
        pv.targetRotation = rotation()

        val ab = ImageAnalysis.Builder()
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
                    .setResolutionStrategy(ResolutionStrategy(Size(640, 360), ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER))
                    .build()
            )
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
        applyQuality(ab)
        val an = ab.build()
        an.targetRotation = rotation()
        an.setAnalyzer(analysisExecutor) { image -> onFrame(image) }
        preview = pv
        analysis = an
        windowStart = 0L; windowCount = 0; fps = null
        lastFrameNs = System.nanoTime()
        Log.i(TAG, "bind")

        try {
            val cam = p.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, pv, an)
            camera = cam
            previewBound = true
            Camera2CameraControl.from(cam.cameraControl).setCaptureRequestOptions(
                CaptureRequestOptions.Builder()
                    .setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                    .setCaptureRequestOption(CaptureRequest.CONTROL_AWB_MODE, CameraMetadata.CONTROL_AWB_MODE_AUTO)
                    .build()
            )
            val live = cam.cameraInfo.cameraState
            val obs = Observer<CameraState> { s ->
                val err = s.error
                when {
                    err != null -> failed(when (err.code) {
                        CameraState.ERROR_CAMERA_IN_USE, CameraState.ERROR_MAX_CAMERAS_IN_USE -> "DEVICE_IN_USE"
                        else -> "CAMERA_FAILED"
                    })
                    // "previewing" is reported only once frames really arrive (onFrame).
                    s.type == CameraState.Type.OPENING || s.type == CameraState.Type.PENDING_OPEN -> {
                        Log.i(TAG, "camera ${s.type}")
                        emit(state("initializing"))
                    }
                    s.type == CameraState.Type.CLOSING || s.type == CameraState.Type.CLOSED -> {
                        Log.i(TAG, "camera ${s.type}")
                        framesSeen = false
                    }
                    else -> Log.i(TAG, "camera ${s.type}")
                }
            }
            stateLive = live
            stateObserver = obs
            live.observe(owner, obs)
        } catch (e: IllegalArgumentException) {
            failed("CAPABILITY_UNSUPPORTED")
        } catch (e: Throwable) {
            failed("CAMERA_FAILED")
        }
    }

    private fun onFrame(image: androidx.camera.core.ImageProxy) {
        val now = System.nanoTime()
        capW = image.width
        capH = image.height
        if (windowStart == 0L) windowStart = now
        windowCount++
        if (now - windowStart >= 1_000_000_000L) {
            fps = windowCount * 1e9 / (now - windowStart)
            Log.i(TAG, "capture fps=${"%.1f".format(fps)} ${image.width}x${image.height} scan=$scanning peer=${sink.wantsFrames}")
            windowStart = now
            windowCount = 0
        }
        lastFrameNs = now
        rebindAttempted = false
        if (!framesSeen) {
            framesSeen = true
            Log.i(TAG, "frames flowing ${image.width}x${image.height}")
            main.post {
                emit(state("previewing"))
                flushWaiters(true)
            }
        }
        // ~5 decodes/s, on a copy and a separate thread so WebRTC feeding never waits.
        if (scanning && now - lastScanNs >= 200_000_000L && decoding.compareAndSet(false, true)) {
            lastScanNs = now
            try {
                val y = image.planes[0]
                val luma = QrScanner.tightLuma(y.buffer, y.rowStride, image.width, image.height)
                val w = image.width
                val h = image.height
                scanExecutor.execute {
                    try {
                        qr.decode(luma, w, h)?.let { text ->
                            val t = System.nanoTime()
                            if (text != lastText || t - lastTextNs > 1_500_000_000L) {
                                lastText = text
                                lastTextNs = t
                                emit(mapOf("event" to "scan", "text" to text))
                            }
                        }
                    } finally {
                        decoding.set(false)
                    }
                }
            } catch (e: Throwable) {
                decoding.set(false)
            }
        }
        if (sink.wantsFrames) sink.onFrame(image) else image.close()
    }

    /** Tap-to-focus/meter at a point normalised to the preview buffer (0..1). */
    fun focus(nx: Double, ny: Double) {
        val cam = camera ?: return
        val pv = preview ?: return
        if (!previewBound || previewW == 0) return
        val f = SurfaceOrientedMeteringPointFactory(previewW.toFloat(), previewH.toFloat(), pv)
        val pt = f.createPoint((nx * previewW).toFloat(), (ny * previewH).toFloat())
        val action = FocusMeteringAction.Builder(pt, FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE)
            .setAutoCancelDuration(3, TimeUnit.SECONDS) // then back to continuous AF
            .build()
        cam.cameraControl.startFocusAndMetering(action)
    }

    private fun stopInternal() {
        Log.i(TAG, "stop")
        main.removeCallbacks(watchdog)
        started = false
        previewBound = false
        framesSeen = false
        stateLive?.let { l -> stateObserver?.let { l.removeObserver(it) } }
        provider?.unbindAll()
        (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).unregisterDisplayListener(displayListener)
        producer?.release()
        producer = null
        preview = null
        analysis = null
        camera = null
        flushWaiters(false)
        emit(state("not_started"))
    }

    /** Hard stop regardless of holds (activity destroyed). */
    fun shutdown() {
        users.clear()
        scanning = false
        if (started) stopInternal()
    }

    /** Frames delivered by ImageAnalysis, measured; null when not running. */
    fun captureStats(): Map<String, Any?>? {
        val w = capW; val h = capH
        if (!started || !framesSeen || w == null || h == null) return null
        return mapOf("width" to w, "height" to h, "fps_measured" to fps)
    }

    private fun state(s: String) = mapOf("event" to "state", "state" to s, "error" to null)
    private fun error(code: String) = mapOf("event" to "state", "state" to "error", "error" to code)
}
