package dev.localvision.local_vision

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SdpFilterTest {
    private fun cand(ip: String, proto: String = "udp") =
        "a=candidate:1 1 $proto 2122260223 $ip 50000 typ host generation 0 network-id 1"

    private val sdp = listOf(
        "v=0",
        "m=video 9 UDP/TLS/RTP/SAVPF 102",
        "c=IN IP6 2a02:1::1",
        cand("127.0.0.1"),
        cand("::1"),
        cand("2a02:aaaa::1"),
        cand("2a00:bbbb::2", "tcp"),
        cand("100.94.73.169"),
        cand("8.8.8.8"),
        cand("10.181.249.119"),
        cand("10.19.20.246", "tcp"),
        cand("172.20.1.2"),
        cand("192.168.1.9"),
        cand("169.254.3.4"),
        "a=end-of-candidates",
        "a=ice-ufrag:abc",
    ).joinToString("\r\n")

    @Test
    fun keepsOnlyPrivateIpv4HostCandidates() {
        val out = SdpFilter.filter(sdp)
        val kept = out.split("\r\n").filter { it.startsWith("a=candidate:") }.map { it.split(' ')[4] }
        assertEquals(listOf("10.181.249.119", "10.19.20.246", "172.20.1.2", "192.168.1.9", "169.254.3.4"), kept)
        assertTrue(out.contains("a=end-of-candidates"))
        assertTrue(out.contains("a=ice-ufrag:abc"))
    }

    @Test
    fun keepsTcpForAllowedIpv4Only() {
        val out = SdpFilter.filter(sdp)
        assertTrue(out.contains("10.19.20.246 50000 typ host") && out.contains(" tcp "))
        assertFalse(out.contains("2a00:bbbb"))
    }

    @Test
    fun rewritesConnectionLineToFirstKeptCandidate() {
        assertTrue(SdpFilter.filter(sdp).contains("c=IN IP4 10.181.249.119"))
    }

    @Test
    fun connectionLineFallsBackWhenNothingKept() {
        val none = "v=0\r\nm=video 9 X 1\r\nc=IN IP4 1.2.3.4\r\n${cand("2a02::1")}\r\n"
        val out = SdpFilter.filter(none)
        assertTrue(out.contains("c=IN IP4 0.0.0.0"))
        assertFalse(out.contains("a=candidate"))
    }

    @Test
    fun addressPolicy() {
        for (ip in listOf("10.0.0.1", "172.16.0.1", "172.31.255.1", "192.168.0.1", "169.254.1.1")) assertTrue(ip, SdpFilter.isAllowedIpv4(ip))
        for (ip in listOf("127.0.0.1", "172.32.0.1", "172.15.0.1", "100.64.0.1", "8.8.8.8", "::1", "10.0.0", "300.1.1.1")) assertFalse(ip, SdpFilter.isAllowedIpv4(ip))
    }
}
