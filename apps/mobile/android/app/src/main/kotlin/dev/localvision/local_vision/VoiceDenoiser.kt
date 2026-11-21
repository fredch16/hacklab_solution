package dev.localvision.local_vision

import android.content.res.AssetManager
import com.k2fsa.sherpa.onnx.OfflineSpeechDenoiserDpdfNetModelConfig
import com.k2fsa.sherpa.onnx.OfflineSpeechDenoiserModelConfig
import com.k2fsa.sherpa.onnx.OnlineSpeechDenoiser
import com.k2fsa.sherpa.onnx.OnlineSpeechDenoiserConfig

/** Streaming enhancement of the 16 kHz voice tap, owned by the voice worker. */
internal class VoiceDenoiser(assets: AssetManager) {
    companion object {
        const val NAME = "dpdfnet-baseline"
        private const val MODEL = "voice/denoise/dpdfnet_baseline.onnx"
        private const val SAMPLE_RATE = 16000
    }

    init {
        // The native model loader assumes this asset exists.
        assets.open(MODEL).close()
    }

    private val denoiser = OnlineSpeechDenoiser(assets, OnlineSpeechDenoiserConfig(
        model = OfflineSpeechDenoiserModelConfig(
            dpdfnet = OfflineSpeechDenoiserDpdfNetModelConfig(model = MODEL),
            numThreads = 1,
        ),
    ))

    fun process(samples: FloatArray): FloatArray = denoiser.run(samples, SAMPLE_RATE).samples

    fun reset() = denoiser.reset()

    fun release() = denoiser.release()
}
