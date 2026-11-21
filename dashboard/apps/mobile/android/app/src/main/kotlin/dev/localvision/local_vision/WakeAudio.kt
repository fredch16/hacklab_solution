package dev.localvision.local_vision

/** Bounded PCM replay for audio spoken while the keyword decoder confirms a hit. */
internal class WakeAudio(capacity: Int) {
    private val buffer = FloatArray(capacity)
    private var next = 0
    private var size = 0

    fun append(samples: FloatArray) {
        for (sample in samples) {
            buffer[next] = sample
            next = (next + 1) % buffer.size
        }
        size = minOf(buffer.size, size + samples.size)
    }

    fun samples(): FloatArray =
        FloatArray(size) { buffer[(next - size + buffer.size + it) % buffer.size] }

    fun clear() {
        size = 0
        next = 0
    }
}
