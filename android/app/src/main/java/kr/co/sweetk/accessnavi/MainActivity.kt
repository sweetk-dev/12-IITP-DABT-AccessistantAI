package kr.co.sweetk.accessnavi

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.webkit.GeolocationPermissions
import android.webkit.HttpAuthHandler
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewDatabase
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.Toast
import org.json.JSONObject

/**
 * 안내 중에는 화면이 가려져도(화면 꺼짐·다른 앱) 페이지에 '보이는 중'이라고 알리는 WebView.
 *
 * 가려졌다고 알리면 브라우저 엔진이 타이머를 늦추고 화면 갱신을 멈춘다 — 다음 안내 지점 판정과
 * 안내 음성 재생이 밀린다. 안내 중이 아닐 때는 그대로 전달한다.
 */
class GuideWebView(context: Context) : WebView(context) {
    override fun onWindowVisibilityChanged(visibility: Int) {
        super.onWindowVisibilityChanged(if (MainActivity.busy) View.VISIBLE else visibility)
    }

    /** 안내가 끝나면 실제 상태를 다시 알린다 — 가려진 채로 '보이는 중'에 머물러 전력을 쓰지 않게. */
    fun syncVisibility() {
        super.onWindowVisibilityChanged(windowVisibility)
    }
}

/**
 * 이동경로 안내 앱 — 서비스 화면(/navi)을 담는 껍데기.
 *
 * 화면과 안내 로직은 모두 웹에 있다. 앱이 더하는 것은 브라우저로는 안 되는 것들이다:
 *   · 화면이 꺼지거나 다른 앱으로 가도 안내가 이어지게 한다(GuideService)
 *   · 접속 계정을 한 번만 입력하게 한다
 *   · 위치·마이크 권한을 앱 권한으로 한 번에 받는다
 *   · 뒤로 가기 키: 안내 중이면 화면의 "안내를 끝낼까요?", 그 밖에는 "앱을 종료할까요?"
 *   · 통화가 시작되면 안내를 멈추고 끝나면 잇는다
 *   · 정책상담 화면이 넘겨준 목적지를 받는다(accessnavi://navi?...)
 *   · 새 버전을 확인해 설치한다(AppUpdater)
 */
class MainActivity : Activity() {

    private lateinit var root: FrameLayout
    private var web: WebView? = null
    private var pageReady = false
    private var opened = false
    private var geoAsked = false
    private var authTries = 0
    private var loginShowing = false
    private var exitShowing = false
    private var backAsking = false
    private var pendingWebPermission: PermissionRequest? = null
    private var pendingGeo: Pair<String, GeolocationPermissions.Callback>? = null
    private val handler = Handler(Looper.getMainLooper())
    private var callHold = false

    private val base: Uri by lazy { Uri.parse(BuildConfig.BASE_URL) }

    // ───────────────────────── 생명주기 ─────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        root = FrameLayout(this).apply { setBackgroundColor(Color.WHITE) }
        setContentView(root)
        fitSystemBars()
        createWebView()
        // 권한을 먼저 받고 나서 화면을 연다 — 화면의 마이크·위치 요청과 겹치지 않게
        if (!askRuntimePermissions()) openService()
    }

    /**
     * 화면을 상태 표시줄·내비게이션 바·키보드 안쪽에 맞춘다.
     *
     * 최신 안드로이드는 앱 화면을 시스템 바 밑까지 깔아 그린다. 그대로 두면 화면 맨 위가 상태 표시줄과,
     * 맨 아래 입력 줄이 내비게이션 바와 겹쳐 누를 수 없다. 시스템이 알려 주는 여백만큼 안쪽으로 들인다.
     */
    private fun fitSystemBars() {
        window.statusBarColor = Color.WHITE
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) window.navigationBarColor = Color.WHITE
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false)
            val light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or
                WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS
            window.insetsController?.setSystemBarsAppearance(light, light)   // 흰 바탕에 어두운 아이콘
            root.setOnApplyWindowInsetsListener { v, insets ->
                val b = insets.getInsets(
                    WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime()
                )
                v.setPadding(b.left, b.top, b.right, b.bottom)
                WindowInsets.CONSUMED
            }
            root.requestApplyInsets()
        } else {
            // 이전 버전은 시스템이 화면을 바 안쪽에 맞춰 준다 — 아이콘 색만 맞춘다
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR else 0)
        }
    }

    private fun openService() {
        if (opened) return
        opened = true
        if (Prefs.hasLogin(this)) {
            loadStart(intent)
            AppUpdater.check(this, force = true)     // 앱을 새로 열 때마다 새 버전을 확인한다 (2.0.2)
        } else showLogin(null)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (!opened) return          // 아직 화면을 열기 전(권한 확인 중) — 열 때 이 인텐트로 한 번만 연다
        val q = handoffQuery(intent) ?: return
        if (!Prefs.hasLogin(this)) return
        val w = web ?: return
        if (pageReady) {
            // 떠 있는 화면에 새 목적지를 넘긴다 — 문자열은 JSON 으로 감싸 그대로 실행되지 않게 한다
            val arg = JSONObject.quote("?$q")
            w.evaluateJavascript(
                "(function(){try{if(window.NAVI&&NAVI.handoff){NAVI.handoff(NAVI.parseHandoff($arg));}}catch(e){}})()", null
            )
        } else {
            w.loadUrl(startUrl(intent))
        }
    }

    override fun onResume() {
        super.onResume()
        if (busy && !GuideService.running) GuideService.start(this)   // 화면이 꺼진 사이 시작하지 못했으면 지금
        if (!busy && Prefs.hasLogin(this)) AppUpdater.check(this)
    }

    override fun onDestroy() {
        busy = false
        GuideService.stop(this)
        handler.removeCallbacks(callWatch)
        web?.let {
            it.stopLoading()
            (it.parent as? ViewGroup)?.removeView(it)
            it.destroy()
        }
        web = null
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        // 뒤로 가기 — 먼저 화면에 묻는다. 안내 중이면 화면이 "안내를 끝낼까요?"를 띄우고(true),
        // 그 밖(안내 전·대화 중·연결 실패 화면)에는 앱 종료를 묻는다.
        if (loginShowing) return                 // 접속 창에는 닫기 버튼이 있다 — 그 위에 겹쳐 묻지 않는다
        val w = web
        if (w == null || !pageReady) { confirmExit(); return }
        if (backAsking) return                   // 앞선 물음의 답을 기다리는 중
        backAsking = true
        // 화면이 멈춰 답이 오지 않으면 앱이 직접 종료를 묻는다
        val fallback = Runnable { if (backAsking) { backAsking = false; confirmExit() } }
        handler.postDelayed(fallback, 700)
        w.evaluateJavascript("(function(){try{return !!(window.NAVI&&NAVI.backKey&&NAVI.backKey());}catch(e){return false;}})()") { r ->
            if (!backAsking) return@evaluateJavascript      // 이미 대신 물었다
            backAsking = false
            handler.removeCallbacks(fallback)
            if (r != "true") confirmExit()
        }
    }

    private fun confirmExit() {
        if (exitShowing || isFinishing) return
        exitShowing = true
        AlertDialog.Builder(this)
            .setTitle("앱을 종료할까요?")
            .setMessage("이동경로 안내를 닫습니다.")
            .setNegativeButton("취소", null)
            .setPositiveButton("종료") { _, _ -> finishAndRemoveTask() }   // 뒤에 남기지 않고 완전히 닫는다
            .setOnDismissListener { exitShowing = false }
            .show()
    }

    // ───────────────────────── 주소 ─────────────────────────

    /** 정책상담이 넘겨준 주소(accessnavi://navi?...)의 질의 문자열. 허용한 이름만 다시 조립한다. */
    private fun handoffQuery(intent: Intent?): String? {
        val d = intent?.data ?: return null
        if (intent.action != Intent.ACTION_VIEW || d.scheme != "accessnavi" || d.host != "navi") return null
        val b = Uri.Builder()
        for (k in HANDOFF_KEYS) {
            val v = try { d.getQueryParameter(k) } catch (_: Exception) { null }
            if (!v.isNullOrEmpty() && v.length <= 200) b.appendQueryParameter(k, v)
        }
        return b.build().encodedQuery?.takeIf { it.isNotEmpty() }
    }

    private fun startUrl(intent: Intent?): String {
        val q = handoffQuery(intent)
        return BuildConfig.BASE_URL + "/navi" + (if (q != null) "?$q" else "")
    }

    private fun loadStart(intent: Intent?) {
        pageReady = false
        authTries = 0
        web?.loadUrl(startUrl(intent))
    }

    private fun sameOrigin(u: Uri?): Boolean {
        if (u == null) return false
        fun port(x: Uri) = if (x.port != -1) x.port else if (x.scheme == "https") 443 else 80
        return u.scheme == base.scheme && u.host.equals(base.host, ignoreCase = true) && port(u) == port(base)
    }

    // ───────────────────────── WebView ─────────────────────────

    @SuppressLint("SetJavaScriptEnabled")
    private fun createWebView() {
        val w = GuideWebView(this)
        w.layoutParams = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT
        )
        val s = w.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.mediaPlaybackRequiresUserGesture = false      // 안내 음성은 누르지 않아도 나와야 한다
        s.setGeolocationEnabled(true)
        s.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        s.allowFileAccess = false
        s.allowContentAccess = false
        s.setSupportMultipleWindows(false)
        s.userAgentString = s.userAgentString + " AccessNaviApp/" + BuildConfig.VERSION_NAME
        w.addJavascriptInterface(Bridge(), "AccessNaviApp")
        w.webViewClient = Client()
        w.webChromeClient = Chrome()
        root.addView(w)
        web = w
    }

    private inner class Client : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val u = request.url
            if (sameOrigin(u)) return false
            // 서비스 밖 주소는 앱 안에서 열지 않는다 — 전화·외부 페이지는 해당 앱으로
            try {
                when (u.scheme) {
                    "tel" -> startActivity(Intent(Intent.ACTION_DIAL, u))
                    "http", "https" -> startActivity(Intent(Intent.ACTION_VIEW, u))
                }
            } catch (_: Exception) {}
            return true
        }

        override fun onReceivedHttpAuthRequest(view: WebView, handler: HttpAuthHandler, host: String, realm: String) {
            val user = Prefs.user(this@MainActivity)
            // 저장한 계정은 서비스 주소에만 보낸다. 한 번 거절되면 다시 입력받는다.
            if (!host.equals(base.host, ignoreCase = true) || user.isEmpty() || authTries >= 1) {
                handler.cancel()
                if (host.equals(base.host, ignoreCase = true)) {
                    showLogin(if (user.isEmpty()) null else "아이디 또는 비밀번호가 맞지 않습니다.")
                }
                return
            }
            authTries++
            handler.proceed(user, Prefs.pass(this@MainActivity))
        }

        override fun onPageFinished(view: WebView, url: String) {
            if (sameOrigin(Uri.parse(url))) {
                pageReady = true
                authTries = 0
            }
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            pageReady = false
            view.loadDataWithBaseURL(null, OFFLINE_HTML, "text/html", "utf-8", null)
        }

        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            // 화면 프로세스가 죽었다 — 새로 만들어 다시 연다(앱이 함께 죽지 않게)
            (view.parent as? ViewGroup)?.removeView(view)
            view.destroy()
            web = null
            pageReady = false
            setBusy(false)
            createWebView()
            if (Prefs.hasLogin(this@MainActivity)) loadStart(null)
            return true
        }
    }

    private inner class Chrome : WebChromeClient() {
        override fun onPermissionRequest(request: PermissionRequest) {
            runOnUiThread {
                val wantsMic = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                if (!wantsMic || !sameOrigin(request.origin)) { request.deny(); return@runOnUiThread }
                if (granted(Manifest.permission.RECORD_AUDIO)) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    pendingWebPermission = request
                    requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
                }
            }
        }

        override fun onPermissionRequestCanceled(request: PermissionRequest) {
            if (pendingWebPermission === request) pendingWebPermission = null
        }

        override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
            if (!sameOrigin(Uri.parse(origin))) { callback.invoke(origin, false, false); return }
            if (hasLocation()) {
                callback.invoke(origin, true, false)     // 대략적 위치만 허용한 경우도 그대로 넘긴다
            } else if (geoAsked) {
                callback.invoke(origin, false, false)    // 이미 물었고 거절됐다 — 되풀이해 묻지 않는다
            } else {
                geoAsked = true
                pendingGeo = Pair(origin, callback)
                requestPermissions(
                    arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION), REQ_GEO
                )
            }
        }
    }

    /** 화면(자바스크립트)이 부르는 것 — 서비스 주소의 화면만 쓴다. */
    private inner class Bridge {
        @JavascriptInterface
        fun setBusy(on: Boolean) {
            runOnUiThread { if (onServicePage()) this@MainActivity.setBusy(on) }
        }

        /** 접속 계정 바꾸기 — 저장한 계정을 지우고 앱을 닫는다. 다시 열면 계정을 묻는다. */
        @JavascriptInterface
        fun logout() {
            runOnUiThread { if (onServicePage()) confirmLogout() }
        }

        @JavascriptInterface
        fun reload() {
            runOnUiThread { if (Prefs.hasLogin(this@MainActivity)) loadStart(null) }
        }

        /** 마이크 권한을 받았는가 — 화면이 음성으로 시작할지 정할 때 쓴다(WebView 는 브라우저식 권한 조회가 안 된다). */
        @JavascriptInterface
        fun micGranted(): Boolean = granted(Manifest.permission.RECORD_AUDIO)

        @JavascriptInterface
        fun version(): String = BuildConfig.VERSION_NAME
    }

    private fun onServicePage(): Boolean = sameOrigin(web?.url?.let { Uri.parse(it) })

    // ───────────────────────── 안내 상태 ─────────────────────────

    private fun setBusy(on: Boolean) {
        if (busy == on) return
        busy = on
        if (on) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)   // 안내 중에만 화면을 켜 둔다
            GuideService.start(this)
            startCallWatch()
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            GuideService.stop(this)
            stopCallWatch()
            (web as? GuideWebView)?.syncVisibility()
            AppUpdater.check(this)
        }
    }

    // 통화 감지 — 전화가 울리거나 통화 중이면 안내와 마이크를 멈추고, 끝나면 잇는다.
    // 통화 권한 없이 오디오 모드만 본다(인터넷 전화는 구분할 수 없어 대상이 아니다).
    private val callWatch = object : Runnable {
        override fun run() {
            if (!busy) return
            val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val inCall = am.mode == AudioManager.MODE_IN_CALL || am.mode == AudioManager.MODE_RINGTONE
            if (inCall != callHold) {
                callHold = inCall
                tellAudioFocus(!inCall)
            }
            handler.postDelayed(this, 1000)
        }
    }

    private fun tellAudioFocus(gain: Boolean) {
        val w = web ?: return
        if (!pageReady) return
        w.evaluateJavascript("(function(){try{if(window.__APP&&__APP.onAudioFocus)__APP.onAudioFocus($gain);}catch(e){}})()", null)
    }

    private fun startCallWatch() {
        handler.removeCallbacks(callWatch)
        handler.postDelayed(callWatch, 1000)
    }

    private fun stopCallWatch() {
        handler.removeCallbacks(callWatch)
        if (callHold) { callHold = false; tellAudioFocus(true) }
    }

    // ───────────────────────── 권한 ─────────────────────────

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED
    private fun hasLocation() = granted(Manifest.permission.ACCESS_FINE_LOCATION) ||
        granted(Manifest.permission.ACCESS_COARSE_LOCATION)

    /** 처음 받을 권한이 있으면 묻고 true. 결과가 오면 화면을 연다. */
    private fun askRuntimePermissions(): Boolean {
        val need = ArrayList<String>()
        if (!hasLocation()) {
            geoAsked = true
            need.add(Manifest.permission.ACCESS_FINE_LOCATION)
            need.add(Manifest.permission.ACCESS_COARSE_LOCATION)
        }
        if (!granted(Manifest.permission.RECORD_AUDIO)) need.add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && !granted(Manifest.permission.POST_NOTIFICATIONS)) {
            need.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (need.isEmpty()) return false
        requestPermissions(need.toTypedArray(), REQ_START)
        return true
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        // 어느 요청의 결과든, 기다리던 화면 쪽 요청을 현재 권한 상태로 마무리한다(거절이면 거절로 — 걸려 있지 않게)
        pendingWebPermission?.let { req ->
            pendingWebPermission = null
            try {
                if (granted(Manifest.permission.RECORD_AUDIO)) req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                else req.deny()
            } catch (_: Exception) {}
        }
        pendingGeo?.let { (origin, cb) ->
            pendingGeo = null
            cb.invoke(origin, hasLocation(), false)
        }
        if (requestCode == REQ_START) openService()
    }

    // ───────────────────────── 접속 계정 ─────────────────────────

    private fun confirmLogout() {
        if (isFinishing) return
        if (busy) {
            Toast.makeText(this, "안내를 끝낸 뒤에 로그아웃할 수 있습니다.", Toast.LENGTH_LONG).show()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("로그아웃")
            .setMessage("지금 계정(" + Prefs.user(this) + ")을 지우고 앱을 닫습니다. 앱을 다시 열면 아이디와 비밀번호를 묻습니다.")
            .setNegativeButton("취소", null)
            .setPositiveButton("로그아웃") { _, _ ->
                setBusy(false)
                Prefs.clearLogin(this)
                try { WebViewDatabase.getInstance(this).clearHttpAuthUsernamePassword() } catch (_: Exception) {}
                // 화면 엔진이 기억한 계정은 프로세스가 끝나야 지워진다 — 앱을 완전히 닫는다
                finishAndRemoveTask()
                handler.postDelayed({ Process.killProcess(Process.myPid()) }, 800)
            }
            .show()
    }

    private fun showLogin(message: String?) {
        if (loginShowing || isFinishing) return
        loginShowing = true
        val pad = (20 * resources.displayMetrics.density).toInt()
        val user = EditText(this).apply {
            hint = "아이디"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setText(Prefs.user(this@MainActivity))
        }
        val pass = EditText(this).apply {
            hint = "비밀번호"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
            addView(user)
            addView(pass)
        }
        AlertDialog.Builder(this)
            .setTitle("이동경로 안내 접속")
            .setMessage(message ?: "안내받은 아이디와 비밀번호를 입력해 주세요. 한 번만 입력하면 됩니다.")
            .setView(box)
            .setCancelable(false)
            .setNegativeButton("닫기") { _, _ -> loginShowing = false; finish() }
            .setPositiveButton("확인") { _, _ ->
                loginShowing = false
                val u = user.text.toString().trim()
                if (u.isEmpty()) {
                    showLogin("아이디를 입력해 주세요.")
                } else {
                    Prefs.saveLogin(this, u, pass.text.toString())
                    loadStart(intent)
                    AppUpdater.check(this, force = true)
                }
            }
            .show()
    }

    companion object {
        private const val REQ_START = 1
        private const val REQ_MIC = 2
        private const val REQ_GEO = 3
        private val HANDOFF_KEYS = arrayOf("dest_name", "dest_poi", "dest_kind", "dest_lat", "dest_lng", "profile")

        /** 상담·안내가 진행 중인가 — 화면이 알려 준다. */
        @Volatile
        var busy = false
            private set

        private const val OFFLINE_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:sans-serif;margin:0;padding:32px 24px;color:#0f172a}h1{font-size:22px}
p{font-size:17px;line-height:1.5;color:#334155}button{font-size:18px;font-weight:700;padding:14px 18px;border:0;
border-radius:12px;background:#2563eb;color:#fff;width:100%;margin-top:16px}</style></head>
<body><h1>연결할 수 없습니다</h1><p>통신 상태를 확인한 뒤 다시 시도해 주세요.</p>
<button onclick="AccessNaviApp.reload()">다시 시도</button></body></html>"""
    }
}
