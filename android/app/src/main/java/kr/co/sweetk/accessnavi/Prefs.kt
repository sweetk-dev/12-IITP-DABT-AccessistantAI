package kr.co.sweetk.accessnavi

import android.content.Context
import android.util.Base64

/** 접속 계정 보관 — 앱 전용 저장소(다른 앱이 읽을 수 없다). 백업 대상에서 뺀다(allowBackup=false). */
object Prefs {
    private const val FILE = "accessnavi"
    private const val K_USER = "user"
    private const val K_PASS = "pass"
    private const val K_UPDATE_TS = "update_checked_at"

    private fun sp(ctx: Context) = ctx.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun user(ctx: Context): String = sp(ctx).getString(K_USER, "") ?: ""
    fun pass(ctx: Context): String = sp(ctx).getString(K_PASS, "") ?: ""
    fun hasLogin(ctx: Context): Boolean = user(ctx).isNotEmpty()

    fun saveLogin(ctx: Context, user: String, pass: String) {
        sp(ctx).edit().putString(K_USER, user).putString(K_PASS, pass).apply()
    }

    fun clearLogin(ctx: Context) {
        sp(ctx).edit().remove(K_USER).remove(K_PASS).apply()
    }

    /** HTTP 기본 인증 헤더 값. 계정이 없으면 null. */
    fun basicAuth(ctx: Context): String? {
        val u = user(ctx)
        if (u.isEmpty()) return null
        val raw = (u + ":" + pass(ctx)).toByteArray(Charsets.UTF_8)
        return "Basic " + Base64.encodeToString(raw, Base64.NO_WRAP)
    }

    fun updateCheckedAt(ctx: Context): Long = sp(ctx).getLong(K_UPDATE_TS, 0L)
    fun markUpdateChecked(ctx: Context) {
        sp(ctx).edit().putLong(K_UPDATE_TS, System.currentTimeMillis()).apply()
    }
}
