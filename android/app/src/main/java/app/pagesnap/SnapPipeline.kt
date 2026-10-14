package app.pagesnap

import android.content.Context
import android.graphics.Bitmap
import app.pagesnap.data.NoteStore
import app.pagesnap.engine.DetectConfig
import app.pagesnap.engine.FrameSampler
import app.pagesnap.engine.NotesPdf
import app.pagesnap.engine.PageCapturer
import app.pagesnap.engine.PageDetector
import app.pagesnap.engine.PageReason
import app.pagesnap.engine.Sensitivity
import app.pagesnap.engine.Signatures
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.util.UUID

/**
 * The whole job, in the order the student sees it:
 *
 *   1. sample the video          3 frames a second, 160x90 grayscale
 *   2. find the page changes     the tested detector, ported from the Python one
 *   3. take the screenshots      full resolution, once per page
 *   4. keep them on the phone    filesDir/notes/<id>/ - nothing uploaded anywhere
 *   5. write the PDF             only when the student taps Save
 *
 * Steps 1-4 are the slow part (a 10 minute lecture takes roughly 30-60 seconds on
 * a mid-range phone); step 5 is instant.
 */
class SnapPipeline(context: Context) {

    /** Handy for the UI (file picking, the link downloader, sharing the PDF). */
    val appContext: Context = context.applicationContext

    private val store = NoteStore(appContext)
    private val sampler = FrameSampler()
    private val capturer = PageCapturer()

    data class Draft(
        val id: String,
        val sourceName: String,
        val sourcePath: String,
        val durationSeconds: Double,
        val pages: MutableList<Page>,
        var title: String,
        val notes: List<String>,
        val thresholds: Map<String, Double>,
    ) {
        data class Page(
            val index: Int,
            val file: String,
            val time: Double,
            val reason: PageReason,
            var keep: Boolean = true,
        )
    }

    /** Steps 1-4: analyse the video and store one screenshot per page. */
    suspend fun analyse(
        sourcePath: String,
        sourceName: String,
        sensitivity: Sensitivity = Sensitivity.BALANCED,
        maxWidthPx: Int = 2200,
        onProgress: (String, Float) -> Unit = { _, _ -> },
    ): Draft = withContext(Dispatchers.Default) {
        val id = UUID.randomUUID().toString().take(12)
        val cfg = DetectConfig.preset(sensitivity)

        onProgress("Watching the video…", 0.02f)
        val durationSeconds = capturer.durationSeconds(sourcePath)
        val signatures = ArrayList<app.pagesnap.engine.FrameSig>()
        val result = sampler.sample(sourcePath) { index, t, gray ->
            signatures += Signatures.of(index, t, gray, cfg.analysisWidth, cfg.analysisHeight)
            if (index % 30 == 0 && durationSeconds > 0) {
                onProgress("Watching the video…",
                    (0.02f + 0.53f * (t / durationSeconds)).coerceAtMost(0.55f))
            }
        }

        onProgress("Finding the page changes…", 0.6f)
        val report = PageDetector(cfg).detect(signatures)

        val pages = ArrayList<Draft.Page>()
        report.pages.forEachIndexed { i, candidate ->
            onProgress("Saving page ${i + 1} of ${report.pages.size}…",
                0.6f + 0.38f * (i + 1) / report.pages.size.coerceAtLeast(1))
            val bitmap = capturer.grab(sourcePath, candidate.time, maxWidthPx) ?: return@forEachIndexed
            val file = store.savePageBitmap(id, i + 1, bitmap)
            bitmap.recycle()
            pages += Draft.Page(i, file, candidate.time, candidate.reason)
        }

        val draft = Draft(
            id = id,
            sourceName = sourceName,
            sourcePath = sourcePath,
            durationSeconds = if (result.durationSeconds > 0) result.durationSeconds else durationSeconds,
            pages = pages,
            title = defaultTitle(sourceName),
            notes = report.notes + listOfNotNull(result.note),
            thresholds = report.thresholds,
        )
        // Persist immediately: if the app is closed now, nothing is lost.
        persist(draft)
        onProgress("Done", 1f)
        draft
    }

    /** Remember the student's ticks and the name they typed. */
    fun persist(draft: Draft) {
        store.saveNote(
            NoteStore.Note(
                id = draft.id,
                title = draft.title,
                createdAt = System.currentTimeMillis(),
                sourceName = draft.sourceName,
                durationSeconds = draft.durationSeconds,
                pages = draft.pages.map {
                    NoteStore.SavedPage(it.file, it.time, it.reason, it.keep)
                },
            )
        )
    }

    /** Step 5: build the PDF from the ticked pages, in memory. */
    suspend fun buildPdf(draft: Draft, options: NotesPdf.Options): ByteArray =
        withContext(Dispatchers.IO) {
            val kept = draft.pages.filter { it.keep }
            require(kept.isNotEmpty()) { "Tick at least one page before saving." }
            val bitmaps: List<Bitmap> = kept.mapNotNull { page ->
                val file = store.bitmapFile(draft.id, page.file)
                if (file.exists()) android.graphics.BitmapFactory.decodeFile(file.absolutePath) else null
            }
            require(bitmaps.isNotEmpty()) { "Those screenshots are gone from storage." }
            val out = ByteArrayOutputStream()
            NotesPdf.write(out, bitmaps, options)
            out.toByteArray()
        }

    /** Builds the PDF and puts it where the student can find it. */
    suspend fun savePdf(
        draft: Draft, options: NotesPdf.Options,
    ): Pair<String, android.net.Uri?> = withContext(Dispatchers.IO) {
        val bytes = buildPdf(draft, options)
        val name = fileNameFor(draft.title)
        name to store.exportPdf(name, bytes)
    }

    fun notes(): List<NoteStore.Note> = store.list()
    fun loadNote(id: String) = store.load(id)
    fun rename(id: String, title: String) = store.rename(id, title)
    fun delete(id: String) = store.delete(id)
    fun bitmapFor(noteId: String, fileName: String) = store.bitmapFile(noteId, fileName)

    private fun defaultTitle(sourceName: String): String =
        sourceName.substringBeforeLast('.').take(60).ifBlank { store.defaultTitle() }

    private fun fileNameFor(title: String): String {
        val safe = title.trim().ifBlank { "notes" }
            .replace(Regex("[^A-Za-z0-9 _\\-().\\[\\]]"), "")
            .replace(Regex("\\s+"), "-")
            .trim('-', '.', '_')
            .take(80)
            .ifBlank { "notes" }
        return if (safe.endsWith(".pdf", ignoreCase = true)) safe else "$safe.pdf"
    }
}
