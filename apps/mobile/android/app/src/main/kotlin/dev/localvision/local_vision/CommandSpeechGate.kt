package dev.localvision.local_vision

/** VAD also hears other speakers. Use the wake's level to ignore quieter speech for endpointing. */
internal class CommandSpeechGate {
    private var peakPower = 0f

    fun start(wakeAudio: FloatArray, window: Int = 512) {
        peakPower = 0f
        for (offset in 0..wakeAudio.size - window step window) {
            peakPower = maxOf(peakPower, power(wakeAudio, offset, window))
        }
    }

    fun accept(frame: FloatArray, speech: Boolean): Boolean {
        if (!speech) return false
        val level = power(frame, 0, frame.size)
        peakPower = maxOf(level, peakPower)
        return level >= peakPower * (0.35f * 0.35f)
    }

    private fun power(samples: FloatArray, offset: Int, count: Int): Float {
        var sum = 0f
        for (i in offset until offset + count) sum += samples[i] * samples[i]
        return sum / count
    }
}
