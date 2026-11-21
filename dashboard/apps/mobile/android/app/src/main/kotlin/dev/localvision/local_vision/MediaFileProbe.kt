package dev.localvision.local_vision

import android.content.Context
import android.media.MediaExtractor
import android.net.Uri
import android.util.Log
import java.io.File

/** Reads back a muxed file with MediaExtractor: the truth about what was recorded. */
object MediaFileProbe {
    data class Track(val mime: String, val samples: Int, val firstUs: Long, val lastUs: Long, val width: Int, val height: Int, val channels: Int, val sampleRate: Int)
    data class Result(val tracks: List<Track>, val durationUs: Long, val sizeBytes: Long) {
        val video get() = tracks.firstOrNull { it.mime.startsWith("video/") }
        val audio get() = tracks.firstOrNull { it.mime.startsWith("audio/") }
        val videoFps: Double? get() = video?.let { if (it.samples > 1 && it.lastUs > it.firstUs) (it.samples - 1) * 1e6 / (it.lastUs - it.firstUs) else null }
        val videoBitrate: Long? get() = video?.let { if (durationUs > 0) sizeBytes * 8 * 1_000_000 / durationUs else null }
    }

    fun probe(ctx: Context, uri: Uri, sizeBytes: Long): Result? = try {
        val ex = MediaExtractor()
        ex.setDataSource(ctx, uri, null)
        read(ex, sizeBytes).also { ex.release() }
    } catch (e: Throwable) { Log.w("LVC", "probe failed: ${e.javaClass.simpleName}"); null }

    fun probe(file: File): Result? = try {
        val ex = MediaExtractor()
        ex.setDataSource(file.absolutePath)
        read(ex, file.length()).also { ex.release() }
    } catch (e: Throwable) { Log.w("LVC", "probe failed: ${e.javaClass.simpleName}"); null }

    private fun read(ex: MediaExtractor, size: Long): Result {
        val tracks = mutableListOf<Track>()
        var dur = 0L
        for (i in 0 until ex.trackCount) {
            val f = ex.getTrackFormat(i)
            val mime = f.getString(android.media.MediaFormat.KEY_MIME) ?: continue
            ex.selectTrack(i)
            ex.seekTo(0, MediaExtractor.SEEK_TO_CLOSEST_SYNC)
            var n = 0; var first = -1L; var last = 0L
            while (true) {
                val t = ex.sampleTime
                if (t < 0) break
                if (first < 0) first = t
                last = t; n++
                ex.advance()
            }
            ex.unselectTrack(i)
            if (f.containsKey(android.media.MediaFormat.KEY_DURATION)) dur = maxOf(dur, f.getLong(android.media.MediaFormat.KEY_DURATION))
            tracks.add(Track(
                mime, n, maxOf(first, 0), last,
                if (f.containsKey("width")) f.getInteger("width") else 0,
                if (f.containsKey("height")) f.getInteger("height") else 0,
                if (f.containsKey("channel-count")) f.getInteger("channel-count") else 0,
                if (f.containsKey("sample-rate")) f.getInteger("sample-rate") else 0,
            ))
        }
        return Result(tracks, dur, size)
    }

    fun log(tag: String, r: Result?) {
        if (r == null) { Log.w(tag, "probe: unreadable"); return }
        Log.i(tag, "probe size=${r.sizeBytes} durationUs=${r.durationUs} videoFps=${r.videoFps} bitrate=${r.videoBitrate}")
        for (t in r.tracks) Log.i(tag, "probe track ${t.mime} samples=${t.samples} firstUs=${t.firstUs} lastUs=${t.lastUs} ${t.width}x${t.height} ch=${t.channels} sr=${t.sampleRate}")
    }
}
