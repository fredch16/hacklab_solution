package dev.localvision.local_vision

import android.content.res.AssetManager
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig

/** Runs on the voice worker. Only the selected phrase can dispatch a wake. */
internal class WakeDetector(private val assets: AssetManager) {
    companion object {
        const val PHONE_MODEL = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
        const val BPE_MODEL = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
        private const val TAG = "WAKE"
    }

    private val phoneDirectory = "voice/kws-phone"
    private val bpeDirectory = "voice/kws"
    private val pronunciation by lazy {
        val lexicon = assets.open("$phoneDirectory/en.phone").bufferedReader().useLines(WakePhrase::loadPronunciations)
        val rules = assets.open("$phoneDirectory/cmu-lts.bin").use { it.readBytes() }
        WakePronunciation(lexicon, rules)
    }
    private val vocab by lazy {
        assets.open("$bpeDirectory/bpe.vocab").bufferedReader().useLines(WakePhrase::loadVocab)
    }
    private val pieces by lazy {
        assets.open("$bpeDirectory/tokens.txt").bufferedReader().use { WakePhrase.tokensOf(it.readLines()) }
    }
    private var active: KeywordSpotter? = null
    private var phoneme = false
    private var stream: OnlineStream? = null
    private var spec = ""

    val modelName get() = if (phoneme) PHONE_MODEL else BPE_MODEL

    fun configure(phrase: String): Boolean {
        val phonePhrases = pronunciation.phrases(phrase)
        val usePhones = phonePhrases.isNotEmpty()
        val configuredSpec = if (usePhones) phonePhrases.joinToString("/") { "$it @$TAG" }
        else WakePhrase.tokenize(phrase, vocab, pieces)
            ?.joinToString(" ", postfix = " @$TAG") ?: return false
        if (active == null || phoneme != usePhones) {
            close()
            val dir = if (usePhones) phoneDirectory else bpeDirectory
            active = KeywordSpotter(assets, KeywordSpotterConfig(
                featConfig = FeatureConfig(sampleRate = 16000, featureDim = 80),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = "$dir/encoder.onnx", decoder = "$dir/decoder.onnx", joiner = "$dir/joiner.onnx"),
                    tokens = "$dir/tokens.txt", numThreads = 1, modelType = "zipformer2"),
                keywordsFile = "$bpeDirectory/keywords.txt",
                maxActivePaths = if (usePhones) 12 else 4,
                numTrailingBlanks = if (usePhones) 0 else 1,
                keywordsScore = if (usePhones) 3.0f else 2.5f,   // generous: the owner prefers a false wake over a missed one (far from the mic)
                keywordsThreshold = if (usePhones) 0.08f else 0.12f,
            ))
            phoneme = usePhones
        }
        spec = configuredSpec
        rearm()
        return true
    }

    fun rearm() {
        stop()
        stream = active?.createStream(spec)
    }

    fun accept(samples: FloatArray): Boolean {
        val k = active ?: return false
        val s = stream ?: return false
        s.acceptWaveform(samples, 16000)
        while (k.isReady(s)) {
            k.decode(s)
            if (k.getResult(s).keyword == TAG) return true
        }
        return false
    }

    private val recordingKeywords by lazy {
        val phrases = listOf(
            "start recording" to "START", "start the recording" to "START",
            "begin recording" to "START", "begin the recording" to "START",
            "stop recording" to "STOP", "stop the recording" to "STOP",
            "end recording" to "STOP", "end the recording" to "STOP",
            "start phone recording" to "START", "start phone camera recording" to "START",
            "start camera recording" to "START", "begin camera recording" to "START",
            "stop phone recording" to "STOP", "stop phone camera recording" to "STOP",
            "stop camera recording" to "STOP", "end camera recording" to "STOP",
            "start robot recording" to "ROBOT_START", "start robot camera recording" to "ROBOT_START",
            "stop robot recording" to "ROBOT_STOP", "stop robot camera recording" to "ROBOT_STOP",
            "end robot recording" to "ROBOT_STOP", "end robot camera recording" to "ROBOT_STOP",
            "stout recording" to "BLOCK",
        )
        phrases.joinToString("/") { (phrase, label) ->
            val phones = phrase.uppercase(java.util.Locale.ROOT).split(' ').joinToString(" ") {
                pronunciation.candidates(it).first()
            }
            "$phones #0.25 @$label"
        }
    }

    private val namedRecordingKeywords by lazy {
        listOf(
            "phone camera recording" to "START", "phone recording" to "START",
            "robot camera recording" to "ROBOT_START", "robot recording" to "ROBOT_START",
        ).joinToString("/") { (phrase, label) ->
            val phones = phrase.uppercase(java.util.Locale.ROOT).split(' ').joinToString(" ") {
                pronunciation.candidates(it).first()
            }
            "$phones #0.25 @$label"
        }
    }

    private val recordKeyword by lazy {
        pronunciation.candidates("RECORD").joinToString("/") { "$it #0.25 @RECORD" }
    }

    /** Constrained second transcription for acoustically similar recording phrases. */
    fun transcribeRecordingPhrase(samples: FloatArray): String? {
        val k = active ?: return null
        if (!phoneme) return null
        return when (firstKeyword(k, recordingKeywords, samples) ?: firstKeyword(k, namedRecordingKeywords, samples)) {
            "START" -> "start recording"
            "STOP" -> "stop recording"
            "ROBOT_START" -> "start robot recording"
            "ROBOT_STOP" -> "stop robot recording"
            "BLOCK" -> null
            else -> if (firstKeyword(k, recordKeyword, samples) == "RECORD") "record" else null
        }
    }

    private fun firstKeyword(k: KeywordSpotter, keywords: String, samples: FloatArray): String? {
        val commandStream = k.createStream(keywords)
        try {
            commandStream.acceptWaveform(samples, 16000)
            while (k.isReady(commandStream)) {
                k.decode(commandStream)
                val keyword = k.getResult(commandStream).keyword
                if (keyword.isNotEmpty()) return keyword
            }
            return null
        } finally {
            commandStream.release()
        }
    }

    fun stop() {
        stream?.release()
        stream = null
    }

    fun close() {
        stop()
        active?.release()
        active = null
    }
}
