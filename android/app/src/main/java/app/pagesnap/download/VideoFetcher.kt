package app.pagesnap.download

import android.content.Context
import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Downloads the video when the student pastes a link instead of picking a file.
 *
 * This is the single step a web page can never do for itself, and the reason
 * PageSnap has to be a real app: the downloader runs *on the phone*, so there is
 * no server in the middle, no queue, no operator, and it keeps working when the
 * app is offline except for the link itself.
 *
 * yt-dlp (via youtubedl-android) is a bundled, self-updating copy of the same
 * engine used on the desktop. Instagram/Facebook/YouTube sometimes demand a
 * login cookie: we read cookies from the phone's own browser where possible, and
 * otherwise the student can sign in once inside the app's in-app browser and we
 * cache the cookie on the device.
 *
 * Long downloads run inside a foreground service on Android (see SnapService) -
 * a background coroutine would be killed once the student leaves the screen.
 */
class VideoFetcher(private val context: Context) {

    companion object {
        @Volatile private var initialised = false

        /** One-time setup; cheap and safe to call repeatedly. */
        @Synchronized
        fun ensureReady(context: Context) {
            if (initialised) return
            YoutubeDL.getInstance().init(context.applicationContext)
            initialised = true
        }
    }

    private fun outputDir(): File =
        File(context.filesDir, "videos").apply { mkdirs() }

    /**
     * @param url the post or video link
     * @param onProgress (percent 0..100, line of yt-dlp output)
     */
    suspend fun download(
        url: String,
        onProgress: (Float, String) -> Unit = { _, _ -> },
    ): File = withContext(Dispatchers.IO) {
        ensureReady(context)
        val dir = outputDir()
        val request = YoutubeDLRequest(url).apply {
            // Prefer a plain mp4 so the phone's own decoder can read it back.
            addOption("-f", "mp4/bestvideo*+bestaudio/best[ext=mp4]/best")
            addOption("-o", File(dir, "%(title).80s-%(id)s.%(ext)s").absolutePath)
            addOption("--no-playlist")
            addOption("--restrict-filenames")
            addOption("--newline")
        }

        val response = YoutubeDL.getInstance().execute(request) { progress, _, line ->
            onProgress(progress.toFloat(), line ?: "")
        }
        val out = response.out
        val file = dir.listFiles()
            ?.filter { it.isFile && it.lastModified() > System.currentTimeMillis() - 10 * 60_000 }
            ?.maxByOrNull { it.lastModified() }
        file ?: error("the download finished but no file was found" +
                (out?.let { " ($it)" } ?: ""))
    }

    /** Keeps the bundled downloader up to date - it is what makes links keep working. */
    suspend fun updateIfNeeded(onProgress: (String) -> Unit = {}) = withContext(Dispatchers.IO) {
        ensureReady(context)
        try {
            onProgress("Updating the link downloader…")
            YoutubeDL.getInstance().updateYoutubeDL(context, YoutubeDL.UpdateChannel.STABLE)
            onProgress("Link downloader is up to date.")
        } catch (t: Throwable) {
            // Not fatal: the bundled version keeps working.
            onProgress("Could not update the downloader (${t.message}). Using the bundled copy.")
        }
    }
}
