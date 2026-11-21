package dev.localvision.local_vision

/**
 * Keeps only LAN-private IPv4 ICE candidates in an SDP: RFC1918 and
 * link-local. Drops loopback, every IPv6 address, CGNAT (100.64/10, where
 * cellular lives) and public addresses, for UDP and TCP candidates alike. The
 * `c=` line of each media section is rewritten to the first kept candidate
 * (or 0.0.0.0 when none is left).
 */
object SdpFilter {
    private val ipv4 = Regex("""^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$""")

    fun isAllowedIpv4(ip: String): Boolean {
        val m = ipv4.matchEntire(ip) ?: return false
        val o = m.groupValues.drop(1).map { it.toInt() }
        if (o.any { it > 255 }) return false
        return o[0] == 10 ||
            (o[0] == 172 && o[1] in 16..31) ||
            (o[0] == 192 && o[1] == 168) ||
            (o[0] == 169 && o[1] == 254)
    }

    private fun candidateIp(line: String): String? =
        line.removePrefix("a=").split(' ').getOrNull(4)

    fun filter(sdp: String): String {
        val eol = if (sdp.contains("\r\n")) "\r\n" else "\n"
        val lines = sdp.split(eol)
        // split into the session header and media sections
        val sections = mutableListOf<MutableList<String>>(mutableListOf())
        for (l in lines) {
            if (l.startsWith("m=")) sections.add(mutableListOf())
            sections.last().add(l)
        }
        for ((i, sec) in sections.withIndex()) {
            if (i == 0) continue
            sec.removeAll { it.startsWith("a=candidate:") && !isAllowedIpv4(candidateIp(it) ?: "") }
            val first = sec.firstOrNull { it.startsWith("a=candidate:") }?.let { candidateIp(it) } ?: "0.0.0.0"
            for (j in sec.indices) if (sec[j].startsWith("c=IN ")) sec[j] = "c=IN IP4 $first"
        }
        return sections.flatten().joinToString(eol)
    }
}
