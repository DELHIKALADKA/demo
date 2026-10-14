package app.pagesnap.engine

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.pdf.PdfDocument
import java.io.OutputStream

/**
 * Writes the captured pages into a PDF, on the phone, with no library and no
 * network. One screenshot per PDF page - we never re-render the text, so the PDF
 * looks exactly like the video did and opens instantly.
 *
 * Page geometry: A4 in points (595x842), rotated for landscape frames so the
 * notes fill the paper, with a small margin. `PAGE_EXACT` instead makes each PDF
 * page the exact shape of the video frame, so there are no white bands at all -
 * nicer for screenshots, worse for printing.
 *
 * Honest note: android.graphics.pdf.PdfDocument cannot set PDF metadata (title,
 * author), so the name the student types is used for the file itself. If we ever
 * need the title inside the document too, we would have to write the PDF trailer
 * ourselves - not worth the complexity here.
 */
object NotesPdf {

    const val A4_SHORT = 595f
    const val A4_LONG = 842f

    enum class PageSize { A4, LETTER, EXACT }

    data class Options(
        val pageSize: PageSize = PageSize.A4,
        val marginPt: Float = 24f,          // ~8 mm
        val pageNumbers: Boolean = false,
        val maxWidthPx: Int = 2200,         // downscale very large captures
        val grayscale: Boolean = false,
    )

    /**
     * @param pages the pictures to write, in order.
     * @return the number of PDF pages written.
     */
    fun write(out: OutputStream, pages: List<Bitmap>, options: Options = Options()): Int {
        val doc = PdfDocument()
        val paint = Paint(Paint.FILTER_BITMAP_FLAG).apply { isAntiAlias = true }
        val label = Paint().apply {
            color = Color.argb(150, 60, 60, 60); textSize = 9f; isAntiAlias = true
        }

        pages.forEachIndexed { i, source ->
            val bmp = prepare(source, options)
            val landscape = bmp.width > bmp.height
            val pageRect = when (options.pageSize) {
                PageSize.EXACT -> Rect(0, 0, bmp.width, bmp.height)
                PageSize.LETTER -> if (landscape) Rect(0, 0, (A4_LONG * 1.029f).toInt(), (A4_SHORT * 1.029f).toInt())
                                   else Rect(0, 0, (A4_SHORT * 1.029f).toInt(), (A4_LONG * 1.029f).toInt())
                PageSize.A4 -> if (landscape) Rect(0, 0, A4_LONG.toInt(), A4_SHORT.toInt())
                               else Rect(0, 0, A4_SHORT.toInt(), A4_LONG.toInt())
            }
            val page = doc.startPage(PdfDocument.PageInfo.Builder(
                pageRect.width(), pageRect.height(), i + 1).create())
            val canvas = page.canvas

            val margin = if (options.pageSize == PageSize.EXACT) 0f else options.marginPt
            val availW = pageRect.width() - 2 * margin
            val availH = pageRect.height() - 2 * margin
            val scale = minOf(availW / bmp.width, availH / bmp.height)
            val drawW = bmp.width * scale
            val drawH = bmp.height * scale
            val left = (pageRect.width() - drawW) / 2f
            val top = (pageRect.height() - drawH) / 2f

            canvas.drawBitmap(bmp, Rect(0, 0, bmp.width, bmp.height),
                RectF(left, top, left + drawW, top + drawH), paint)

            if (options.pageNumbers) {
                canvas.drawText("${i + 1} / ${pages.size}",
                    pageRect.width() - margin - 2f, pageRect.height() - 6f, label)
            }
            doc.finishPage(page)
            if (bmp !== source) bmp.recycle()
        }

        doc.writeTo(out)
        doc.close()
        return pages.size
    }

    /** Scale down and/or desaturate one capture for a smaller PDF. */
    private fun prepare(source: Bitmap, options: Options): Bitmap {
        var bmp = source
        if (options.maxWidthPx > 0 && bmp.width > options.maxWidthPx) {
            val ratio = options.maxWidthPx.toFloat() / bmp.width
            bmp = Bitmap.createScaledBitmap(bmp, options.maxWidthPx,
                (bmp.height * ratio).toInt().coerceAtLeast(1), true)
        }
        if (options.grayscale) {
            val gray = Bitmap.createBitmap(bmp.width, bmp.height, Bitmap.Config.ARGB_8888)
            val c = Canvas(gray)
            val p = Paint()
            val cm = android.graphics.ColorMatrix().apply { setSaturation(0f) }
            p.colorFilter = android.graphics.ColorMatrixColorFilter(cm)
            c.drawBitmap(bmp, 0f, 0f, p)
            if (bmp !== source) bmp.recycle()
            bmp = gray
        }
        return bmp
    }
}
