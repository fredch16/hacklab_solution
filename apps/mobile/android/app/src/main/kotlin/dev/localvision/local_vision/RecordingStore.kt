package dev.localvision.local_vision

import android.content.Context
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject

/** Finalized recordings (metadata only; the video itself lives in MediaStore). */
class RecordingStore(private val ctx: Context) {
    private val prefs = ctx.getSharedPreferences("lvc_recordings", Context.MODE_PRIVATE)

    fun add(e: JSONObject) {
        val arr = load()
        val out = JSONArray().put(e)
        for (i in 0 until minOf(arr.length(), 49)) out.put(arr.get(i))
        prefs.edit().putString("list", out.toString()).apply()
    }

    private fun load(): JSONArray = try { JSONArray(prefs.getString("list", "[]")) } catch (_: Throwable) { JSONArray() }

    /** Newest first; items whose MediaStore row is gone are dropped. Paths/uris are not exposed. */
    fun list(): List<Map<String, Any?>> {
        val arr = load()
        val keep = JSONArray()
        val out = mutableListOf<Map<String, Any?>>()
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            val exists = try {
                ctx.contentResolver.query(Uri.parse(o.getString("uri")), arrayOf("_id"), null, null, null)?.use { it.count > 0 } ?: false
            } catch (_: Throwable) { false }
            if (!exists) continue
            keep.put(o)
            out.add(mapOf(
                "id" to o.getString("id"), "filename" to o.getString("filename"),
                "created_utc" to o.getString("created_utc"), "duration_ms" to o.getLong("duration_ms"),
                "size_bytes" to o.getLong("size_bytes"), "width" to o.getInt("width"), "height" to o.getInt("height"),
                "fps_measured" to (if (o.isNull("fps_measured")) null else o.getDouble("fps_measured")),
                "codec" to o.getString("codec"), "finalized" to true,
            ))
        }
        if (keep.length() != arr.length()) prefs.edit().putString("list", keep.toString()).apply()
        return out
    }
}
