package dev.localvision.local_vision

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.Locale

/** Flite 2.2 CMU letter-to-sound rules, repacked as a little-endian Android asset. */
internal class WakePronunciation(
    private val lexicon: Map<String, String>,
    packedRules: ByteArray,
) {
    private val rules = ByteBuffer.wrap(packedRules).order(ByteOrder.LITTLE_ENDIAN)
    private val endOfPhones = packedRules.indexOf(0, start = 52)
    private val phones = String(packedRules, 52, endOfPhones - 52, Charsets.US_ASCII).split(' ')

    private fun ByteArray.indexOf(value: Byte, start: Int): Int =
        (start until size).first { this[it] == value }

    /** Canonical pronunciation plus bounded corrections for spelling and syllable stress. */
    fun candidates(word: String): List<String> = buildList {
        val known = lexicon[word]
        if (known != null) add(known)
        val predicted = predict(word)
        if (predicted.isNotEmpty()) {
            add(predicted.joinToString(" "))
            val stressed = predicted.indices.filter { predicted[it].endsWith('1') }
            if (stressed.size > 1) {
                val index = stressed.last()
                val unstressed = predicted.toMutableList()
                unstressed[index] = predicted[index].dropLast(1) + "0"
                add(unstressed.joinToString(" "))
            }
        }
        if (known == null) {
            // Unknown compound names sometimes keep both words' dictionary pronunciations.
            for (split in 2..word.length - 3) {
                val prefix = lexicon[word.substring(0, split)] ?: continue
                val suffix = lexicon[word.substring(split)] ?: continue
                add("$prefix $suffix")
                val parts = suffix.split(' ').toMutableList()
                val stresses = parts.indices.filter { parts[it].last() in "12" }
                if (stresses.size > 1) {
                    parts[stresses[0]] = "AH0"
                    val index = stresses[1]
                    parts[index] = parts[index].dropLast(1) + "1"
                    add("$prefix ${parts.joinToString(" ")}")
                }
                break
            }
        }
    }.distinct()

    /** The default pronunciation plus replacements for one name word at a time. */
    fun phrases(phrase: String): List<String> {
        val words = phrase.uppercase(Locale.ROOT).split(' ')
        val prefix = lexicon["HEY"] ?: return emptyList()
        val options = words.drop(1).map(::candidates)
        if (options.any { it.isEmpty() }) return emptyList()
        val canonical = listOf(prefix) + options.map { it.first() }
        return buildList {
            add(canonical.joinToString(" "))
            for ((i, choices) in options.withIndex()) {
                for (choice in choices.drop(1)) {
                    val alternate = canonical.toMutableList()
                    alternate[i + 1] = choice
                    add(alternate.joinToString(" "))
                    if (size == 8) return@buildList
                }
            }
        }
    }

    private fun predict(word: String): List<String> = buildList {
        val context = "000#${word.lowercase(Locale.ROOT)}#000"
        for (i in 4 until context.length - 4) {
            val letter = context[i]
            if (letter !in 'a'..'z') continue
            var node = rules.getShort(2 * (letter - 'a')).toInt() and 0xffff
            while (true) {
                val offset = endOfPhones + 1 + node * 6
                val feature = rules.get(offset).toInt() and 0xff
                val value = rules.get(offset + 1).toInt() and 0xff
                if (feature == 255) {
                    if (value != 0) addAll(phones[value].split('-'))
                    break
                }
                val actual = when (feature) {
                    in 0..3 -> context[i + feature - 4]
                    in 4..7 -> context[i + feature - 3]
                    else -> '0' // Flite's neutral part-of-speech feature
                }
                val branch = if (actual.code == value) 2 else 4
                node = rules.getShort(offset + branch).toInt() and 0xffff
            }
        }
    }
}
