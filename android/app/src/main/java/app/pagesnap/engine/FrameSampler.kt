package app.pagesnap.engine

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import java.nio.ByteBuffer

/**
 * Pulls small grayscale frames out of a video, on the device, offline.
 *
 * Why not just call `MediaMetadataRetriever.getFrameAtTime` in a loop?
 * That re-seeks and re-decodes for every frame, so a 10-minute lecture would take
 * minutes. Instead we decode the file once, in order, using the phone's hardware
 * decoder and read the luma (Y) plane - which *is* a grayscale image, at exactly
 * the resolution we want to compare. One pass, no image conversion, no temp files.
 *
 * The decoded plane is box-averaged down to 160x90 (see [Signatures]), which also
 * removes sensor noise, and one sample is emitted every 1/[fps] seconds.
 */
class FrameSampler(
    private val fps: Float = DetectDefaults.ANALYZE_FPS,
    private val outWidth: Int = DetectDefaults.ANALYSIS_WIDTH,
    private val outHeight: Int = DetectDefaults.ANALYSIS_HEIGHT,
) {

    class Result(val frames: Int, val durationSeconds: Double, val note: String? = null)

    /**
     * Streams samples to [onFrame] as (index, seconds, gray160x90). Keeping it a
     * callback means we never hold a whole video's worth of frames in memory.
     */
    fun sample(path: String, onFrame: (Int, Double, ByteArray) -> Unit): Result {
        val extractor = MediaExtractor()
        var codec: MediaCodec? = null
        try {
            extractor.setDataSource(path)
            val track = (0 until extractor.trackCount).firstOrNull { i ->
                extractor.getTrackFormat(i).getString(MediaFormat.KEY_MIME)
                    ?.startsWith("video/") == true
            } ?: return Result(0, 0.0, "That file has no video track.")

            extractor.selectTrack(track)
            val format = extractor.getTrackFormat(track)
            val mime = format.getString(MediaFormat.KEY_MIME)!!
            val durationUs = if (format.containsKey(MediaFormat.KEY_DURATION))
                format.getLong(MediaFormat.KEY_DURATION) else 0L

            val decoder = MediaCodec.createDecoderByType(mime)
            codec = decoder
            decoder.configure(format, null, null, 0)   // no surface: we want the pixels
            decoder.start()

            val info = MediaCodec.BufferInfo()
            val stepUs = (1_000_000.0 / fps).toLong()
            var nextEmitUs = 0L
            var index = 0
            var sawInputEnd = false
            var sawOutputEnd = false
            val gray = ByteArray(outWidth * outHeight)

            while (!sawOutputEnd) {
                if (!sawInputEnd) {
                    val inIndex = decoder.dequeueInputBuffer(10_000)
                    if (inIndex >= 0) {
                        val buffer = decoder.getInputBuffer(inIndex)!!
                        val size = extractor.readSampleData(buffer, 0)
                        if (size < 0) {
                            decoder.queueInputBuffer(inIndex, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                            sawInputEnd = true
                        } else {
                            decoder.queueInputBuffer(inIndex, 0, size, extractor.sampleTime, 0)
                            extractor.advance()
                        }
                    }
                }

                when (val outIndex = decoder.dequeueOutputBuffer(info, 10_000)) {
                    MediaCodec.INFO_TRY_AGAIN_LATER -> Unit
                    MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> Unit
                    else -> {
                        if (outIndex >= 0) {
                            val pts = info.presentationTimeUs
                            if (pts >= nextEmitUs && info.size > 0) {
                                val image = decoder.getOutputImage(outIndex)
                                if (image != null && image.planes.isNotEmpty()) {
                                    val plane = image.planes[0]
                                    val rowStride = plane.rowStride
                                    val pixelStride = plane.pixelStride
                                    val buf: ByteBuffer = plane.buffer
                                    copyLuma(buf, rowStride, pixelStride, image.width, image.height,
                                        outWidth, outHeight, gray)
                                    onFrame(index, pts / 1_000_000.0, gray.copyOf())
                                    index++
                                }
                                while (nextEmitUs <= pts) nextEmitUs += stepUs
                            }
                            decoder.releaseOutputBuffer(outIndex, false)
                            if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) sawOutputEnd = true
                        }
                    }
                }
            }
            return Result(index, durationUs / 1_000_000.0)
        } catch (t: Throwable) {
            // Some devices refuse surface-less decoding of exotic files. Fall back
            // to the retriever, which is slower but works almost everywhere.
            val fallback = sampleWithRetriever(path, onFrame)
            return fallback ?: Result(0, 0.0, "Could not decode this video: ${t.message}")
        } finally {
            try { codec?.stop() } catch (_: Throwable) {}
            try { codec?.release() } catch (_: Throwable) {}
            try { extractor.release() } catch (_: Throwable) {}
        }
    }

    /** Box-average the decoded luma plane straight into the 160x90 buffer. */
    private fun copyLuma(
        buffer: ByteBuffer, rowStride: Int, pixelStride: Int,
        srcW: Int, srcH: Int, outW: Int, outH: Int, out: ByteArray,
    ) {
        buffer.rewind()
        for (oy in 0 until outH) {
            val y0 = oy * srcH / outH
            val y1 = maxOf((oy + 1) * srcH / outH, y0 + 1)
            for (ox in 0 until outW) {
                val x0 = ox * srcW / outW
                val x1 = maxOf((ox + 1) * srcW / outW, x0 + 1)
                var acc = 0
                var n = 0
                for (y in y0 until y1) {
                    var pos = y * rowStride + x0 * pixelStride
                    for (x in x0 until x1) {
                        acc += buffer.get(pos).toInt() and 0xFF
                        pos += pixelStride
                        n++
                    }
                }
                out[oy * outW + ox] = (acc / maxOf(n, 1)).toByte()
            }
        }
    }

    /** Slow but universally available fallback: seek and grab. */
    private fun sampleWithRetriever(
        path: String, onFrame: (Int, Double, ByteArray) -> Unit,
    ): Result? {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(path)
            val durationMs = retriever
                .extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                ?.toLongOrNull() ?: return null
            val stepMs = (1000.0 / fps).toLong().coerceAtLeast(1)
            var index = 0
            var t = 0L
            while (t <= durationMs) {
                val bitmap = retriever.getFrameAtTime(t * 1000, MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
                    ?: run { t += stepMs; continue }
                val scaled = android.graphics.Bitmap.createScaledBitmap(bitmap, outWidth, outHeight, true)
                val gray = ByteArray(outWidth * outHeight)
                val pixels = IntArray(outWidth * outHeight)
                scaled.getPixels(pixels, 0, outWidth, 0, 0, outWidth, outHeight)
                for (i in pixels.indices) {
                    val p = pixels[i]
                    val luma = ((p shr 16 and 0xFF) * 77 + (p shr 8 and 0xFF) * 150 + (p and 0xFF) * 29) shr 8
                    gray[i] = luma.toByte()
                }
                onFrame(index++, t / 1000.0, gray)
                bitmap.recycle()
                if (scaled !== bitmap) scaled.recycle()
                t += stepMs
            }
            Result(index, durationMs / 1000.0, "used the slow fallback decoder")
        } catch (t: Throwable) {
            null
        } finally {
            try { retriever.release() } catch (_: Throwable) {}
        }
    }
}
