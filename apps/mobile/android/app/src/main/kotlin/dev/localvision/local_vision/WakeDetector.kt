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
    val replaySamples get() = if (phoneme) 5120 else 8960 // 320 / 560 ms at 16 kHz

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
                keywordsScore = 1.5f,
                keywordsThreshold = if (usePhones) 0.20f else 0.25f,
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
