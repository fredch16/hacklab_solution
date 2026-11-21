package dev.localvision.local_vision

/**
 * Timing rules for one command after a wake hit. Pure logic on the *audio*
 * clock (ms of audio processed), so it is deterministic and testable.
 *  - end "silence": speech was heard and then >= 1.0 s without speech
 *  - end "max_duration": 15 s since the wake
 *  - end "no_speech": no speech within 5 s of the wake
 * Speech heard in the first 300 ms is the wake phrase itself and is ignored.
 */
class UtteranceTracker(
    private val silenceMs: Long = 1000,
    private val maxMs: Long = MAX_MS.toLong(),
    private val noSpeechMs: Long = 5_000,
    private val ignoreMs: Long = 300,
) {
    companion object { const val MAX_MS = 15_000 }

    private var wakeMs = 0L
    private var speechStarted = false
    private var lastSpeechMs = 0L

    val hasSpeech get() = speechStarted

    fun start(nowMs: Long) { wakeMs = nowMs; speechStarted = false; lastSpeechMs = nowMs }

    /** Returns the end reason, or null to keep going. */
    fun update(nowMs: Long, speech: Boolean): String? {
        if (speech && nowMs - wakeMs >= ignoreMs) { speechStarted = true; lastSpeechMs = nowMs }
        return when {
            nowMs - wakeMs >= maxMs -> "max_duration"
            speechStarted && nowMs - lastSpeechMs >= silenceMs -> "silence"
            !speechStarted && nowMs - wakeMs >= noSpeechMs -> "no_speech"
            else -> null
        }
    }
}
