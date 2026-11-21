package dev.localvision.local_vision

/** Command and cooldown timing on the processed-audio clock. */
class VoiceFlow(private val phrase: String) {
    enum class Mode { LISTENING, COMMAND, COOLDOWN }

    sealed class Action {
        data class Wake(val phrase: String) : Action()
        data class Partial(val text: String) : Action()
        data class End(val reason: String) : Action()
        object Rearm : Action()
    }

    var mode = Mode.LISTENING
        private set
    private val tracker = UtteranceTracker()
    private var cooldownUntil = 0L

    fun onWake(audioMs: Long): List<Action> {
        if (mode != Mode.LISTENING) return emptyList()
        mode = Mode.COMMAND
        tracker.start(audioMs)
        return listOf(Action.Wake(phrase))
    }

    fun onText(text: String): List<Action> =
        if (mode == Mode.COMMAND) listOf(Action.Partial(text)) else emptyList()

    fun onVad(audioMs: Long, speech: Boolean): List<Action> {
        if (mode != Mode.COMMAND) return emptyList()
        val reason = tracker.update(audioMs, speech) ?: return emptyList()
        mode = Mode.COOLDOWN
        cooldownUntil = audioMs + 1000
        return listOf(Action.End(reason))
    }

    fun onClock(audioMs: Long): List<Action> {
        if (mode != Mode.COOLDOWN || audioMs < cooldownUntil) return emptyList()
        mode = Mode.LISTENING
        return listOf(Action.Rearm)
    }
}
