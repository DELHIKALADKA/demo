package app.pagesnap.engine

import android.graphics.Bitmap
import android.media.MediaMetadataRetriever

/**
 * Takes the actual screenshot - one per page, at full resolution.
 *
 * The sampler only ever looked at 160x90 thumbnails, so this is where quality is
 * won or lost. We ask for OPTION_CLOSEST (the frame nearest the timestamp, not
 * just the nearest keyframe) because the instant we want is the one where the
 * picture had settled: a keyframe grab could easily land mid-fade.
 */
class PageCapturer {
    /**
     * @param path the video file
     * @param seconds where to grab (from [PageDetector])
     * @param maxWidthPx downscale wider frames (0 = keep native pixels)
     */
    fun grab(path: String, seconds: Double, maxWidthPx: Int = 0): Bitmap? {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(path)
            val bitmap = retriever.getFrameAtTime(
                (seconds * 1_000_000).toLong(),
                MediaMetadataRetriever.OPTION_CLOSEST,
            ) ?: return null
            if (maxWidthPx > 0 && bitmap.width > maxWidthPx) {
                val ratio = maxWidthPx.toFloat() / bitmap.width
                val scaled = Bitmap.createScaledBitmap(
                    bitmap, maxWidthPx, (bitmap.height * ratio).toInt().coerceAtLeast(1), true)
                bitmap.recycle()
                scaled
            } else {
                bitmap
            }
        } catch (t: Throwable) {
            null
        } finally {
            try { retriever.release() } catch (_: Throwable) {}
        }
    }

    /** Duration in seconds, or 0 if the file cannot be read. */
    fun durationSeconds(path: String): Double {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(path)
            (retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                ?.toLongOrNull() ?: 0L) / 1000.0
        } catch (t: Throwable) {
            0.0
        } finally {
            try { retriever.release() } catch (_: Throwable) {}
        }
    }
}
