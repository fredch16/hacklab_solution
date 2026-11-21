package dev.localvision.local_vision

import android.content.res.AssetManager
import com.k2fsa.sherpa.onnx.OfflineModelConfig
import com.k2fsa.sherpa.onnx.OfflineRecognizer
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig
import com.k2fsa.sherpa.onnx.OfflineWhisperModelConfig

/** Decodes a bounded command utterance on the voice worker after endpoint detection. */
internal class CommandTranscriber(assets: AssetManager) {
    companion object {
        const val NAME = "whisper-base.en-int8"
        private const val ENCODER = "voice/asr/encoder.onnx"
        private const val DECODER = "voice/asr/decoder.onnx"
        private const val TOKENS = "voice/asr/tokens.txt"

        fun commandText(text: String, wakePhrase: String): String {
            val name = wakePhrase.substringAfter(' ').split(' ').joinToString("[\\s-]+", transform = Regex::escape)
            val leadingWake = Regex(
                """^\s*(?:(?:hey|hay|hi)[\s,-]+)?$name(?=$|[\s,!.?:])[\s,!.?:]*""",
                RegexOption.IGNORE_CASE,
            )
            return text.trim().trim('"', '“', '”').replaceFirst(leadingWake, "").trim()
        }
    }

    init {
        for (path in listOf(ENCODER, DECODER, TOKENS)) assets.open(path).close()
    }

    private val recognizer = OfflineRecognizer(assets, OfflineRecognizerConfig(
        modelConfig = OfflineModelConfig(
            whisper = OfflineWhisperModelConfig(encoder = ENCODER, decoder = DECODER, language = "en"),
            tokens = TOKENS,
            numThreads = 2,
        ),
    ))

    fun transcribe(samples: FloatArray, wakePhrase: String): String {
        val stream = recognizer.createStream()
        return try {
            stream.acceptWaveform(samples, 16000)
            recognizer.decode(stream)
            commandText(recognizer.getResult(stream).text, wakePhrase)
        } finally {
            stream.release()
        }
    }

    fun release() = recognizer.release()
}
