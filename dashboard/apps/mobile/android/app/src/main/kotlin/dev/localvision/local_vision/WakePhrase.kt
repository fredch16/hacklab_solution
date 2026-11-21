package dev.localvision.local_vision

import java.util.Locale

/** Wake phrase rules and on-device BPE tokenization for the open-vocabulary keyword spotter. */
object WakePhrase {
    private val allowed = Regex("^[A-Za-z' ]{2,40}$")
    private val space = Regex("\\s+")
    private const val PREFIX = "hey "

    /** Validate the full phrase sent by the paired dashboard. */
    fun validate(raw: String?): String? {
        val phrase = raw?.trim()?.replace(space, " ") ?: return null
        if (!phrase.startsWith(PREFIX, ignoreCase = true)) return null
        val name = phrase.substring(PREFIX.length)
        if (!allowed.matches(name) || name.none { it.isLetter() }) return null
        return PREFIX + name
    }

    /** Saved phrases from before the fixed prefix are reused as the editable name. */
    fun fromSaved(raw: String?): String? {
        val saved = raw?.trim() ?: return null
        if (saved.equals("hey", ignoreCase = true)) return null
        return if (saved.startsWith(PREFIX, ignoreCase = true)) validate(saved) else validate(PREFIX + saved)
    }

    fun loadPronunciations(lines: Sequence<String>): Map<String, String> =
        lines.mapNotNull { line ->
            val key = line.substringBefore(' ')
            val phones = line.substringAfter(' ', "")
            if (key.isEmpty() || phones.isEmpty() || '(' in key) null else key to phones
        }.toMap()

    /** piece -> unigram score, from the model's bpe.vocab ("piece<TAB>score"). */
    fun loadVocab(lines: Sequence<String>): Map<String, Float> {
        val m = HashMap<String, Float>()
        for (l in lines) {
            val t = l.split('\t')
            if (t.size == 2) t[1].toFloatOrNull()?.let { m[t[0]] = it }
        }
        return m
    }

    /**
     * Sentencepiece-unigram encoding by Viterbi over [vocab] (restricted to [allowedPieces]):
     * words are upper-cased and prefixed with the word-boundary mark. Null if a word cannot be covered.
     */
    fun tokenize(phrase: String, vocab: Map<String, Float>, allowedPieces: Set<String>): List<String>? {
        val out = mutableListOf<String>()
        for (word in phrase.trim().uppercase(Locale.ROOT).split(' ').filter { it.isNotEmpty() }) {
            val s = "▁$word"
            val n = s.length
            val best = FloatArray(n + 1) { Float.NEGATIVE_INFINITY }
            val back = IntArray(n + 1) { -1 }
            best[0] = 0f
            for (i in 0 until n) {
                if (best[i] == Float.NEGATIVE_INFINITY) continue
                for (j in i + 1..minOf(n, i + 16)) {
                    val piece = s.substring(i, j)
                    val sc = vocab[piece] ?: continue
                    if (piece !in allowedPieces) continue
                    if (best[i] + sc > best[j]) { best[j] = best[i] + sc; back[j] = i }
                }
            }
            if (back[n] < 0) return null
            val pieces = ArrayDeque<String>()
            var j = n
            while (j > 0) { val i = back[j]; pieces.addFirst(s.substring(i, j)); j = i }
            out.addAll(pieces)
        }
        return out
    }

    /** tokens.txt lines are "<piece> <id>". */
    fun tokensOf(lines: List<String>): Set<String> =
        lines.mapNotNull { it.substringBeforeLast(' ', "").ifEmpty { null } }.toSet()
}
