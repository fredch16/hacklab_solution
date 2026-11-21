package dev.localvision.local_vision

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceLogicTest {
    // ---- utterance timing (audio clock, 32 ms steps) ----

    private fun run(t: UtteranceTracker, speechRanges: List<LongRange>, until: Long = 30_000): Pair<String, Long>? {
        var now = 0L
        t.start(now)
        while (now < until) {
            now += 32
            val s = speechRanges.any { now in it }
            t.update(now, s)?.let { return it to now }
        }
        return null
    }

    @Test fun endsOnOneSecondOfSilenceAfterSpeech() {
        val (reason, at) = run(UtteranceTracker(), listOf(500L..2000L))!!
        assertEquals("silence", reason)
        assertTrue("ended at $at", at in 3000..3100)
    }

    @Test fun capsAtFifteenSeconds() {
        val (reason, at) = run(UtteranceTracker(), listOf(300L..40_000L))!!
        assertEquals("max_duration", reason)
        assertTrue(at in 15_000..15_040)
    }

    @Test fun noSpeechWithinFiveSeconds() {
        val (reason, at) = run(UtteranceTracker(), emptyList())!!
        assertEquals("no_speech", reason)
        assertTrue(at in 5000..5040)
    }

    @Test fun speechInsideWakePhraseTailIsIgnored() {
        // only the keyword's own tail (first 300 ms) counts as speech -> still no_speech
        val (reason, _) = run(UtteranceTracker(), listOf(0L..250L))!!
        assertEquals("no_speech", reason)
    }

    // ---- phrase validation ----

    @Test fun phraseValidation() {
        assertEquals("hey robot", WakePhrase.validate("  hey   robot "))
        assertNull(WakePhrase.validate("don't stop"))
        assertEquals("hey ELi", WakePhrase.validate(" Hey   ELi "))
        assertEquals("hey Elrobot", WakePhrase.validate("hey Elrobot"))
        assertEquals("hey Elrobot", WakePhrase.fromSaved("Elrobot"))
        assertEquals("hey Robot", WakePhrase.fromSaved("HEY Robot"))
        assertNull(WakePhrase.fromSaved(null))
        assertNull(WakePhrase.validate("hey"))
        assertNull(WakePhrase.validate("hey ''"))
        assertNull(WakePhrase.validate("a"))
        assertNull(WakePhrase.validate(""))
        assertNull(WakePhrase.validate(null))
        assertNull(WakePhrase.validate("hey robot 2"))
        assertNull(WakePhrase.validate("héy robot"))
        assertNull(WakePhrase.validate("hey " + "x".repeat(41)))
        assertNotNull(WakePhrase.validate("hey " + "x".repeat(40)))
    }

    @Test fun derivesPronunciationsForArbitraryNamesAndKeepsTheHeyPrefix() {
        val file = java.io.File("src/main/assets/voice/kws-phone/cmu-lts.bin")
        val lexicon = mapOf("HEY" to "HH EY1", "ROBOT" to "R OW1 B AA2 T", "ELI" to "IY1 L AY0", "EL" to "EH1 L")
        val pronunciation = WakePronunciation(lexicon, file.readBytes())
        assertEquals("HH EY1 R OW1 B AA2 T", pronunciation.phrases("hey Robot").first())
        assertTrue("HH EY1 EH1 L IY0" in pronunciation.phrases("hey Eli"))
        assertTrue("HH EY1 EH1 L R AH0 B AA1 T" in pronunciation.phrases("hey Elrobot"))
        assertEquals(listOf("HH EY1 Z AO1 R B OW0"), pronunciation.phrases("hey Zorbo"))
        assertEquals(listOf("HH EY1 N AH0 V IH1 R OW0"), pronunciation.phrases("hey Naviro"))
    }

    // ---- resampler ----

    private fun tone(freq: Double, sec: Double, ch: Int): ByteArray {
        val n = (48000 * sec).toInt()
        val b = ByteArray(n * ch * 2)
        for (i in 0 until n) {
            val v = (sin(2 * PI * freq * i / 48000) * 16000).toInt().toShort()
            for (c in 0 until ch) { b[(i * ch + c) * 2] = (v.toInt() and 0xff).toByte(); b[(i * ch + c) * 2 + 1] = (v.toInt() shr 8).toByte() }
        }
        return b
    }

    private fun rms(f: FloatArray, skip: Int) = Math.sqrt(f.drop(skip).map { (it * it).toDouble() }.average())

    @Test fun resamplerOutputLengthAndPassband() {
        val out = Resampler(2).process(tone(1000.0, 1.0, 2), 48000 * 4)
        assertEquals(16000, out.size)
        // 16000/ sqrt2 -> rms 0.346 for amplitude 16000/32768
        assertEquals(16000 / 32768.0 / Math.sqrt(2.0), rms(out, 100), 0.02)
    }

    @Test fun resamplerRejectsAboveNyquist() {
        val pass = rms(Resampler(1).process(tone(1000.0, 0.5, 1), 48000), 100)
        val alias = rms(Resampler(1).process(tone(12000.0, 0.5, 1), 48000), 100)
        assertTrue("12 kHz must be attenuated (pass=$pass alias=$alias)", alias < pass * 0.05)
    }

    @Test fun resamplerKeepsStateAcrossChunks() {
        val whole = Resampler(1).process(tone(500.0, 0.2, 1), 9600 * 2)
        val r = Resampler(1)
        val t = tone(500.0, 0.2, 1)
        val a = r.process(t.copyOfRange(0, 4000), 4000)
        val b = r.process(t.copyOfRange(4000, t.size), t.size - 4000)
        val joined = a + b
        assertTrue(abs(joined.size - whole.size) <= 1)
        for (i in 0 until minOf(joined.size, whole.size)) assertEquals(whole[i].toDouble(), joined[i].toDouble(), 1e-4)
    }

    @Test fun wakeComesFromTheSpotterAndCommandsRearm() {
        val flow = VoiceFlow("hey robot")
        assertTrue(flow.onText("hey robot").isEmpty())
        repeat(3) { attempt ->
            val now = attempt * 3000L
            assertEquals(listOf(VoiceFlow.Action.Wake("hey robot")), flow.onWake(now))
            assertTrue(flow.onWake(now + 100).isEmpty())
            assertEquals(listOf(VoiceFlow.Action.Partial("start recording")), flow.onText("start recording"))
            assertTrue(flow.onVad(now + 400, true).isEmpty())
            assertEquals(listOf(VoiceFlow.Action.End("silence")), flow.onVad(now + 1400, false))
            assertTrue(flow.onWake(now + 1500).isEmpty())
            assertEquals(listOf(VoiceFlow.Action.Rearm), flow.onClock(now + 2400))
        }
    }

    @Test fun wakeReplayKeepsTheLatestAudioAcrossChunks() {
        val replay = WakeAudio(4)
        replay.append(floatArrayOf(1f, 2f, 3f))
        assertEquals(listOf(1f, 2f, 3f), replay.samples().toList())
        replay.append(floatArrayOf(4f, 5f, 6f))
        assertEquals(listOf(3f, 4f, 5f, 6f), replay.samples().toList())
        replay.clear()
        assertTrue(replay.samples().isEmpty())
    }
}
