package dev.localvision.local_vision

import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.camera.core.ImageProxy
import org.webrtc.CapturerObserver
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.JavaI420Buffer
import org.webrtc.MediaConstraints
import org.webrtc.MediaStreamTrack
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RTCStatsReport
import org.webrtc.RtpParameters
import org.webrtc.RtpTransceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.VideoFrame
import org.webrtc.VideoSource
import org.webrtc.VideoTrack

/** Receives camera frames; must close every image it is given. */
interface FrameSink {
    val wantsFrames: Boolean
    fun onFrame(image: ImageProxy)
}

/**
 * Native libwebrtc sender. Frames come from CameraSession (the single camera
 * owner) and are only converted while a peer exists. One peer at a time.
 */
private fun Handler.later(block: () -> Unit) { post(block) }

class VisionStream(
    private val appContext: Context,
    private val emit: (Map<String, Any?>) -> Unit,
) : FrameSink {
    private val main = Handler(Looper.getMainLooper())
    private var egl: EglBase? = null
    private var factory: PeerConnectionFactory? = null
    private var source: VideoSource? = null
    private var track: VideoTrack? = null
    private var capturer: CapturerObserver? = null
    private var peer: PeerConnection? = null
    private var peerGen = 0
    private var connected = false
    private var lastBytes: Double? = null
    private var lastBytesUs: Long? = null
    private var disconnectCheck: Runnable? = null

    @Volatile
    override var wantsFrames = false
        private set

    val hasPeer: Boolean get() = peer != null

    private fun ensureFactory(): PeerConnectionFactory {
        factory?.let { return it }
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext).createInitializationOptions()
        )
        val e = EglBase.create()
        egl = e
        val f = PeerConnectionFactory.builder()
            .setVideoEncoderFactory(DefaultVideoEncoderFactory(e.eglBaseContext, true, false))
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(e.eglBaseContext))
            .createPeerConnectionFactory()
        val s = f.createVideoSource(false)
        s.capturerObserver.onCapturerStarted(true)
        capturer = s.capturerObserver
        source = s
        track = f.createVideoTrack("lvc-cam", s).also { it.setEnabled(true) }
        factory = f
        return f
    }

    override fun onFrame(image: ImageProxy) {
        try {
            val cap = capturer ?: return
            val w = image.width
            val h = image.height
            val p = image.planes
            val buf = JavaI420Buffer.allocate(w, h)
            YuvConverter.toI420(
                w, h,
                Plane(p[0].buffer, p[0].rowStride, p[0].pixelStride),
                Plane(p[1].buffer, p[1].rowStride, p[1].pixelStride),
                Plane(p[2].buffer, p[2].rowStride, p[2].pixelStride),
                buf.dataY, buf.strideY, buf.dataU, buf.strideU, buf.dataV, buf.strideV,
            )
            val frame = VideoFrame(buf, image.imageInfo.rotationDegrees, System.nanoTime())
            cap.onFrameCaptured(frame)
            frame.release()
        } finally {
            image.close()
        }
    }

    /** Main thread. Replaces any existing peer. [done] gets the answer SDP or an error code. */
    fun offer(sdp: String, done: (answer: String?, code: String?, message: String?) -> Unit) {
        if (peer != null) stop("replaced")
        val f: PeerConnectionFactory
        try {
            f = ensureFactory()
        } catch (e: Throwable) {
            done(null, "NEGOTIATION_FAILED", "WebRTC init failed")
            return
        }
        val gen = ++peerGen
        var finished = false
        var gathered = false
        var pending: (() -> Unit)? = null
        fun fail(msg: String) {
            if (finished) return
            finished = true
            if (gen == peerGen) {
                stop("error", failure = "NEGOTIATION_FAILED")
            }
            done(null, "NEGOTIATION_FAILED", msg)
        }

        val cfg = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
        }
        val pc = f.createPeerConnection(cfg, object : PeerConnection.Observer {
            override fun onSignalingChange(s: PeerConnection.SignalingState?) {}
            override fun onIceConnectionChange(s: PeerConnection.IceConnectionState?) {}
            override fun onIceConnectionReceivingChange(b: Boolean) {}
            override fun onIceGatheringChange(s: PeerConnection.IceGatheringState?) {
                if (s == PeerConnection.IceGatheringState.COMPLETE) {
                    main.post { gathered = true; pending?.invoke() }
                }
            }
            override fun onIceCandidate(c: org.webrtc.IceCandidate?) {}
            override fun onIceCandidatesRemoved(c: Array<out org.webrtc.IceCandidate>?) {}
            override fun onAddStream(s: org.webrtc.MediaStream?) {}
            override fun onRemoveStream(s: org.webrtc.MediaStream?) {}
            override fun onDataChannel(d: org.webrtc.DataChannel?) {}
            override fun onRenegotiationNeeded() {}
            override fun onAddTrack(r: org.webrtc.RtpReceiver?, s: Array<out org.webrtc.MediaStream>?) {}
            override fun onConnectionChange(s: PeerConnection.PeerConnectionState?) {
                main.post { if (gen == peerGen) onConnectionState(s) }
            }
        })
        if (pc == null) {
            fail("createPeerConnection returned null")
            return
        }
        peer = pc
        connected = false
        lastBytes = null
        wantsFrames = true
        emit(mapOf("event" to "stream", "state" to "connecting", "error" to null))

        pc.setRemoteDescription(object : Sdp() {
            override fun onSetSuccess() = main.later {
                if (gen != peerGen) return@later
                val t = pc.transceivers.firstOrNull {
                    it.mediaType == MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO
                }
                if (t == null) return@later fail("offer has no video section")
                t.sender.setTrack(track, false)
                t.setDirection(RtpTransceiver.RtpTransceiverDirection.SEND_ONLY)
                pc.createAnswer(object : Sdp() {
                    override fun onCreateSuccess(d: SessionDescription) = main.later {
                        if (gen != peerGen) return@later
                        pc.setLocalDescription(object : Sdp() {
                            override fun onSetSuccess() = main.later {
                                if (gen != peerGen) return@later
                                applySenderParams(t)
                                val finish = Runnable {
                                    if (finished || gen != peerGen) return@Runnable
                                    finished = true
                                    val desc = pc.localDescription
                                    if (desc == null) { finished = false; fail("no local description") }
                                    else done(desc.description, null, null)
                                }
                                pending = { finish.run() }
                                if (gathered || pc.iceGatheringState() == PeerConnection.IceGatheringState.COMPLETE) finish.run()
                                else main.postDelayed(finish, 3000) // send what was gathered
                            }
                            override fun onSetFailure(e: String?) = main.later { fail("setLocalDescription: $e") }
                        }, d)
                    }
                    override fun onCreateFailure(e: String?) = main.later { fail("createAnswer: $e") }
                }, MediaConstraints())
            }
            override fun onSetFailure(e: String?) = main.later { fail("setRemoteDescription: $e") }
        }, SessionDescription(SessionDescription.Type.OFFER, sdp))
    }

    private fun applySenderParams(t: RtpTransceiver) {
        val p = t.sender.parameters
        if (p.encodings.isNotEmpty()) {
            p.encodings[0].maxBitrateBps = 1_200_000
            p.encodings[0].maxFramerate = 30
        }
        p.degradationPreference = RtpParameters.DegradationPreference.BALANCED
        t.sender.setParameters(p)
    }

    private fun onConnectionState(s: PeerConnection.PeerConnectionState?) {
        when (s) {
            PeerConnection.PeerConnectionState.CONNECTED -> {
                connected = true
                disconnectCheck?.let { main.removeCallbacks(it) }
                emit(mapOf("event" to "stream", "state" to "connected", "error" to null))
            }
            PeerConnection.PeerConnectionState.DISCONNECTED -> {
                connected = false
                val r = Runnable { if (!connected && peer != null) stop("ice_failed", failure = "ICE_FAILED") }
                disconnectCheck = r
                main.postDelayed(r, 5000)
            }
            PeerConnection.PeerConnectionState.FAILED, PeerConnection.PeerConnectionState.CLOSED ->
                if (peer != null) stop("ice_failed", failure = "ICE_FAILED")
            else -> {}
        }
    }

    /** Tears the peer down (if any). The camera is never touched. */
    fun stop(reason: String, failure: String? = null) {
        val pc = peer ?: return
        peer = null
        peerGen++
        wantsFrames = false
        connected = false
        disconnectCheck?.let { main.removeCallbacks(it) }
        try { pc.close(); pc.dispose() } catch (_: Throwable) {}
        emit(mapOf("event" to "webrtc_stopped", "reason" to reason))
        emit(mapOf("event" to "stream", "state" to if (failure != null) "failed" else "idle", "error" to failure))
    }

    fun dispose() {
        stop("session_ended")
        track?.dispose(); source?.dispose(); factory?.dispose(); egl?.release()
        track = null; source = null; factory = null; egl = null; capturer = null
    }

    /** Calls [cb] on the main thread with the contract `stream` stats, or null. */
    fun collectStats(cb: (Map<String, Any?>?) -> Unit) {
        val pc = peer
        if (pc == null || !connected) { cb(null); return }
        pc.getStats { report -> main.post { cb(parse(report)) } }
    }

    private fun num(v: Any?): Double? = (v as? Number)?.toDouble()

    private fun parse(r: RTCStatsReport): Map<String, Any?>? {
        val all = r.statsMap
        val out = all.values.firstOrNull { it.type == "outbound-rtp" && it.members["kind"] == "video" } ?: return null
        val m = out.members
        var bitrate: Double? = null
        val bytes = num(m["bytesSent"])
        if (bytes != null) {
            val pb = lastBytes; val pt = lastBytesUs
            if (pb != null && pt != null && out.timestampUs > pt) {
                bitrate = (bytes - pb) * 8 * 1_000_000 / (out.timestampUs - pt)
            }
            lastBytes = bytes; lastBytesUs = out.timestampUs.toLong()
        }
        val codec = (all[m["codecId"]]?.members?.get("mimeType") as? String)?.substringAfter('/')
        val pairId = all.values.firstOrNull { it.type == "transport" }?.members?.get("selectedCandidatePairId") as? String
        val pair = (pairId?.let { all[it] }) ?: all.values.firstOrNull {
            it.type == "candidate-pair" && it.members["nominated"] == true && it.members["state"] == "succeeded"
        }
        val rtt = pair?.let { num(it.members["currentRoundTripTime"]) }
        return mapOf(
            "codec" to codec,
            "encoder" to (m["encoderImplementation"] as? String),
            "width" to num(m["frameWidth"])?.toInt(),
            "height" to num(m["frameHeight"])?.toInt(),
            "fps" to num(m["framesPerSecond"]),
            "bitrate_bps" to bitrate?.toLong(),
            "quality_limitation" to (m["qualityLimitationReason"] as? String),
            "rtt_ms" to rtt?.let { it * 1000 },
            "local_candidate" to pair?.let { all[it.members["localCandidateId"]]?.members?.get("candidateType") as? String },
            "remote_candidate" to pair?.let { all[it.members["remoteCandidateId"]]?.members?.get("candidateType") as? String },
        )
    }

    private open class Sdp : SdpObserver {
        override fun onCreateSuccess(d: SessionDescription) {}
        override fun onSetSuccess() {}
        override fun onCreateFailure(e: String?) {}
        override fun onSetFailure(e: String?) {}
    }
}
