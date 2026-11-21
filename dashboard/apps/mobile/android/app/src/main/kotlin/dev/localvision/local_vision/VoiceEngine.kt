package dev.localvision.local_vision

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.SystemClock
import android.util.Log
import androidx.core.content.ContextCompat
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicLong

/** Offline keyword spotting and command transcription on the shared microphone tap. */
class VoiceEngine(
    private val ctx: Context,
    private val audio: AudioCaptureBus,
    private val emit: (Map<String, Any?>) -> Unit,
) : PcmSubscriber {
    companion object {
        private const val TAG = "LVC"
        private const val REPLAY_MS = 1250
        private const val COMMAND_BUFFER_SAMPLES = (UtteranceTracker.MAX_MS + REPLAY_MS + 100) * 16
        private const val WINDOW = 512
        private const val QUEUE_CHUNKS = 50 // 20 ms each; limit how far behind live audio inference can fall
    }

    private val prefs = ctx.getSharedPreferences("lvc_voice", Context.MODE_PRIVATE)
    private val queue = LinkedBlockingQueue<Any>(QUEUE_CHUNKS + 20) // ByteArray (48 kHz PCM) or Runnable
    private var worker: Thread? = null
    @Volatile private var sessionActive = false
    private val drops = AtomicLong()
    private val revision = AtomicLong()

    // state (published under lock)
    private val lock = Any()
    private var state = "unavailable"
    private var error: String? = null
    private var phrase: String? = WakePhrase.fromSaved(prefs.getString("wake_phrase", null))
    private var muted = false
    private var kwsName: String? = null
    private var denoiseName: String? = null
    private val transcripts = ArrayList<Map<String, Any?>>()
    private var subscribed = false
    private var resampler: Resampler? = null

    // worker thread only
    private var asr: CommandTranscriber? = null
    private var vad: Vad? = null
    private var denoiser: VoiceDenoiser? = null
    private val detector = WakeDetector(ctx.assets)
    private val replay = WakeAudio(REPLAY_MS * 16)
    private val commandAudio = WakeAudio(COMMAND_BUFFER_SAMPLES)
    private val speechGate = CommandSpeechGate()
    private var flow: VoiceFlow? = null
    private var runningRevision = -1L
    private var audioSamples = 0L
    private val audioMs get() = audioSamples / 16
    private val vadBuf = FloatArray(WINDOW)
    private var vadFill = 0
    private var uttId = ""
    private var uttStartedUtc = ""

    // load / throughput stats (worker thread)
    private var procNs = 0L
    private var statAudioMs = 0L
    private var statStartMs = 0L

    // ---- public API (any thread) ----

    fun snapshot(): Map<String, Any?> = synchronized(lock) {
        mapOf(
            "state" to state, "error" to error, "wake_phrase" to phrase, "language" to "en",
            "model" to (if (phrase != null) mapOf(
                "asr" to CommandTranscriber.NAME, "kws" to kwsName, "vad" to "silero", "denoiser" to denoiseName,
            ) else null),
            "transcripts" to transcripts.toList(),
        )
    }

    /** Stream started: begin listening if a phrase is set and the microphone is allowed. */
    fun start() {
        sessionActive = true
        beginIfPossible()
    }

    /** Stop listening (stream ended). [clear] also forgets this session's transcripts (session end). */
    fun stop(clear: Boolean = false) {
        sessionActive = false
        pause()
        synchronized(lock) { if (clear) transcripts.clear(); error = null; state = "unavailable" }
        publish()
    }

    /** Null on success, else INVALID_WAKE_PHRASE. */
    fun setPhrase(raw: String?): String? {
        val p = WakePhrase.validate(raw) ?: return "INVALID_WAKE_PHRASE"
        prefs.edit().putString("wake_phrase", p).apply()
        synchronized(lock) { phrase = p; kwsName = null; if (error == "INVALID_WAKE_PHRASE") error = null }
        if (sessionActive) beginIfPossible()
        publish()
        return null
    }

    fun setMuted(m: Boolean): String? {
        synchronized(lock) { muted = m }
        if (sessionActive) beginIfPossible() else publish()
        return null
    }

    // ---- bus callback (bus thread: copy and return) ----

    override fun onPcm(data: ByteArray, size: Int, ptsUs: Long) {
        if (!queue.offer(data.copyOf(size))) drops.incrementAndGet() // never block the bus; drops are counted and logged
    }

    // ---- lifecycle ----

    private fun pause() {
        val generation = revision.incrementAndGet()
        if (worker != null) enqueueControl {
            if (generation == revision.get()) {
                unsubscribe()
                clearStreams()
            }
        }
    }

    private fun enqueueControl(block: () -> Unit) {
        queue.removeIf { it is ByteArray }
        queue.put(Runnable(block))
    }

    private fun beginIfPossible() {
        val p = synchronized(lock) { phrase }
        if (p == null || synchronized(lock) { muted }) {
            pause()
            setState(if (p == null) "unavailable" else "muted", null)
            return
        }
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pause()
            setState("error", "PERMISSION_MIC")
            return
        }
        val generation = revision.incrementAndGet()
        setState("loading_model", null)
        if (worker == null) worker = Thread({ loop() }, "lvc-voice").also { it.start() }
        enqueueControl {
            if (generation != revision.get()) return@enqueueControl
            unsubscribe()
            clearStreams()
            val configured = try {
                if (!loadModels()) "MODEL_MISSING"
                else if (!detector.configure(p)) "INVALID_WAKE_PHRASE"
                else null
            } catch (e: RuntimeException) {
                Log.e(TAG, "keyword model load failed", e)
                "MODEL_MISSING"
            }
            if (configured == null && denoiser == null) {
                denoiser = try {
                    VoiceDenoiser(ctx.assets)
                } catch (e: Exception) {
                    Log.w(TAG, "voice noise suppression unavailable", e)
                    null
                }
            }
            if (generation != revision.get()) return@enqueueControl
            val problem = configured ?: if (!subscribe()) "PERMISSION_MIC" else null
            if (problem != null) {
                unsubscribe()
                clearStreams()
                setState("error", problem)
                return@enqueueControl
            }
            flow = VoiceFlow(p)
            runningRevision = generation
            val denoiseModel = denoiser?.let { VoiceDenoiser.NAME }
            synchronized(lock) { kwsName = detector.modelName; denoiseName = denoiseModel }
            Log.i(TAG, "voice keyword model=${detector.modelName} denoiser=${denoiseModel ?: "off"}")
            setState("listening", null)
        }
    }

    private fun subscribe(): Boolean {
        if (subscribed) return true
        subscribed = audio.subscribe("voice", this)
        if (subscribed) resampler = Resampler(audio.channels)
        return subscribed
    }

    private fun unsubscribe() {
        if (subscribed) { audio.unsubscribe("voice"); subscribed = false }
        queue.removeIf { it is ByteArray }
    }

    private fun setState(s: String, err: String?) {
        if (synchronized(lock) { state == s && error == err }) return
        synchronized(lock) { state = s; error = err }
        if (s == "listening") Log.i(TAG, "voice listening")
        publish()
    }

    private fun publish() { emit(mapOf("event" to "voice", "voice" to snapshot())) }

    // ---- worker ----

    private fun loop() {
        try {
            while (!Thread.currentThread().isInterrupted) {
                try {
                    when (val m = queue.take()) {
                        is Runnable -> m.run()
                        is ByteArray -> if (sessionActive && runningRevision == revision.get()) feed(m)
                    }
                } catch (_: InterruptedException) {
                    return
                } catch (e: RuntimeException) {
                    Log.e(TAG, "voice worker failed", e)
                    unsubscribe()
                    clearStreams()
                    if (sessionActive) setState("error", "VOICE_PROCESSING_FAILED")
                }
            }
        } finally {
            unsubscribe()
            clearStreams()
            detector.close()
            asr?.release()
            vad?.release()
            denoiser?.release()
        }
    }

    private fun loadModels(): Boolean {
        if (asr != null && vad != null) return true
        return try {
            val t0 = SystemClock.elapsedRealtime()
            val am = ctx.assets
            asr = CommandTranscriber(am)
            vad = Vad(am, VadModelConfig(
                sileroVadModelConfig = SileroVadModelConfig(
                    model = "voice/vad/silero_vad.onnx", threshold = 0.5f, minSilenceDuration = 0.25f,
                    minSpeechDuration = 0.1f, windowSize = WINDOW, maxSpeechDuration = 30f),
                sampleRate = 16000, numThreads = 1))
            Log.i(TAG, "voice models loaded in ${SystemClock.elapsedRealtime() - t0} ms (asr=${CommandTranscriber.NAME})")
            true
        } catch (t: Throwable) {
            asr?.release(); asr = null
            vad?.release(); vad = null
            Log.e(TAG, "voice model load failed: ${t.javaClass.simpleName} ${t.message}")
            false
        }
    }

    private fun clearStreams() {
        detector.stop()
        flow = null
        replay.clear()
        commandAudio.clear()
        vadFill = 0
        vad?.reset()
        denoiser?.reset()
    }

    private fun feed(b: ByteArray) {
        val st = synchronized(lock) { state }
        if (st == "muted" || st == "unavailable" || st == "error" || st == "loading_model") return
        val t0 = SystemClock.elapsedRealtimeNanos()
        val f = resampler?.process(b, b.size) ?: return
        if (f.isNotEmpty()) process(f)
        procNs += SystemClock.elapsedRealtimeNanos() - t0
        statAudioMs += f.size / 16
        logStats()
    }

    private fun enhance(f: FloatArray): FloatArray =
        try {
            denoiser?.process(f) ?: f
        } catch (e: RuntimeException) {
            Log.w(TAG, "voice noise suppression failed, continuing with mic input", e)
            denoiser?.release()
            denoiser = null
            synchronized(lock) { denoiseName = null }
            publish()
            f
        }

    private fun process(f: FloatArray) {
        val fl = flow ?: return
        audioSamples += f.size
        for (act in fl.onClock(audioMs)) apply(act)
        when (fl.mode) {
            VoiceFlow.Mode.COOLDOWN -> return
            VoiceFlow.Mode.LISTENING -> {
                replay.append(f)
                if (detector.accept(f)) for (act in fl.onWake(audioMs)) apply(act)
            }
            VoiceFlow.Mode.COMMAND -> {
                commandAudio.append(f)
                detectCommandEnd(fl, enhance(f))
            }
        }
    }

    private fun detectCommandEnd(fl: VoiceFlow, f: FloatArray) {
        val v = vad ?: return
        var offset = 0
        while (offset < f.size && fl.mode == VoiceFlow.Mode.COMMAND) {
            val count = minOf(WINDOW - vadFill, f.size - offset)
            System.arraycopy(f, offset, vadBuf, vadFill, count)
            vadFill += count
            offset += count
            if (vadFill == WINDOW) {
                vadFill = 0
                v.acceptWaveform(vadBuf)
                val speech = speechGate.accept(vadBuf, speech = v.isSpeechDetected())
                while (!v.empty()) v.pop()
                for (act in fl.onVad(audioMs, speech)) apply(act)
            }
        }
    }

    private fun apply(act: VoiceFlow.Action) {
        if (!sessionActive || runningRevision != revision.get()) return
        when (act) {
            is VoiceFlow.Action.Wake -> onWake(act.phrase)
            is VoiceFlow.Action.End -> endUtterance(act.reason)
            VoiceFlow.Action.Rearm -> {
                detector.rearm()
                setState("listening", null)
            }
        }
    }

    private fun onWake(p: String) {
        detector.stop()
        val lead = replay.samples()
        commandAudio.clear()
        commandAudio.append(lead)
        replay.clear()
        val now = System.currentTimeMillis()
        uttId = "utt-$now"
        uttStartedUtc = iso(now)
        vad?.reset(); vadFill = 0
        Log.i(TAG, "WAKE $uttId")
        setState("wake_detected", null)
        emit(mapOf("event" to "voice_event", "type" to "voice.wake_detected",
            "payload" to mapOf("utterance_id" to uttId, "phrase" to p, "timestamp_utc" to uttStartedUtc)))
        denoiser?.reset()
        speechGate.start(enhance(lead))
    }

    private fun endUtterance(reason: String) {
        val a = asr ?: return
        val wakePhrase = flow?.phrase ?: return
        val t0 = SystemClock.elapsedRealtime()
        var acousticText: String? = null
        val text = if (reason == "no_speech") "" else {
            setState("recognizing", null)
            val samples = commandAudio.samples()
            val finalText = a.transcribe(samples, wakePhrase)
            if (reason == "silence") acousticText = detector.transcribeRecordingPhrase(samples)
            finalText
        }
        commandAudio.clear()
        val latency = SystemClock.elapsedRealtime() - t0
        val ended = iso(System.currentTimeMillis())
        if (!sessionActive || runningRevision != revision.get()) return
        Log.i(TAG, "FINAL $uttId reason=$reason asr_tail_latency=${latency}ms")
        synchronized(lock) {
            transcripts.add(0, mapOf("id" to uttId, "text" to text, "started_utc" to uttStartedUtc, "ended_utc" to ended, "end_reason" to reason))
            while (transcripts.size > 50) transcripts.removeAt(transcripts.size - 1)
        }
        emit(mapOf("event" to "voice_event", "type" to "voice.transcript.final",
            "payload" to mapOf("utterance_id" to uttId, "text" to text, "started_utc" to uttStartedUtc,
                "ended_utc" to ended, "end_reason" to reason, "asr_latency_ms" to latency,
                "acoustic_text" to acousticText)))
        setState("cooling_down", null)
    }

    /** Every 5 s: real-time factor of inference, dropped audio and queue lag. */
    private fun logStats() {
        val now = SystemClock.elapsedRealtime()
        if (statStartMs == 0L) statStartMs = now
        if (now - statStartMs < 5000) return
        val rtf = if (statAudioMs > 0) procNs / 1e6 / statAudioMs else 0.0
        Log.i(TAG, "voice rtf=${"%.2f".format(rtf)} drops=${drops.get()} lag_ms=${queue.size * 20} audio_ms=$statAudioMs")
        procNs = 0; statAudioMs = 0; statStartMs = now
    }

    private fun iso(ms: Long) = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(ms))

    fun dispose() {
        stop(clear = true)
        worker?.interrupt()
    }
}
