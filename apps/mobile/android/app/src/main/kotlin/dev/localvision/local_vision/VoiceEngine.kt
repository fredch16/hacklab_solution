package dev.localvision.local_vision

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.SystemClock
import android.util.Log
import androidx.core.content.ContextCompat
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineRecognizer
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.LinkedBlockingQueue

/**
 * Offline voice: wake phrase (sherpa-onnx KWS, phrase tokenized on the phone) ->
 * command transcription (streaming ASR) ended by Silero VAD silence. Fed by the
 * single AudioCaptureBus; all inference runs on one worker thread, the bus
 * thread only enqueues a copy. No audio leaves the phone; transcripts live in
 * memory for the session only.
 */
class VoiceEngine(
    private val ctx: Context,
    private val audio: AudioCaptureBus,
    private val emit: (Map<String, Any?>) -> Unit,
) : PcmSubscriber {
    companion object {
        private const val TAG = "LVC"
        const val KWS_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
        const val ASR_NAME = "sherpa-onnx-streaming-zipformer-en-20M-2023-02-17"
        private const val KWS_BOOST = 1.5f
        private const val KWS_THRESHOLD = 0.25f
        private const val WINDOW = 512
    }

    private val prefs = ctx.getSharedPreferences("lvc_voice", Context.MODE_PRIVATE)
    private val queue = LinkedBlockingQueue<Any>(300) // ByteArray (48 kHz PCM) or Runnable
    private var worker: Thread? = null
    @Volatile private var sessionActive = false

    // state (published under lock)
    private val lock = Any()
    private var state = "unavailable"
    private var error: String? = null
    private var phrase: String? = prefs.getString("wake_phrase", null)
    private var muted = false
    private val transcripts = ArrayList<Map<String, Any?>>()
    private var subscribed = false
    private var resampler: Resampler? = null

    // models (worker thread only)
    private var kws: KeywordSpotter? = null
    private var kwsStream: OnlineStream? = null
    private var asr: OnlineRecognizer? = null
    private var asrStream: OnlineStream? = null
    private var vad: Vad? = null
    private var bpeVocab: Map<String, Float> = emptyMap()
    private var kwsTokens: Set<String> = emptySet()
    private val tracker = UtteranceTracker()
    private var audioMs = 0L
    private var cooldownUntil = 0L
    private val vadBuf = FloatArray(WINDOW)
    private var vadFill = 0
    private var uttId = ""
    private var uttStartedUtc = ""
    private var lastPartial = ""
    private var lastPartialMs = -1000L
    private var wakeAudioMs = 0L
    private var wakeToLog = 0L

    // ---- public API (any thread) ----

    fun snapshot(): Map<String, Any?> = synchronized(lock) {
        mapOf(
            "state" to state, "error" to error, "wake_phrase" to phrase, "language" to "en",
            "model" to (if (phrase != null) mapOf("kws" to KWS_NAME, "asr" to ASR_NAME, "vad" to "silero") else null),
            "transcripts" to transcripts.toList(),
        )
    }

    /** Session start: begin listening if a phrase is set and the microphone is allowed. */
    fun start() {
        sessionActive = true
        beginIfPossible()
    }

    /** Stop listening (stream ended). [clear] also forgets this session's transcripts (session end). */
    fun stop(clear: Boolean = false) {
        sessionActive = false
        enqueue(Runnable {
            unsubscribe()
            endUtteranceQuietly()
        })
        synchronized(lock) { if (clear) transcripts.clear(); error = null; state = "unavailable" }
        publish()
    }

    /** Null on success, else INVALID_WAKE_PHRASE. */
    fun setPhrase(raw: String?): String? {
        val p = WakePhrase.validate(raw) ?: return "INVALID_WAKE_PHRASE"
        prefs.edit().putString("wake_phrase", p).apply()
        synchronized(lock) { phrase = p; if (error == "INVALID_WAKE_PHRASE") error = null }
        if (sessionActive) {
            if (worker == null) beginIfPossible() else {
                setState("loading_model", null)
                enqueue(Runnable { loadKeywords(); if (!muted) setState("listening", null) })
            }
        } else publish() // phrase shows in the snapshot even while not listening
        return null
    }

    fun setMuted(m: Boolean): String? {
        synchronized(lock) { muted = m }
        if (!sessionActive || phrase == null) { publish(); return null }
        if (m) {
            enqueue(Runnable { unsubscribe(); endUtteranceQuietly() })
            setState("muted", null)
        } else {
            enqueue(Runnable { if (subscribe()) { resetKws(); setState("listening", null) } })
        }
        return null
    }

    // ---- bus callback (bus thread: copy and return) ----

    override fun onPcm(data: ByteArray, size: Int, ptsUs: Long) {
        queue.offer(data.copyOf(size)) // full queue: drop, never block the bus
    }

    // ---- lifecycle ----

    private fun enqueue(r: Runnable) { queue.put(r) }

    private fun beginIfPossible() {
        val p = synchronized(lock) { phrase }
        if (p == null) { setState("unavailable", null); return }
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            setState("error", "PERMISSION_MIC"); return
        }
        if (synchronized(lock) { muted }) { setState("muted", null); return }
        setState("loading_model", null)
        if (worker == null) {
            worker = Thread({ loop() }, "lvc-voice").also { it.start() }
        }
        enqueue(Runnable {
            if (!loadModels()) { setState("error", "MODEL_MISSING"); return@Runnable }
            if (!subscribe()) { setState("error", "PERMISSION_MIC"); return@Runnable }
            setState("listening", null)
        })
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
        synchronized(lock) { state = s; error = err }
        if (s == "listening") Log.i(TAG, "voice listening")
        publish()
    }

    private fun publish() { emit(mapOf("event" to "voice", "voice" to snapshot())) }

    // ---- worker ----

    private fun loop() {
        while (true) {
            try {
                when (val m = queue.take()) {
                    is Runnable -> m.run()
                    is ByteArray -> if (resampler != null) feed(m)
                }
            } catch (_: InterruptedException) { return } catch (t: Throwable) { Log.e(TAG, "voice worker: $t") }
        }
    }

    private fun feed(b: ByteArray) {
        if (synchronized(lock) { state } in listOf("muted", "unavailable", "error", "loading_model")) return
        feedFloats(resampler!!.process(b, b.size))
    }

    private fun assetLines(path: String): List<String> = ctx.assets.open(path).bufferedReader().readLines()

    private fun loadModels(): Boolean {
        if (kws != null) { loadKeywords(); return true }
        return try {
            val t0 = SystemClock.elapsedRealtime()
            val am = ctx.assets
            bpeVocab = WakePhrase.loadVocab(assetLines("voice/kws/bpe.vocab").asSequence())
            kwsTokens = WakePhrase.tokensOf(assetLines("voice/kws/tokens.txt"))
            kws = KeywordSpotter(am, KeywordSpotterConfig(
                featConfig = FeatureConfig(sampleRate = 16000, featureDim = 80),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = "voice/kws/encoder.onnx", decoder = "voice/kws/decoder.onnx", joiner = "voice/kws/joiner.onnx"),
                    tokens = "voice/kws/tokens.txt", numThreads = 2, modelType = "zipformer2"),
                keywordsFile = "voice/kws/keywords.txt", // required at construction; the real phrase is a per-stream keyword
                keywordsScore = KWS_BOOST, keywordsThreshold = KWS_THRESHOLD,
            ))
            asr = OnlineRecognizer(am, OnlineRecognizerConfig(
                featConfig = FeatureConfig(sampleRate = 16000, featureDim = 80),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = "voice/asr/encoder.onnx", decoder = "voice/asr/decoder.onnx", joiner = "voice/asr/joiner.onnx"),
                    tokens = "voice/asr/tokens.txt", numThreads = 2),
                enableEndpoint = false, decodingMethod = "greedy_search",
            ))
            vad = Vad(am, VadModelConfig(
                sileroVadModelConfig = SileroVadModelConfig(
                    model = "voice/vad/silero_vad.onnx", threshold = 0.5f, minSilenceDuration = 0.25f,
                    minSpeechDuration = 0.1f, windowSize = WINDOW, maxSpeechDuration = 30f),
                sampleRate = 16000, numThreads = 1))
            Log.i(TAG, "voice models loaded in ${SystemClock.elapsedRealtime() - t0} ms")
            loadKeywords()
        } catch (t: Throwable) {
            Log.e(TAG, "voice model load failed: ${t.javaClass.simpleName} ${t.message}")
            false
        }
    }

    /** (Re)creates the KWS stream for the current phrase. False when it cannot be tokenized. */
    private fun loadKeywords(): Boolean {
        val k = kws ?: return false
        val p = synchronized(lock) { phrase } ?: return false
        val tokens = WakePhrase.tokenize(p, bpeVocab, kwsTokens)
        if (tokens == null) {
            synchronized(lock) { error = "INVALID_WAKE_PHRASE" }
            setState("error", "INVALID_WAKE_PHRASE"); return false
        }
        kwsStream?.release()
        val spec = tokens.joinToString(" ") + " :$KWS_BOOST #$KWS_THRESHOLD @kw"
        kwsStream = k.createStream(spec)
        Log.i(TAG, "kws phrase='$p' tokens=${tokens.joinToString(" ")}")
        return true
    }

    private fun resetKws() {
        val k = kws ?: return
        kwsStream?.let { k.reset(it) }
        if (kwsStream == null) loadKeywords()
    }

    private fun feedFloats(f: FloatArray) {
        val st = synchronized(lock) { state }
        if (st == "muted" || st == "unavailable" || st == "error" || st == "loading_model") return
        audioMs += f.size / 16
        if (audioMs < cooldownUntil && st == "listening") return
        if (st == "listening") listen(f) else recognize(f)
    }

    private fun listen(f: FloatArray) {
        val k = kws ?: return
        val s = kwsStream ?: return
        val t0 = SystemClock.elapsedRealtimeNanos()
        s.acceptWaveform(f, 16000)
        while (k.isReady(s)) k.decode(s)
        val hit = k.getResult(s).keyword
        wakeToLog += SystemClock.elapsedRealtimeNanos() - t0
        if (hit.isNotEmpty()) onWake()
    }

    private fun onWake() {
        val now = System.currentTimeMillis()
        val a = asr ?: return
        kws?.let { k -> kwsStream?.let { k.reset(it) } }
        uttId = "utt-$now"
        uttStartedUtc = iso(now)
        asrStream?.release()
        asrStream = a.createStream("")
        vad?.reset(); vadFill = 0
        tracker.start(audioMs)
        lastPartial = ""; lastPartialMs = -1000
        wakeAudioMs = audioMs
        Log.i(TAG, "WAKE $uttId (kws time since start of utterance window ${wakeToLog / 1_000_000} ms total)")
        wakeToLog = 0
        setState("wake_detected", null)
        emit(mapOf("event" to "voice_event", "type" to "voice.wake_detected",
            "payload" to mapOf("utterance_id" to uttId, "phrase" to synchronized(lock) { phrase }, "timestamp_utc" to uttStartedUtc)))
    }

    private fun recognize(f: FloatArray) {
        val a = asr ?: return
        val s = asrStream ?: return
        val v = vad
        s.acceptWaveform(f, 16000)
        while (a.isReady(s)) a.decode(s)
        // VAD in fixed windows; the tracker decides when the command ends
        var speech = false
        var i = 0
        var end: String? = null
        while (i < f.size && end == null) {
            val n = minOf(WINDOW - vadFill, f.size - i)
            System.arraycopy(f, i, vadBuf, vadFill, n)
            vadFill += n; i += n
            if (vadFill == WINDOW) {
                vadFill = 0
                v?.let { it.acceptWaveform(vadBuf); speech = it.isSpeechDetected(); while (!it.empty()) it.pop() }
                end = tracker.update(audioMs, speech)
            }
        }
        if (synchronized(lock) { state } == "wake_detected" && audioMs - wakeAudioMs >= 300) setState("recognizing", null)
        val text = pretty(a.getResult(s).text)
        if (end == null && text != lastPartial && audioMs - lastPartialMs >= 200) {
            lastPartial = text; lastPartialMs = audioMs
            emit(mapOf("event" to "voice_event", "type" to "voice.transcript.partial",
                "payload" to mapOf("utterance_id" to uttId, "text" to text)))
        }
        if (end != null) endUtterance(end)
    }

    private fun endUtterance(reason: String) {
        val a = asr ?: return
        val s = asrStream ?: return
        val t0 = SystemClock.elapsedRealtime()
        s.inputFinished()
        while (a.isReady(s)) a.decode(s)
        val text = if (reason == "no_speech") "" else pretty(a.getResult(s).text)
        val latency = SystemClock.elapsedRealtime() - t0
        val ended = iso(System.currentTimeMillis())
        Log.i(TAG, "FINAL $uttId reason=$reason asr_tail_latency=${latency}ms text='$text'")
        s.release(); asrStream = null
        synchronized(lock) {
            transcripts.add(0, mapOf("id" to uttId, "text" to text, "started_utc" to uttStartedUtc, "ended_utc" to ended, "end_reason" to reason))
            while (transcripts.size > 50) transcripts.removeAt(transcripts.size - 1)
        }
        emit(mapOf("event" to "voice_event", "type" to "voice.transcript.final",
            "payload" to mapOf("utterance_id" to uttId, "text" to text, "started_utc" to uttStartedUtc,
                "ended_utc" to ended, "end_reason" to reason, "asr_latency_ms" to latency)))
        cooldownUntil = audioMs + 1000
        kwsStream?.let { kws?.reset(it) }
        setState(if (synchronized(lock) { muted }) "muted" else "listening", null)
    }

    private fun endUtteranceQuietly() {
        asrStream?.release(); asrStream = null
        kwsStream?.let { kws?.reset(it) }
    }

    private fun pretty(t: String): String {
        val s = t.trim().lowercase()
        return if (s.isEmpty()) s else s.substring(0, 1).uppercase() + s.substring(1)
    }

    private fun iso(ms: Long) = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(ms))

    fun dispose() {
        stop(clear = true)
        worker?.interrupt()
    }
}
