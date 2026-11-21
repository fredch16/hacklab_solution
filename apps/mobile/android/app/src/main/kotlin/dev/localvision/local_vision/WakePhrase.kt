package dev.localvision.local_vision

/** Wake phrase rules and on-device BPE tokenization for the open-vocabulary keyword spotter. */
object WakePhrase {
    private val allowed = Regex("^[A-Za-z' ]+$")

    /** Normalised phrase (single spaces) or null when invalid: 2-40 chars, letters/spaces/apostrophes. */
    fun validate(raw: String?): String? {
        val p = raw?.trim()?.replace(Regex("\\s+"), " ") ?: return null
        if (p.length !in 2..40 || !allowed.matches(p)) return null
        return p
    }

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
        for (word in phrase.trim().uppercase().split(' ').filter { it.isNotEmpty() }) {
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
