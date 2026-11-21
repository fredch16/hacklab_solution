package dev.localvision.local_vision

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.MeteringRectangle
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.hardware.display.DisplayManager
import android.media.Image
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Surface
import androidx.core.content.ContextCompat
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import io.flutter.view.TextureRegistry
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The single camera owner (Camera2). Outputs: preview texture, a small YUV
 * ImageReader (WebRTC + QR scanning) and, while recording, the master
 * encoder's input surface. The camera runs while at least one "user" holds it
 * ("stream", "scan", "record"). CameraX was dropped: with Preview + Analysis +
 * 4K Video it rejects the stream combination on this device (ADR 0003).
 */
class CameraSession(
    private val context: Context,
    private val owner: LifecycleOwner,
    private val textures: TextureRegistry,
    private val sink: FrameSink,
    private val emit: (Map<String, Any?>) -> Unit,
) {
    private val main = Handler(Looper.getMainLooper())
    private val mainExec = ContextCompat.getMainExecutor(context)
    private val frameThread = HandlerThread("lvc-frames").apply { start() }
    private val frameHandler = Handler(frameThread.looper)
    private val scanThread = HandlerThread("lvc-scan").apply { start() }
    private val scanHandler = Handler(scanThread.looper)
    private val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    private val qr = QrScanner()
    private val decoding = AtomicBoolean(false)

    private val users = mutableSetOf<String>()
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var chars: CameraCharacteristics? = null
    private var cameraId: String? = null
    private var producer: TextureRegistry.SurfaceProducer? = null
    private var previewValid = true
    private var reader: ImageReader? = null
    private var previewSize = Size(1280, 720)
    private var started = false
    private var opening = false
    private var suspended = false // activity in background: device closed, holds kept
    private var generation = 0

    // recording output (set by MasterRecorder)
    private var recSurface: Surface? = null
    private var recSpec: VideoSpec? = null
    var onVideoBindFailed: ((String) -> Unit)? = null
    var onBackgrounded: (() -> Unit)? = null

    private var tapUntil = 0L
    private var tapRegion: MeteringRectangle? = null

    private var framesSeen = false
    private val waiters = mutableListOf<(Boolean) -> Unit>()
    private var waitTimeout: Runnable? = null
    var lastFailure: String = "Phone camera did not start in time"
        private set

    @Volatile private var capW: Int? = null
    @Volatile private var capH: Int? = null
    @Volatile private var fps: Double? = null
    @Volatile private var scanning = false
    @Volatile private var streamRotation = 0
    @Volatile private var lastFrameNs = 0L
    private var windowStart = 0L
    private var windowCount = 0
    private var lastScanNs = 0L
    private var lastText: String? = null
    private var lastTextNs = 0L
    private var rebindAttempted = false

    private companion object { const val TAG = "LVC" }

    val running: Boolean get() = started
    val framesFlowing: Boolean get() = framesSeen

    init {
        main.post {
            owner.lifecycle.addObserver(object : DefaultLifecycleObserver {
                override fun onStart(o: LifecycleOwner) {
                    suspended = false
                    if (started && device == null) openCamera()
                }
                override fun onStop(o: LifecycleOwner) {
                    if (started) {
                        Log.i(TAG, "activity stopped: closing camera")
                        onBackgrounded?.invoke()
                        suspended = true
                        closeDevice()
                        framesSeen = false
                    }
                }
            })
        }
    }

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(id: Int) {}
        override fun onDisplayRemoved(id: Int) {}
        override fun onDisplayChanged(id: Int) { if (device != null) publishPreview() }
    }

    private val surfaceCallback = object : TextureRegistry.SurfaceProducer.Callback {
        // Flutter recreated the surface: the session must draw into the new one.
        override fun onSurfaceAvailable() { main.post { previewValid = true; if (device != null) reconfigure() } }
        // Surface is going away: stop drawing into it; analysis/recording keep running.
        override fun onSurfaceCleanup() { main.post { previewValid = false; if (device != null) reconfigure() } }
    }

    // ---- holds ----

    fun start(user: String, done: (Long?) -> Unit) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            lastFailure = "Camera permission not granted on the phone"
            emit(error("PERMISSION_CAMERA"))
            done(null)
            return
        }
        users.add(user)
        scanning = "scan" in users
        waitForFrames { ok -> if (ok) done(producer?.id()) else { stop(user); done(null) } }
        if (!started) startInternal()
    }

    fun stop(user: String) {
        users.remove(user)
        scanning = "scan" in users
        if (users.isEmpty() && started) stopInternal()
    }

    fun holds(user: String) = user in users

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

    private fun startInternal() {
        started = true
        framesSeen = false
        rebindAttempted = false
        lastFrameNs = System.nanoTime()
        producer = producer ?: textures.createSurfaceProducer().also { it.setCallback(surfaceCallback) }
        (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).registerDisplayListener(displayListener, main)
        main.removeCallbacks(watchdog)
        main.postDelayed(watchdog, 1000)
        emit(state("initializing"))
        if (!suspended) openCamera()
    }

    private fun stopInternal() {
        Log.i(TAG, "stop")
        started = false
        main.removeCallbacks(watchdog)
        closeDevice()
        (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).unregisterDisplayListener(displayListener)
        producer?.release()
        producer = null
        framesSeen = false
        flushWaiters(false)
        emit(state("not_started"))
    }

    fun shutdown() {
        users.clear(); scanning = false
        if (started) stopInternal()
        frameThread.quitSafely(); scanThread.quitSafely()
    }

    // ---- recording output ----

    /** Adds/removes the master encoder surface (reconfigures the session). */
    fun setRecordSurface(surface: Surface?, spec: VideoSpec?) {
        recSurface = surface
        recSpec = spec
        if (device != null) reconfigure()
    }

    /** Clockwise degrees to rotate the (sensor-oriented) buffer to appear upright for [displayDeg]. */
    private fun rotationFor(displayDeg: Int): Int {
        val sensor = chars?.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        return (sensor - displayDeg + 360) % 360 // back camera
    }

    private fun displayDegrees(): Int {
        val d = (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).getDisplay(0)
        return when (d?.rotation) { Surface.ROTATION_90 -> 90; Surface.ROTATION_180 -> 180; Surface.ROTATION_270 -> 270; else -> 0 }
    }

    /** MP4 orientation hint for a recording started now (follows the device orientation). */
    fun orientationHint(): Int = rotationFor(displayDegrees())

    /** True when the camera can run a fixed [fps] AE range (e.g. 60). */
    fun supportsFps(fps: Int): Boolean {
        val c = chars ?: manager.cameraIdList.firstOrNull {
            manager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
        }?.let { manager.getCameraCharacteristics(it) } ?: return false
        return c.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)?.any { it.lower == fps && it.upper == fps } == true
    }

    /** Camera sensor clock domain; recording needs REALTIME (boottime) to align with audio. */
    fun timestampIsRealtime(): Boolean =
        chars?.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE) == CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME

    private fun publishPreview() {
        val prod = producer ?: return
        val rot = rotationFor(displayDegrees())
        streamRotation = rot
        emit(mapOf(
            "event" to "preview", "textureId" to prod.id(),
            "width" to previewSize.width, "height" to previewSize.height, "rotationDegrees" to rot,
        ))
    }

    // ---- camera device ----

    private fun openCamera() {
        if (opening || device != null || !started) return
        val id = manager.cameraIdList.firstOrNull {
            manager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
        }
        if (id == null) { failed("CAPABILITY_UNSUPPORTED"); return }
        cameraId = id
        chars = manager.getCameraCharacteristics(id)
        opening = true
        val gen = ++generation
        try {
            manager.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(d: CameraDevice) {
                    opening = false
                    if (gen != generation || !started) { d.close(); return }
                    device = d
                    Log.i(TAG, "camera opened $id")
                    createSession()
                }
                override fun onDisconnected(d: CameraDevice) {
                    opening = false; d.close()
                    if (device === d) { device = null; session = null }
                    if (started && !suspended) failed("CAMERA_FAILED")
                }
                override fun onError(d: CameraDevice, code: Int) {
                    opening = false; d.close()
                    if (device === d) { device = null; session = null }
                    failed(if (code == ERROR_CAMERA_IN_USE || code == ERROR_MAX_CAMERAS_IN_USE) "DEVICE_IN_USE" else "CAMERA_FAILED")
                }
            }, main)
        } catch (e: Throwable) {
            opening = false
            failed(if (e is SecurityException) "PERMISSION_CAMERA" else "CAMERA_FAILED")
        }
    }

    private fun closeDevice() {
        generation++
        try { session?.close() } catch (_: Throwable) {}
        session = null
        try { device?.close() } catch (_: Throwable) {}
        device = null
        opening = false
        try { reader?.close() } catch (_: Throwable) {}
        reader = null
    }

    private fun reconfigure() {
        try { session?.close() } catch (_: Throwable) {}
        session = null
        createSession()
    }

    private fun createSession() {
        val dev = device ?: return
        val ch = chars ?: return
        val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)!!
        previewSize = pick(map.getOutputSizes(android.graphics.SurfaceTexture::class.java).filter { it.width <= 1280 }.toTypedArray(), 1280, 720)
        val yuv = pick(map.getOutputSizes(ImageFormat.YUV_420_888), 640, 360)
        try { reader?.close() } catch (_: Throwable) {}
        val rd = ImageReader.newInstance(yuv.width, yuv.height, ImageFormat.YUV_420_888, 3)
        rd.setOnImageAvailableListener({ r -> r.acquireLatestImage()?.let { onFrame(it) } }, frameHandler)
        reader = rd

        val outs = mutableListOf<OutputConfiguration>()
        val prod = producer
        var withPreview = false
        if (prod != null && previewValid) {
            prod.setSize(previewSize.width, previewSize.height)
            outs.add(OutputConfiguration(prod.surface))
            withPreview = true
        }
        outs.add(OutputConfiguration(rd.surface))
        val rs = recSurface
        if (rs != null) {
            outs.add(OutputConfiguration(rs))
        }
        val gen = generation
        try {
            dev.createCaptureSession(SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outs, mainExec,
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(s: CameraCaptureSession) {
                        if (gen != generation || device !== dev) { s.close(); return }
                        session = s
                        updateRepeating(withPreview)
                        publishPreview()
                    }
                    override fun onConfigureFailed(s: CameraCaptureSession) {
                        Log.w(TAG, "session configure failed (record=${rs != null})")
                        if (rs != null) {
                            recSurface = null; recSpec = null
                            onVideoBindFailed?.invoke("PROFILE_UNSUPPORTED")
                            if (device === dev) createSession()
                        } else failed("CAMERA_FAILED")
                    }
                }))
        } catch (e: Throwable) {
            Log.w(TAG, "createCaptureSession: ${e.javaClass.simpleName} ${e.message}")
            if (rs != null) {
                recSurface = null; recSpec = null
                onVideoBindFailed?.invoke("PROFILE_UNSUPPORTED")
                createSession()
            } else failed("CAMERA_FAILED")
        }
    }

    private var previewInSession = false

    private fun updateRepeating(withPreview: Boolean = previewInSession) {
        val dev = device ?: return
        val s = session ?: return
        previewInSession = withPreview
        try {
            val recording = recSurface != null
            val b = dev.createCaptureRequest(if (recording) CameraDevice.TEMPLATE_RECORD else CameraDevice.TEMPLATE_PREVIEW)
            if (withPreview) producer?.let { b.addTarget(it.surface) }
            reader?.let { b.addTarget(it.surface) }
            recSurface?.let { b.addTarget(it) }
            val rate = if (recording) (recSpec?.fps ?: 30) else 30
            b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(rate, rate))
            b.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)
            b.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
            b.set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_ON)
            b.set(CaptureRequest.CONTROL_AWB_MODE, CameraMetadata.CONTROL_AWB_MODE_AUTO)
            val tap = tapRegion
            if (tap != null && System.currentTimeMillis() < tapUntil) {
                b.set(CaptureRequest.CONTROL_AF_REGIONS, arrayOf(tap))
                b.set(CaptureRequest.CONTROL_AE_REGIONS, arrayOf(tap))
            }
            s.setRepeatingRequest(b.build(), null, main)
        } catch (e: Throwable) {
            Log.w(TAG, "setRepeatingRequest: ${e.javaClass.simpleName} ${e.message}")
        }
    }

    /** Tap-to-focus/meter at a point normalised to the preview buffer (0..1); back to continuous AF after 3 s. */
    fun focus(nx: Double, ny: Double) {
        val ch = chars ?: return
        val active = ch.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE) ?: return
        // 16:9 stream = centred crop of the active array.
        val cw = active.width().toDouble()
        val chh = minOf(active.height().toDouble(), cw * 9 / 16)
        val rect = Rect(
            active.left,
            (active.top + (active.height() - chh) / 2).toInt(),
            active.right,
            (active.top + (active.height() + chh) / 2).toInt(),
        )
        val x = rect.left + (nx * rect.width()).toInt()
        val y = rect.top + (ny * rect.height()).toInt()
        val r = (active.width() / 12)
        tapRegion = MeteringRectangle(
            (x - r).coerceIn(active.left, active.right - 2 * r), (y - r).coerceIn(active.top, active.bottom - 2 * r),
            2 * r, 2 * r, MeteringRectangle.METERING_WEIGHT_MAX - 1,
        )
        tapUntil = System.currentTimeMillis() + 3000
        updateRepeating()
        main.postDelayed({ if (System.currentTimeMillis() >= tapUntil) { tapRegion = null; updateRepeating() } }, 3100)
    }

    // ---- frames ----

    private fun onFrame(image: Image) {
        val now = System.nanoTime()
        try {
            capW = image.width; capH = image.height
            lastFrameNs = now
            rebindAttempted = false
            if (windowStart == 0L) windowStart = now
            windowCount++
            if (now - windowStart >= 1_000_000_000L) {
                fps = windowCount * 1e9 / (now - windowStart)
                Log.i(TAG, "capture fps=${"%.1f".format(fps)} ${image.width}x${image.height} scan=$scanning peer=${sink.wantsFrames} rec=${recSurface != null}")
                windowStart = now; windowCount = 0
            }
            if (!framesSeen) {
                framesSeen = true
                Log.i(TAG, "frames flowing ${image.width}x${image.height}")
                main.post { emit(state("previewing")); flushWaiters(true) }
            }
            // ~5 decodes/s on a copy and a separate thread: WebRTC feeding never waits for ZXing.
            if (scanning && now - lastScanNs >= 200_000_000L && decoding.compareAndSet(false, true)) {
                lastScanNs = now
                val y = image.planes[0]
                val luma = QrScanner.tightLuma(y.buffer, y.rowStride, image.width, image.height)
                val w = image.width; val h = image.height
                scanHandler.post {
                    try {
                        qr.decode(luma, w, h)?.let { text ->
                            val t = System.nanoTime()
                            if (text != lastText || t - lastTextNs > 1_500_000_000L) {
                                lastText = text; lastTextNs = t
                                emit(mapOf("event" to "scan", "text" to text))
                            }
                        }
                    } finally { decoding.set(false) }
                }
            }
        } catch (e: Throwable) {
            decoding.set(false)
        }
        if (sink.wantsFrames) sink.onFrame(image, streamRotation) else image.close()
    }

    // No frames for 3 s while open and foreground: report, reopen once.
    private val watchdog = object : Runnable {
        override fun run() {
            if (!started) return
            val fg = owner.lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.STARTED)
            if (!fg || device == null) lastFrameNs = System.nanoTime()
            if (fg && device != null && System.nanoTime() - lastFrameNs > 3_000_000_000L) {
                Log.w(TAG, "no frames for 3 s (rebindAttempted=$rebindAttempted)")
                emit(error("CAMERA_FAILED"))
                framesSeen = false
                if (!rebindAttempted) {
                    rebindAttempted = true
                    closeDevice(); lastFrameNs = System.nanoTime(); openCamera()
                }
            }
            main.postDelayed(this, 1000)
        }
    }

    /** 16:9 size at least [w] wide, closest to it; else the smallest. */
    private fun pick(sizes: Array<Size>, w: Int, h: Int): Size {
        val ar = w.toDouble() / h
        val same = sizes.filter { kotlin.math.abs(it.width.toDouble() / it.height - ar) < 0.02 && it.width >= w }
        return same.minByOrNull { it.width * it.height } ?: sizes.maxByOrNull { it.width * it.height }!!
    }

    private fun failed(code: String) {
        lastFailure = "Phone camera error: $code"
        emit(error(code))
        flushWaiters(false)
    }

    /** Frames delivered by the YUV stream, measured; null when not running. */
    fun captureStats(): Map<String, Any?>? {
        val w = capW; val h = capH
        if (!started || !framesSeen || w == null || h == null) return null
        return mapOf("width" to w, "height" to h, "fps_measured" to fps)
    }

    private fun state(s: String) = mapOf("event" to "state", "state" to s, "error" to null)
    private fun error(code: String) = mapOf("event" to "state", "state" to "error", "error" to code)
}
