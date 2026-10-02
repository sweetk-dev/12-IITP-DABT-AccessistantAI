package kr.co.sweetk.accessnavi

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager

/**
 * 길안내가 진행되는 동안 떠 있는 포그라운드 서비스.
 *
 * 화면이 꺼지거나 다른 앱(메신저·전화)으로 넘어가도 위치·마이크·안내 음성이 끊기지 않게
 * 프로세스를 유지한다. 실제 안내는 화면(WebView)이 한다 — 여기서는 알림을 띄우고
 * CPU 가 잠들지 않게만 한다.
 */
class GuideService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            startAsForeground()
        } catch (e: Exception) {
            // 권한이 없거나 화면이 보이지 않는 상태에서 시작된 경우 — 안내는 화면이 켜진 동안만 이어진다
            stopSelf()
            return START_NOT_STICKY
        }
        if (wakeLock == null) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "accessnavi:guide").apply {
                setReferenceCounted(false)
                acquire(MAX_HOLD_MS)
            }
        }
        running = true
        return START_NOT_STICKY
    }

    private fun startAsForeground() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.guide_channel), NotificationManager.IMPORTANCE_LOW)
            )
        }
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val n: Notification = Notification.Builder(this, CHANNEL)
            .setContentTitle(getString(R.string.guide_title))
            .setContentText(getString(R.string.guide_text))
            .setSmallIcon(R.drawable.ic_stat_nav)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // 허용된 권한에 해당하는 종류만 붙인다 — 권한 없는 종류를 붙이면 시작이 거부된다
            val loc = if (hasLocation(this)) ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0
            val mic = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && hasMic(this))
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
            // 둘 다 → 위치만 → 마이크만 순으로 시도한다(한 종류가 거부돼도 나머지로 유지)
            var last: Exception? = null
            for (types in listOf(loc or mic, loc, mic).filter { it != 0 }.distinct()) {
                try {
                    startForeground(NOTIF_ID, n, types)
                    return
                } catch (e: Exception) {
                    last = e
                }
            }
            throw last ?: IllegalStateException("no permission for a foreground service type")
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    override fun onDestroy() {
        running = false
        try { wakeLock?.let { if (it.isHeld) it.release() } } catch (_: Exception) {}
        wakeLock = null
        super.onDestroy()
    }

    companion object {
        private const val CHANNEL = "guide"
        private const val NOTIF_ID = 11
        private const val MAX_HOLD_MS = 4L * 60 * 60 * 1000   // 안내 1회 상한 — 깜빡 켜 둔 채로 배터리를 다 쓰지 않게
        const val ACTION_STOP = "kr.co.sweetk.accessnavi.STOP"

        @Volatile
        var running = false
            private set

        private fun granted(ctx: Context, p: String) = ctx.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED
        private fun hasLocation(ctx: Context) = granted(ctx, Manifest.permission.ACCESS_FINE_LOCATION) ||
            granted(ctx, Manifest.permission.ACCESS_COARSE_LOCATION)
        private fun hasMic(ctx: Context) = granted(ctx, Manifest.permission.RECORD_AUDIO)

        fun start(ctx: Context) {
            // 붙일 수 있는 종류가 없으면 시작하지 않는다 — 시작해 놓고 포그라운드로 올리지 못하면 앱이 죽는다
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && !hasLocation(ctx) && !hasMic(ctx)) return
            val i = Intent(ctx, GuideService::class.java)
            try {
                // startForegroundService 가 아니라 startService 로 시작한다. 전자는 "곧 포그라운드로 올리겠다"는
                // 약속이라, 권한·상태 문제로 올리지 못하면 시스템이 앱을 죽인다. 화면이 보이는 동안에는
                // startService 가 허용되고, 올리기에 실패해도 서비스만 조용히 끝난다.
                ctx.startService(i)
            } catch (_: Exception) {
                // 화면이 보이지 않는 상태에서는 시작할 수 없다 — 다음에 화면이 켜졌을 때 다시 시도된다
            }
        }

        fun stop(ctx: Context) {
            try { ctx.stopService(Intent(ctx, GuideService::class.java)) } catch (_: Exception) {}
        }
    }
}
