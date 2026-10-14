package app.pagesnap.download

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * Downloads run here, not in the screen's coroutine.
 *
 * Instagram and YouTube often feed a 40 MB video slowly; if the student leaves
 * the app during that, a plain background coroutine gets killed and the download
 * dies with it. A foreground service keeps it alive and shows honest progress in
 * the notification shade.
 *
 * Only the *download* needs this. The interesting part - finding pages and making
 * the PDF - needs no service at all: it never touches the network, so Android
 * lets it run whenever the app is in the foreground.
 */
class DownloadService : Service() {

    companion object {
        const val EXTRA_URL = "url"
        private const val CHANNEL = "pagesnap-download"
        private const val NOTIFICATION_ID = 41

        /** Progress is broadcast so the UI can show it; the result path is the last one. */
        const val ACTION_PROGRESS = "app.pagesnap.DOWNLOAD_PROGRESS"
        const val ACTION_DONE = "app.pagesnap.DOWNLOAD_DONE"
        const val EXTRA_PERCENT = "percent"
        const val EXTRA_LINE = "line"
        const val EXTRA_PATH = "path"
        const val EXTRA_ERROR = "error"

        fun start(context: Context, url: String) {
            val intent = Intent(context, DownloadService::class.java).putExtra(EXTRA_URL, url)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var work: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val url = intent?.getStringExtra(EXTRA_URL) ?: return START_NOT_STICKY
        startForeground(NOTIFICATION_ID, notification("Starting…", 0, true))

        work?.cancel()
        work = scope.launch {
            try {
                val file = VideoFetcher(this@DownloadService).download(url) { percent, line ->
                    notifyProgress(percent, line)
                }
                sendBroadcast(Intent(ACTION_DONE).setPackage(packageName)
                    .putExtra(EXTRA_PATH, file.absolutePath))
            } catch (t: Throwable) {
                sendBroadcast(Intent(ACTION_DONE).setPackage(packageName)
                    .putExtra(EXTRA_ERROR, t.message ?: "download failed"))
            } finally {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    private fun notifyProgress(percent: Float, line: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification(line.ifBlank { "Downloading…" }, percent.toInt(), false))
        sendBroadcast(Intent(ACTION_PROGRESS).setPackage(packageName)
            .putExtra(EXTRA_PERCENT, percent).putExtra(EXTRA_LINE, line))
    }

    private fun notification(text: String, percent: Int, indeterminate: Boolean): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL) == null) {
                manager.createNotificationChannel(
                    NotificationChannel(CHANNEL, "Downloading a video",
                        NotificationManager.IMPORTANCE_LOW))
            }
        }
        return NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("PageSnap is downloading the video")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .setProgress(100, percent.coerceIn(0, 100), indeterminate)
            .build()
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }
}
