package kr.co.sweetk.accessnavi

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * 앱 업데이트 — 서비스 서버에 올려 둔 배포본 정보(/app/latest.json)를 보고 새 버전이 있으면
 * 내려받아 설치 화면을 띄운다.
 *
 *   {"versionCode": 20001, "versionName": "2.0.1", "file": "accessnavi-2.0.1.apk",
 *    "sha256": "<설치 파일 해시>", "notes": "바뀐 점"}
 *
 * 내려받은 파일(앱 전용 임시 저장소의 update.apk)은 해시가 맞을 때만 설치로 넘긴다.
 * 설치는 시스템 설치 화면을 거치고, 서명이 다른 파일은 시스템이 거부한다.
 */
object AppUpdater {
    private const val CHECK_EVERY_MS = 6L * 60 * 60 * 1000
    private val APK_NAME = Regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\\.apk$")
    private val ui = Handler(Looper.getMainLooper())

    @Volatile
    private var working = false

    /** 안내 중이 아닐 때만 부른다. force=false 면 6시간에 한 번만 확인한다(앱을 새로 열 때는 force). */
    fun check(activity: Activity, force: Boolean = false) {
        if (working) return
        val auth = Prefs.basicAuth(activity) ?: return
        if (!force && System.currentTimeMillis() - Prefs.updateCheckedAt(activity) < CHECK_EVERY_MS) return
        working = true
        Thread {
            try {
                val info = fetchLatest(auth)
                // 서버가 답했고 새 버전이 없을 때만 '확인함'으로 적는다 — 확인에 실패했거나, 새 버전이 있는데
                // 설치까지 가지 못했으면 다음 실행 때 다시 본다
                if (info != null && info.versionCode <= BuildConfig.VERSION_CODE) Prefs.markUpdateChecked(activity)
                if (info != null && info.versionCode > BuildConfig.VERSION_CODE) {
                    ui.post { if (!activity.isFinishing && !activity.isDestroyed) ask(activity, info, auth) else working = false }
                } else {
                    working = false
                }
            } catch (_: Exception) {
                working = false          // 확인 실패는 조용히 넘긴다 — 다음 실행 때 다시 본다
            }
        }.start()
    }

    private data class Info(val versionCode: Int, val versionName: String, val file: String,
                            val sha256: String, val notes: String)

    private fun open(path: String, auth: String): HttpURLConnection {
        val c = URL(BuildConfig.BASE_URL + path).openConnection() as HttpURLConnection
        c.connectTimeout = 10_000
        c.readTimeout = 30_000
        c.instanceFollowRedirects = false       // 다른 곳으로 돌려보내는 응답은 따르지 않는다(계정 헤더 보호)
        c.setRequestProperty("Authorization", auth)
        return c
    }

    private fun fetchLatest(auth: String): Info? {
        val c = open("/app/latest.json", auth)
        try {
            if (c.responseCode != 200) return null
            val text = c.inputStream.use { it.readBytes().toString(Charsets.UTF_8) }
            val o = JSONObject(text)
            val file = o.optString("file")
            val sha = o.optString("sha256").lowercase()
            if (!APK_NAME.matches(file) || !Regex("^[0-9a-f]{64}$").matches(sha)) return null
            return Info(o.optInt("versionCode"), o.optString("versionName"), file, sha, o.optString("notes"))
        } finally {
            c.disconnect()
        }
    }

    private fun ask(activity: Activity, info: Info, auth: String) {
        if (MainActivity.busy) { working = false; return }      // 안내 중에는 묻지 않는다
        val msg = "새 버전 " + info.versionName + " 이(가) 있습니다. (지금 " + BuildConfig.VERSION_NAME + ")" +
            (if (info.notes.isNotBlank()) "\n\n" + info.notes else "") + "\n\n지금 설치할까요?"
        AlertDialog.Builder(activity)
            .setTitle("업데이트")
            .setMessage(msg)
            .setCancelable(false)
            .setNegativeButton("나중에") { _, _ -> Prefs.markUpdateChecked(activity); working = false }
            .setPositiveButton("설치") { _, _ -> download(activity, info, auth) }
            .show()
    }

    private fun download(activity: Activity, info: Info, auth: String) {
        Toast.makeText(activity, "새 버전을 내려받는 중입니다…", Toast.LENGTH_LONG).show()
        val app = activity.applicationContext
        Thread {
            val out = ApkProvider.file(app)
            try {
                // 이미 받아 둔 파일이 같은 것이면 다시 받지 않는다(설치 화면에서 취소했다가 다시 누른 경우)
                if (out.isFile && sha256(out) == info.sha256) {
                    ui.post { if (!activity.isFinishing && !activity.isDestroyed) installFile(activity, out) }
                    return@Thread
                }
                val c = open("/app/download/" + info.file, auth)
                c.readTimeout = 120_000
                try {
                    if (c.responseCode != 200) throw IllegalStateException("HTTP " + c.responseCode)
                    val md = MessageDigest.getInstance("SHA-256")
                    c.inputStream.use { ins ->
                        out.outputStream().use { os ->
                            val buf = ByteArray(64 * 1024)
                            var total = 0L
                            while (true) {
                                val n = ins.read(buf)
                                if (n < 0) break
                                total += n
                                if (total > MAX_APK_BYTES) throw IllegalStateException("too large")
                                md.update(buf, 0, n)
                                os.write(buf, 0, n)
                            }
                        }
                    }
                    val sha = md.digest().joinToString("") { "%02x".format(it) }
                    if (sha != info.sha256) throw IllegalStateException("hash mismatch")
                } finally {
                    c.disconnect()
                }
                ui.post {
                    if (!activity.isFinishing && !activity.isDestroyed) installFile(activity, out)
                }
            } catch (_: Exception) {
                out.delete()
                ui.post { Toast.makeText(app, "업데이트를 내려받지 못했습니다. 다음에 다시 시도합니다.", Toast.LENGTH_LONG).show() }
            } finally {
                working = false
            }
        }.start()
    }

    private const val MAX_APK_BYTES = 80L * 1024 * 1024

    private fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { ins ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = ins.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    // '이 출처 허용' 설정 화면으로 보낸 상태 — 돌아오면 설치를 잇는다
    @Volatile
    private var waitingSource = false

    /** 화면으로 돌아올 때 부른다. 출처 허용을 켜고 돌아왔으면 설치 화면을 바로 연다. */
    fun resume(activity: Activity) {
        if (!waitingSource) return
        waitingSource = false
        if (activity.packageManager.canRequestPackageInstalls()) installFile(activity, ApkProvider.file(activity))
    }

    /**
     * 시스템 설치 화면을 연다 (2.0.4).
     *
     * 종전에는 설치 세션을 만들고 확인 화면은 알림 수신기가 띄우게 했는데, 실기기(Android 16)에서 내려받기까지만 되고
     * 확인 화면이 뜨지 않았다. 화면에 떠 있는 우리 액티비티가 설치 화면을 직접 여는 표준 방식으로 바꾼다.
     */
    fun installFile(activity: Activity, apk: File) {
        if (!apk.isFile) return
        val pm = activity.packageManager
        // "이 앱이 설치하는 것을 허용"이 꺼져 있으면 그 설정 화면부터 연다 — 허용하고 돌아오면 resume() 이 잇는다
        if (!pm.canRequestPackageInstalls()) {
            Toast.makeText(activity, "설정에서 '이 출처 허용'을 켜고 돌아오면 설치가 이어집니다.", Toast.LENGTH_LONG).show()
            waitingSource = true
            try {
                activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + activity.packageName)))
            } catch (_: Exception) {}
            return
        }
        val view = Intent(Intent.ACTION_VIEW)
            .setDataAndType(ApkProvider.uri(activity), ApkProvider.MIME)
            // 설치 화면은 별도 태스크로 — 덮어쓰는 동안 우리 앱이 종료돼도 설치 화면은 남는다
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            activity.startActivity(view)
        } catch (_: Exception) {
            Toast.makeText(activity, "설치 화면을 열지 못했습니다.", Toast.LENGTH_LONG).show()
        }
    }
}

