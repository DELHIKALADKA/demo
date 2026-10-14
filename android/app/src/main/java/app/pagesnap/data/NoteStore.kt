package app.pagesnap.data

import android.content.ContentValues
import android.content.Context
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import app.pagesnap.engine.PageReason
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Every note lives in the phone's own storage - no server, no database, no
 * account, no operator. `filesDir/notes/<id>/` holds the screenshots and a small
 * meta.json; the PDF is written only when the student taps Save, straight into
 * Downloads (or straight into the app's own folder on older Android versions).
 *
 * Deleting a note deletes the folder - there is nothing anywhere else to delete.
 */
class NoteStore(private val context: Context) {

    data class SavedPage(
        val file: String,
        val sourceTime: Double,
        val reason: PageReason,
        val keep: Boolean = true,
    )

    data class Note(
        val id: String,
        val title: String,
        val createdAt: Long,
        val sourceName: String,
        val durationSeconds: Double,
        val pages: List<SavedPage>,
    ) {
        val keptPages: List<SavedPage> get() = pages.filter { it.keep }
    }

    private val root: File get() = File(context.filesDir, "notes").apply { mkdirs() }

    private fun folder(id: String) = File(root, id).apply { mkdirs() }
    private fun metaFile(id: String) = File(folder(id), "meta.json")
    fun bitmapFile(id: String, name: String) = File(folder(id), name)

    // -- writing -------------------------------------------------------------

    /** Stores one screenshot as a lossless PNG (screenshots of text compress well). */
    fun savePageBitmap(id: String, index: Int, bitmap: Bitmap): String {
        val name = String.format(Locale.US, "page_%03d.png", index)
        FileOutputStream(bitmapFile(id, name)).use { out ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
        }
        return name
    }

    fun saveNote(note: Note) {
        val pages = JSONArray()
        note.pages.forEach { p ->
            pages.put(JSONObject().apply {
                put("file", p.file)
                put("time", p.sourceTime)
                put("reason", p.reason.name)
                put("keep", p.keep)
            })
        }
        val json = JSONObject().apply {
            put("id", note.id)
            put("title", note.title)
            put("createdAt", note.createdAt)
            put("sourceName", note.sourceName)
            put("durationSeconds", note.durationSeconds)
            put("pages", pages)
        }
        metaFile(note.id).writeText(json.toString(2))
    }

    fun delete(id: String): Boolean = folder(id).deleteRecursively()

    fun rename(id: String, title: String): Note? =
        load(id)?.let { note -> note.copy(title = title).also { saveNote(it) } }

    fun setKept(id: String, keep: List<Boolean>): Note? =
        load(id)?.let { note ->
            val updated = note.pages.mapIndexed { i, p -> p.copy(keep = keep.getOrElse(i) { p.keep }) }
            note.copy(pages = updated).also { saveNote(it) }
        }

    // -- reading -------------------------------------------------------------

    fun load(id: String): Note? {
        val file = metaFile(id)
        if (!file.exists()) return null
        return try {
            val json = JSONObject(file.readText())
            val pages = json.getJSONArray("pages")
            Note(
                id = json.getString("id"),
                title = json.optString("title", "notes"),
                createdAt = json.optLong("createdAt"),
                sourceName = json.optString("sourceName"),
                durationSeconds = json.optDouble("durationSeconds", 0.0),
                pages = (0 until pages.length()).map { i ->
                    val p = pages.getJSONObject(i)
                    SavedPage(
                        file = p.getString("file"),
                        sourceTime = p.optDouble("time", 0.0),
                        reason = runCatching { PageReason.valueOf(p.optString("reason")) }
                            .getOrDefault(PageReason.CUT),
                        keep = p.optBoolean("keep", true),
                    )
                },
            )
        } catch (t: Throwable) {
            null
        }
    }

    fun list(): List<Note> =
        root.listFiles().orEmpty().filter { it.isDirectory }
            .mapNotNull { load(it.name) }
            .sortedByDescending { it.createdAt }

    // -- exporting -----------------------------------------------------------

    /**
     * Copies the finished PDF where the student can find it: Downloads/PageSnap
     * on modern Android (no permission needed), or the app folder on older ones.
     * Returns a content Uri so the app can offer "open" and "share" immediately.
     */
    fun exportPdf(fileName: String, bytes: ByteArray): Uri? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, fileName)
                put(MediaStore.Downloads.MIME_TYPE, "application/pdf")
                put(MediaStore.Downloads.RELATIVE_PATH,
                    Environment.DIRECTORY_DOWNLOADS + "/PageSnap")
            }
            val resolver = context.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            uri?.let { resolver.openOutputStream(it)?.use { out -> out.write(bytes) } }
            uri
        } else {
            val dir = File(context.getExternalFilesDir(null), "PageSnap").apply { mkdirs() }
            val file = File(dir, fileName)
            file.writeBytes(bytes)
            Uri.fromFile(file)
        }
    }

    fun defaultTitle(): String =
        "Notes " + SimpleDateFormat("d MMM yyyy, HH:mm", Locale.getDefault()).format(Date())
}
