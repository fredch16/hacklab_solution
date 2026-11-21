package dev.localvision.local_vision

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.camera2.CaptureRequest
import android.hardware.display.DisplayManager
import android.util.Range
import android.util.Size
import android.view.Surface
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
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

/**
 * The single camera owner: back camera, Preview -> Flutter texture,
 * ImageAnalysis (640x360 YUV) -> [sink]. Bound to the activity lifecycle.
 */
@androidx.annotation.OptIn(ExperimentalCamera2Interop::class)
class CameraSession(
    private val context: Context,
    private val owner: LifecycleOwner,
    private val textures: TextureRegistry,
    private val sink: FrameSink,
    private val emit: (Map<String, Any?>) -> Unit,
) {
    private val mainExecutor = ContextCompat.getMainExecutor(context)
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private var provider: ProcessCameraProvider? = null
    private var producer: TextureRegistry.SurfaceProducer? = null
    private var preview: Preview? = null
    private var analysis: ImageAnalysis? = null
    private var stateLive: LiveData<CameraState>? = null
    private var stateObserver: Observer<CameraState>? = null
    private var started = false
    private var paused = false

    // measured by the analyzer thread
    @Volatile private var capW: Int? = null
    @Volatile private var capH: Int? = null
    @Volatile private var fps: Double? = null
    private var windowStart = 0L
    private var windowCount = 0

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(id: Int) {}
        override fun onDisplayRemoved(id: Int) {}
        override fun onDisplayChanged(id: Int) {
            val r = rotation()
            preview?.targetRotation = r
            analysis?.targetRotation = r
        }
    }

    val running: Boolean get() = started && !paused

    private fun rotation(): Int {
        val d = (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).getDisplay(0)
        return d?.rotation ?: Surface.ROTATION_0
    }

    /** [done] receives the texture id (null on failure; an error event is emitted). */
    fun start(done: (Long?) -> Unit) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            emit(error("PERMISSION_CAMERA"))
            done(null)
            return
        }
        if (started) { done(producer?.id()); return }
        started = true
        paused = false
        emit(state("initializing"))
        val fut = ProcessCameraProvider.getInstance(context)
        fut.addListener({
            try {
                provider = fut.get()
                producer = producer ?: textures.createSurfaceProducer()
                (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager)
                    .registerDisplayListener(displayListener, null)
                bind()
                done(producer?.id())
            } catch (e: Throwable) {
                started = false
                emit(error("CAMERA_FAILED"))
                done(null)
            }
        }, mainExecutor)
    }

    private fun fpsRange(): Range<Int> = Range(30, 30)

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
        Camera2Interop.Extender(previewBuilder)
            .setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange())
        val pv = previewBuilder.build()
        pv.setSurfaceProvider(mainExecutor) { req ->
            val res = req.resolution
            prod.setSize(res.width, res.height)
            req.provideSurface(prod.surface, mainExecutor) {}
            req.setTransformationInfoListener(mainExecutor) { info ->
                emit(mapOf(
                    "event" to "preview",
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
        Camera2Interop.Extender(ab)
            .setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange())
        val an = ab.build()
        an.targetRotation = rotation()
        an.setAnalyzer(analysisExecutor) { image ->
            val now = System.nanoTime()
            capW = image.width
            capH = image.height
            if (windowStart == 0L) windowStart = now
            windowCount++
            if (now - windowStart >= 1_000_000_000L) {
                fps = windowCount * 1e9 / (now - windowStart)
                windowStart = now
                windowCount = 0
            }
            if (sink.wantsFrames) sink.onFrame(image) else image.close()
        }
        preview = pv
        analysis = an
        windowStart = 0L; windowCount = 0; fps = null

        try {
            val cam = p.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, pv, an)
            val live = cam.cameraInfo.cameraState
            val obs = Observer<CameraState> { s ->
                val err = s.error
                when {
                    err != null -> emit(error(when (err.code) {
                        CameraState.ERROR_CAMERA_IN_USE, CameraState.ERROR_MAX_CAMERAS_IN_USE -> "DEVICE_IN_USE"
                        else -> "CAMERA_FAILED"
                    }))
                    s.type == CameraState.Type.OPEN -> emit(state("previewing"))
                    s.type == CameraState.Type.OPENING || s.type == CameraState.Type.PENDING_OPEN -> emit(state("initializing"))
                    else -> {}
                }
            }
            stateLive = live
            stateObserver = obs
            live.observe(owner, obs)
        } catch (e: IllegalArgumentException) {
            emit(error("CAPABILITY_UNSUPPORTED"))
        } catch (e: Throwable) {
            emit(error("CAMERA_FAILED"))
        }
    }

    /** Scanner page is open: release the camera. */
    fun pause() {
        if (!started || paused) return
        paused = true
        provider?.unbindAll()
        emit(state("paused"))
    }

    fun resume() {
        if (!started || !paused) return
        paused = false
        emit(state("initializing"))
        bind()
    }

    fun stop() {
        if (!started) return
        started = false
        paused = false
        stateLive?.let { l -> stateObserver?.let { l.removeObserver(it) } }
        provider?.unbindAll()
        (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).unregisterDisplayListener(displayListener)
        producer?.release()
        producer = null
        preview = null
        analysis = null
    }

    /** Frames delivered by ImageAnalysis, measured; null parts when unknown. */
    fun captureStats(): Map<String, Any?>? {
        val w = capW; val h = capH
        if (!running || w == null || h == null) return null
        return mapOf("width" to w, "height" to h, "fps_measured" to fps)
    }

    private fun state(s: String) = mapOf("event" to "state", "state" to s, "error" to null)
    private fun error(code: String) = mapOf("event" to "state", "state" to "error", "error" to code)
}
