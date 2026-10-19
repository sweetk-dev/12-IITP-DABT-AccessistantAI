package kr.co.sweetk.accessnavi

import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
import com.kakao.vectormap.GestureType
import com.kakao.vectormap.KakaoMap
import com.kakao.vectormap.KakaoMapReadyCallback
import com.kakao.vectormap.KakaoMapSdk
import com.kakao.vectormap.LatLng
import com.kakao.vectormap.MapAuthException
import com.kakao.vectormap.MapLifeCycleCallback
import com.kakao.vectormap.MapView
import com.kakao.vectormap.camera.CameraAnimation
import com.kakao.vectormap.camera.CameraPosition
import com.kakao.vectormap.camera.CameraUpdateFactory
import com.kakao.vectormap.label.Label
import com.kakao.vectormap.label.LabelOptions
import com.kakao.vectormap.label.LabelStyle
import com.kakao.vectormap.label.LabelStyles
import com.kakao.vectormap.route.RouteLine
import com.kakao.vectormap.route.RouteLineOptions
import com.kakao.vectormap.route.RouteLineSegment
import com.kakao.vectormap.route.RouteLineStyle
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * 길안내 화면의 지도 칸을 그리는 카카오맵 Android SDK(v2) 지도 — 지도 엔진 준비와 시험 화면.
 *
 * 웹 지도(카카오 JavaScript 지도)는 진행 방향으로 지도를 돌릴 수 없다. 그래서 앱 안에서는 웹 화면의
 * 지도 칸(#naviMap) 자리에 이 지도를 정확히 겹쳐 놓는다. 화면 배치·안내 로직은 계속 웹에 있고,
 * 웹은 "지도 칸이 어디에 얼마만큼 있는지"와 "무엇을 그릴지"만 알려 준다(NativeMapController).
 */
object NativeMap {
    private const val TAG = "NativeMap"

    /** 지도 엔진을 쓸 수 있는가 — 키가 빌드에 들어 있고 초기화에 성공했을 때만 true. */
    @Volatile
    var sdkReady = false
        private set

    /**
     * 앱 시작 때 한 번 부른다. 키가 없거나(빌드 설정 누락) 지도 엔진이 이 단말 구조용으로
     * 들어 있지 않으면 조용히 실패하고, 길안내 화면은 웹 지도를 그대로 쓴다.
     */
    fun initSdk(context: Context) {
        if (sdkReady) return
        val key = BuildConfig.KAKAO_NATIVE_KEY
        if (key.isBlank()) { Log.w(TAG, "native key missing — web map only"); return }
        sdkReady = try {
            // 엔진 라이브러리가 없는 구조(32비트 전용·x86)면 여기서 UnsatisfiedLinkError 가 난다
            System.loadLibrary("K3fAndroid")
            KakaoMapSdk.init(context.applicationContext, key)
            true
        } catch (t: Throwable) {
            Log.w(TAG, "sdk init failed — web map only", t)
            false
        }
    }

    /**
     * (디버그 빌드 시험용) 지도 하나를 화면 전체에 띄우고 인증 결과를 글자로 보여 준다.
     * 콘솔에 등록한 패키지명·키 해시가 이 빌드의 서명과 맞는지 확인하는 데 쓴다.
     *   adb shell am start -n kr.co.sweetk.accessnavi/.MainActivity --ez test_map true
     */
    fun showAuthTest(activity: Activity, root: FrameLayout) {
        root.removeAllViews()
        val label = TextView(activity).apply {
            textSize = 16f
            setBackgroundColor(0xEEFFFFFF.toInt())
            setPadding(24, 24, 24, 24)
            text = if (sdkReady) "지도 인증 확인 중…" else "지도 엔진 초기화 실패(키 또는 엔진 없음)"
        }
        if (!sdkReady) { root.addView(label); return }
        val mv = MapView(activity)
        root.addView(mv, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(label, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        mv.start(object : MapLifeCycleCallback() {
            override fun onMapDestroy() {}
            override fun onMapError(error: Exception) {
                // 인증 실패(키 해시·패키지명 불일치 등)는 여기로 온다 — 기록에 남겨 adb 로 확인한다
                val code = (error as? MapAuthException)?.errorCode
                Log.e(TAG, "AUTH_TEST map error code=$code $error")
                activity.runOnUiThread { label.text = "지도 오류 code=$code: $error" }
            }
        }, object : KakaoMapReadyCallback() {
            override fun onMapReady(map: KakaoMap) {
                Log.i(TAG, "AUTH_TEST map ready")
                activity.runOnUiThread { label.text = "지도 인증 성공 (" + BuildConfig.VERSION_NAME + ")" }
                // 회전 규칙 측정(디버그): 카메라를 camDeg 만큼 돌리고, 위쪽을 가리키는 화살표 4개를 0·90·180·270 라디안으로 돌린다.
                val camDeg = activity.intent?.getDoubleExtra("cam_deg", -1.0) ?: -1.0
                if (camDeg >= 0) activity.runOnUiThread {
                    val c = LatLng.from(37.3943, 126.9568)
                    val cur = map.cameraPosition
                    if (cur != null) map.moveCamera(CameraUpdateFactory.newCameraPosition(
                        CameraPosition.from(CameraPosition.Builder(cur).setPosition(c).setZoomLevel(17).setRotationAngle(Math.toRadians(camDeg)))))
                    val d = activity.resources.displayMetrics.density
                    val s = (30 * d).toInt(); val b = Bitmap.createBitmap(s, s, Bitmap.Config.ARGB_8888); val cv = Canvas(b)
                    val p = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.parseColor("#2563EB") }
                    cv.drawCircle(s / 2f, s / 2f, s / 2f, p); p.color = Color.WHITE
                    cv.drawPath(Path().apply { moveTo(s / 2f, 4 * d); lineTo(s - 8 * d, s - 8 * d); lineTo(8 * d, s - 8 * d); close() }, p)
                    val offs = listOf(0.0 to 0.0, 0.0007 to 0.0, 0.0 to 0.0009, -0.0007 to 0.0)   // 가운데·북·동·남
                    offs.forEachIndexed { i, o ->
                        val lb = map.labelManager?.layer?.addLabel(LabelOptions.from("t$i", LatLng.from(c.latitude + o.first, c.longitude + o.second))
                            .setStyles(LabelStyles.from(LabelStyle.from(b).setAnchorPoint(0.5f, 0.5f))))
                        lb?.rotateTo((i * PI / 2).toFloat())
                    }
                    label.text = "cam=$camDeg  가운데0 북90 동180 남270(도, rotateTo 값)"
                }
            }
            override fun getPosition(): LatLng = LatLng.from(37.3943, 126.9568)   // 안양시청 부근
            override fun getZoomLevel(): Int = 15
        })
    }
}

/**
 * 웹 화면이 보내는 지도 명령을 받아 앱 지도에 그리는 쪽.
 *
 * 화면 → 앱 (AccessNaviApp.mapRect / mapCmd)
 *   · mapRect(x, y, w, h)  지도 칸의 위치·크기(웹 화면 기준 실제 픽셀). w·h 가 0 이면 지도를 숨긴다
 *                          (다른 화면으로 갔거나 팝업이 떠서 지도 칸이 가려진 경우).
 *   · mapCmd([명령...])     JSON 배열. 명령 종류는 apply() 의 when 절 참조.
 * 앱 → 화면 (window.__NMAP.emit(종류, 값))
 *   · ready / error{code}       지도 준비 완료 / 실패(실패하면 화면이 웹 지도로 되돌아간다)
 *   · click{lat,lng}            빈 곳을 눌렀다(출발지·도착지 지정)
 *   · label{id}                 누를 수 있는 표시(관광지 이름 태그)를 눌렀다
 *   · dragstart                 사용자가 지도를 끌거나 돌렸다(현재 위치 따라가기 해제)
 *   · camera{lat,lng,level}     카메라 이동이 끝났다(화면이 중심·확대 단계를 알고 있게)
 *
 * 확대 단계: 웹 지도는 level(1=가장 가까움, 숫자가 클수록 넓게), 앱 지도는 zoom(숫자가 클수록 가까움).
 *   zoom = ZOOM_BASE - level 로 바꾼다(웹 level 3 ≈ 앱 zoom 17, 골목 수준).
 */
class NativeMapController(
    private val activity: Activity,
    private val stage: FrameLayout,
    private val webProvider: () -> WebView?,
) {
    companion object {
        private const val TAG = "NativeMap"
        private const val ZOOM_BASE = 20
        /**
         * 카메라 회전각 부호 — 진행 방위 h(정북 기준 시계방향)를 화면 위로 세우려면 카메라 회전각을 -h 로 준다
         * (카메라 θ 면 방위 w 가 화면 w + θ 에 놓이므로 h + (-h) = 0). 실기기 확인(2026-10-19) → -1.
         */
        private const val ROTATION_SIGN = -1.0
        // 라벨(화살표) 회전 규칙(실기기 측정 2026-10-19, 디버그 시험 화면 cam_deg 0·90):
        //   · 라벨 rotateTo(r) 은 지도와 상관없이 **화면 기준·시계 방향**으로 r 만큼 돈다.
        //   · 카메라 회전각 θ 를 주면 지도 위의 방위 w 는 화면에서 w + θ 에 놓인다.
        //   → 화살표가 지도 위 진행 방위 h 를 가리키려면 화면 각도 h + θ 로 돌린다(rotateArrows).
        /** 이만큼(도) 이상 진행 방향이 바뀌었을 때만 지도를 돌린다 — 저속·GPS 흔들림에 지도가 떨지 않게. */
        private const val TURN_THRESHOLD_DEG = 15.0
        /** 현재 위치가 경로에서 이 거리(m) 안이면 경로 구간 방향을 진행 방향으로 본다. */
        private const val ON_ROUTE_M = 30.0
        /** 경로 밖일 때 직전 위치에서 이만큼(m) 이상 움직여야 GPS 이동 방향을 믿는다. */
        private const val MIN_MOVE_M = 4.0
        private const val PREF_HEADING = "map_heading_up"
    }

    private val density = activity.resources.displayMetrics.density
    private fun dp(v: Float) = (v * density).roundToInt()

    /**
     * 지도와 '진행 방향' 버튼을 담는 칸 — 웹 지도 칸 자리에 겹친다.
     * 손가락이 지도 위에서 일정 거리 이상 움직이면 '사람이 지도를 끌었다'(dragstart)를 화면에 알린다.
     * 지도 엔진의 카메라 이동 시작 알림만으로는 부족하다 — 안내 중에는 앱이 0.2초마다 카메라를 옮기고 있어서,
     * 그 사이 사람이 끌어도 엔진이 '앱이 옮긴 것(Unknown)'으로만 알려 따라가기가 풀리지 않았다(실기기 2026-10-19).
     */
    private val box = object : FrameLayout(activity) {
        private val slop = android.view.ViewConfiguration.get(activity).scaledTouchSlop
        private var downX = 0f; private var downY = 0f; private var dragged = false
        override fun dispatchTouchEvent(ev: android.view.MotionEvent): Boolean {
            when (ev.actionMasked) {
                android.view.MotionEvent.ACTION_DOWN -> { downX = ev.x; downY = ev.y; dragged = false }
                android.view.MotionEvent.ACTION_MOVE -> if (!dragged && !onHeadBtn(downX, downY) &&
                    (abs(ev.x - downX) > slop || abs(ev.y - downY) > slop || ev.pointerCount > 1)) {
                    dragged = true
                    emit("dragstart", JSONObject())
                }
            }
            return super.dispatchTouchEvent(ev)
        }
    }.apply { visibility = View.GONE }

    /** 누른 곳이 '진행 방향' 버튼 위인가 — 버튼을 누르다 손이 조금 밀려도 따라가기를 풀지 않게. */
    private fun onHeadBtn(x: Float, y: Float): Boolean =
        x >= headBtn.left && x <= headBtn.right && y >= headBtn.top && y <= headBtn.bottom
    private var mapView: MapView? = null
    private var kmap: KakaoMap? = null
    private var failed = false
    /** 이번 실행에서 앱 지도를 계속 쓸 수 있는가 — 지도 오류가 한 번 나면 false(화면은 웹 지도로 간다). */
    val usable: Boolean get() = !failed
    private val pending = ArrayList<JSONObject>()        // 지도가 준비되기 전에 온 명령

    // 그린 것들 — 웹이 붙인 id 로 찾는다
    private val labels = HashMap<String, Label>()
    // 아래 둘은 지도 엔진 스레드의 라벨 누르기 콜백에서도 읽는다 — 동시 접근에 안전한 자료형
    private val labelIds = java.util.concurrent.ConcurrentHashMap<Label, String>()
    private val clickable: MutableSet<String> = java.util.concurrent.ConcurrentHashMap.newKeySet()
    private val roles = HashMap<String, String>()          // id → me / sim (방향 화살표가 있는 표시)
    private val lines = LinkedHashMap<String, RouteLine>()
    private val linePts = LinkedHashMap<String, List<LatLng>>()   // 진행 방향 계산용 경로 좌표(도보 구간)
    private val bitmapCache = HashMap<String, Bitmap>()

    // 진행 방향 회전 상태
    private var headingUp = activity.getPreferences(Context.MODE_PRIVATE).getBoolean(PREF_HEADING, true)
    private var lastFollow: LatLng? = null
    private var appliedDeg: Double? = null                 // 지금 지도에 적용된 진행 방향(도)
    private var lastLevel = -1
    private val headBtn = TextView(activity)

    init {
        stage.addView(box, FrameLayout.LayoutParams(0, 0))
        headBtn.apply {
            textSize = 15f
            setTypeface(typeface, Typeface.BOLD)
            gravity = Gravity.CENTER
            minHeight = dp(44f)
            setPadding(dp(12f), 0, dp(12f), 0)
            setTextColor(Color.parseColor("#1D4ED8"))
            background = GradientDrawable().apply {
                setColor(Color.WHITE); cornerRadius = dp(12f).toFloat()
                setStroke(dp(1.5f), Color.parseColor("#1D4ED8"))
            }
            elevation = dp(3f).toFloat()
            setOnClickListener { setHeadingUp(!headingUp) }
        }
        val lp = FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.TOP or Gravity.END)
        lp.setMargins(0, dp(10f), dp(10f), 0)
        box.addView(headBtn, lp)
        updateHeadBtn()
    }

    // ───────────────────────── 지도 칸 위치 ─────────────────────────

    /** 지도 칸의 위치·크기(웹 화면 기준 실제 픽셀). WebView 가 stage 를 꽉 채우므로 좌표를 그대로 쓴다. */
    fun setRect(x: Int, y: Int, w: Int, h: Int) {
        if (failed) return
        if (w <= 0 || h <= 0) { box.visibility = View.GONE; return }
        val lp = box.layoutParams as FrameLayout.LayoutParams
        if (lp.leftMargin != x || lp.topMargin != y || lp.width != w || lp.height != h) {
            lp.leftMargin = x; lp.topMargin = y; lp.width = w; lp.height = h
            box.layoutParams = lp
        }
        box.visibility = View.VISIBLE
        box.bringToFront()
        ensureMapView()
    }

    private fun ensureMapView() {
        if (mapView != null || failed) return
        val mv = MapView(activity)
        mapView = mv
        box.addView(mv, 0, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        val start = pending.lastOrNull { it.optString("op") == "init" }
        mv.start(object : MapLifeCycleCallback() {
            override fun onMapDestroy() {}
            override fun onMapError(error: Exception) {
                // 인증 실패·엔진 오류 — 화면에 알려 웹 지도로 되돌아가게 하고, 앱 지도는 걷어낸다
                val code = (error as? MapAuthException)?.errorCode ?: -1
                Log.e(TAG, "map error code=$code", error)
                activity.runOnUiThread { fail(code) }
            }
        }, object : KakaoMapReadyCallback() {
            override fun onMapReady(map: KakaoMap) { activity.runOnUiThread { onReady(map) } }
            override fun getPosition(): LatLng = if (start != null)
                LatLng.from(start.optDouble("lat", 37.3943), start.optDouble("lng", 126.9568))
            else LatLng.from(37.3943, 126.9568)
            override fun getZoomLevel(): Int = ZOOM_BASE - (start?.optInt("level", 5) ?: 5)
        })
    }

    private fun fail(code: Int) {
        if (failed) return
        failed = true
        box.visibility = View.GONE
        try { mapView?.finish() } catch (_: Exception) {}
        box.removeAllViews()
        mapView = null; kmap = null
        pending.clear(); forget()
        emit("error", JSONObject().put("code", code))
    }

    private fun onReady(map: KakaoMap) {
        kmap = map
        map.setOnMapClickListener { _, ll, _, _ ->
            emit("click", JSONObject().put("lat", ll.latitude).put("lng", ll.longitude))
        }
        map.setOnLabelClickListener { _, _, label ->
            val id = labelIds[label]
            if (id != null && id in clickable) { emit("label", JSONObject().put("id", id)); true } else false
        }
        map.setOnCameraMoveStartListener { _, g ->
            if (BuildConfig.DEBUG) Log.d(TAG, "MOVE_START gesture=$g")
            // 사람이 끌거나 돌린 경우만 — 앱이 옮긴 카메라(Unknown)·두 번 눌러 확대는 따라가기를 풀지 않는다
            if (g == GestureType.Pan || g == GestureType.Rotate || g == GestureType.RotateZoom ||
                g == GestureType.LongTapAndDrag) emit("dragstart", JSONObject())
        }
        map.setOnCameraMoveEndListener { _, pos, _ ->
            if (BuildConfig.DEBUG) Log.d(TAG, "CAMERA_END rot=${pos.rotationAngle} zoom=${pos.zoomLevel} tilt=${pos.tiltAngle}")
            activity.runOnUiThread {
                rotateArrows(pos.rotationAngle)     // 손으로 돌렸을 때도 화살표가 지도 위 진행 방향을 가리키게
                val lv = ZOOM_BASE - pos.zoomLevel
                emit("camera", JSONObject().put("lat", pos.position.latitude).put("lng", pos.position.longitude).put("level", lv))
                lastLevel = lv
            }
        }
        val q = ArrayList(pending); pending.clear()
        q.forEach { run(it) }
        emit("ready", JSONObject())
    }

    // ───────────────────────── 명령 ─────────────────────────

    /** 화면이 보낸 명령 묶음(JSON 배열). 지도가 아직 없으면 쌓아 두었다가 준비되면 차례로 실행한다. */
    fun apply(json: String) {
        if (failed) return
        val arr = try { JSONArray(json) } catch (_: Exception) { return }
        for (i in 0 until arr.length()) {
            val c = arr.optJSONObject(i) ?: continue
            if (c.optString("op") == "init") {
                // 화면을 새로 열었다 — 쌓인 것과 그려진 것을 모두 비우고 처음부터
                pending.clear(); clearAll()
                if (kmap == null) { pending.add(c); continue }
            }
            if (kmap == null) pending.add(c) else run(c)
        }
    }

    private fun run(c: JSONObject) {
        val map = kmap ?: return
        try {
            when (c.optString("op")) {
                "init" -> map.moveCamera(CameraUpdateFactory.newCenterPosition(ll(c), ZOOM_BASE - c.optInt("level", 5)))
                "label" -> addLabel(map, c)
                "move" -> {
                    val id = c.optString("id")
                    labels[id]?.moveTo(ll(c))
                }
                "line" -> addLine(map, c)
                "remove" -> remove(c.optString("id"))
                "center" -> { if (BuildConfig.DEBUG) Log.d(TAG, "CENTER"); map.moveCamera(CameraUpdateFactory.newCenterPosition(ll(c)), CameraAnimation.from(300)) }
                "follow" -> follow(map, ll(c))
                "level" -> {
                    val z = ZOOM_BASE - c.optInt("level", 5)
                    if (c.has("lat")) map.moveCamera(CameraUpdateFactory.newCenterPosition(ll(c), z), CameraAnimation.from(250))
                    else map.moveCamera(CameraUpdateFactory.zoomTo(z), CameraAnimation.from(250))
                }
                "bounds" -> { if (BuildConfig.DEBUG) Log.d(TAG, "BOUNDS"); fitBounds(map, c.optJSONArray("pts")) }
            }
        } catch (e: Exception) {
            Log.w(TAG, "cmd failed: ${c.optString("op")}", e)
        }
    }

    private fun ll(c: JSONObject) = LatLng.from(c.optDouble("lat"), c.optDouble("lng"))

    private fun clearAll() {
        labels.values.forEach { try { it.remove() } catch (_: Exception) {} }
        labels.clear(); labelIds.clear(); clickable.clear(); roles.clear()
        lines.values.forEach { try { it.remove() } catch (_: Exception) {} }
        lines.clear(); linePts.clear()
        lastFollow = null; appliedDeg = null
    }

    private fun remove(id: String) {
        labels.remove(id)?.let { l -> labelIds.remove(l); try { l.remove() } catch (_: Exception) {} }
        clickable.remove(id); roles.remove(id)
        lines.remove(id)?.let { try { it.remove() } catch (_: Exception) {} }
        if (linePts.remove(id) != null && linePts.isEmpty()) {
            // 경로를 다 지웠다(안내 종료·경로 지우기) — 지도를 다시 북쪽 위로, 화살표도 위로
            appliedDeg = null; lastFollow = null
            rotateTo(0.0, 400)
        }
    }

    // ───────────────────────── 표시(라벨) ─────────────────────────

    private fun addLabel(map: KakaoMap, c: JSONObject) {
        val id = c.optString("id")
        remove(id)
        val kind = c.optString("kind", "dot")
        val text = c.optString("text", "")
        val color = c.optString("color", "#2563EB")
        val key = "$kind|$text|$color"
        if (bitmapCache.size > 300) bitmapCache.clear()      // 관광지 이름마다 그림이 생긴다 — 무한히 쌓이지 않게
        val bmp = bitmapCache.getOrPut(key) { drawBitmap(kind, text, color) }
        val anchorY = when (kind) { "pin" -> 1.0f; "tag", "tag-multi" -> 1.15f; else -> 0.5f }
        val style = LabelStyle.from(bmp).setAnchorPoint(0.5f, anchorY)
        val rank = when (kind) { "me", "sim" -> 100L; "pin" -> 90L; "origin" -> 80L; "tag", "tag-multi" -> 50L; "cw" -> 20L; else -> 10L }
        val opts = LabelOptions.from(id, ll(c)).setStyles(LabelStyles.from(style)).setRank(rank)
            .setClickable(c.optBoolean("clickable", false))
        val label = map.labelManager?.layer?.addLabel(opts) ?: return
        labels[id] = label; labelIds[label] = id
        if (c.optBoolean("clickable", false)) clickable.add(id)
        if (kind == "me" || kind == "sim") {
            roles[id] = kind
            rotateArrows()
        }
    }

    /** 표시 그림을 그린다 — 웹 지도의 표시와 같은 색·모양을 따른다(CSS .poi-tag, .cw-mark 등). */
    private fun drawBitmap(kind: String, text: String, color: String): Bitmap {
        val p = Paint(Paint.ANTI_ALIAS_FLAG)
        return when (kind) {
            "me", "sim" -> {
                // 현재 위치(모의 주행은 주황) — 흰 테두리 원 + 진행 방향 화살촉(위쪽이 진행 방향)
                val s = dp(30f); val b = Bitmap.createBitmap(s, s, Bitmap.Config.ARGB_8888); val cv = Canvas(b)
                val fill = if (kind == "sim") Color.parseColor("#F97316") else Color.parseColor("#2563EB")
                p.color = Color.WHITE; cv.drawCircle(s / 2f, s / 2f, s / 2f - dp(1f), p)
                p.color = fill; cv.drawCircle(s / 2f, s / 2f, s / 2f - dp(4f), p)
                p.color = Color.WHITE
                val path = Path().apply {
                    moveTo(s / 2f, dp(7f).toFloat()); lineTo(s - dp(9f).toFloat(), s - dp(9f).toFloat())
                    lineTo(s / 2f, s - dp(12f).toFloat()); lineTo(dp(9f).toFloat(), s - dp(9f).toFloat()); close()
                }
                cv.drawPath(path, p); b
            }
            "origin", "dot" -> {
                val s = dp(20f); val b = Bitmap.createBitmap(s, s, Bitmap.Config.ARGB_8888); val cv = Canvas(b)
                p.color = Color.WHITE; cv.drawCircle(s / 2f, s / 2f, s / 2f - dp(1f), p)
                p.color = safeColor(color); cv.drawCircle(s / 2f, s / 2f, s / 2f - dp(3.5f), p); b
            }
            "pin" -> {
                // 안내 지점(다음 안내 위치) — 빨간 핀, 아래 끝이 지점
                val w = dp(26f); val h = dp(36f); val b = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888); val cv = Canvas(b)
                val r = w / 2f - dp(1f)
                val path = Path().apply {
                    addCircle(w / 2f, r + dp(1f), r, Path.Direction.CW)
                    moveTo(w / 2f - r * 0.7f, r * 1.6f); lineTo(w / 2f, h.toFloat()); lineTo(w / 2f + r * 0.7f, r * 1.6f); close()
                }
                p.color = Color.parseColor("#DC2626"); cv.drawPath(path, p)
                p.color = Color.WHITE; cv.drawCircle(w / 2f, r + dp(1f), r * 0.38f, p); b
            }
            "cw", "cw-near" -> {
                // 횡단보도 — 흰·검 줄무늬 사각형. 경로 옆(near)은 작고 옅게
                val near = kind == "cw-near"
                val s = dp(if (near) 10f else 18f); val b = Bitmap.createBitmap(s, s, Bitmap.Config.ARGB_8888); val cv = Canvas(b)
                val stripe = dp(if (near) 2f else 3f).coerceAtLeast(1)
                p.alpha = 255
                p.color = Color.WHITE; cv.drawRect(0f, 0f, s.toFloat(), s.toFloat(), p)
                p.color = Color.parseColor("#111827")
                var x = stripe; while (x < s) { cv.drawRect(x.toFloat(), 0f, (x + stripe).toFloat(), s.toFloat(), p); x += stripe * 2 }
                p.style = Paint.Style.STROKE; p.strokeWidth = dp(if (near) 1.5f else 2f).toFloat()
                cv.drawRect(p.strokeWidth / 2, p.strokeWidth / 2, s - p.strokeWidth / 2, s - p.strokeWidth / 2, p)
                if (near) { val faded = Bitmap.createBitmap(s, s, Bitmap.Config.ARGB_8888); val fp = Paint().apply { alpha = 140 }
                    Canvas(faded).drawBitmap(b, 0f, 0f, fp); faded } else b
            }
            else -> {
                // 관광지 이름 태그 — 둥근 알약 모양, 묶음 태그(tag-multi)는 파란 바탕
                val multi = kind == "tag-multi"
                val primary = Color.parseColor("#1D4ED8")
                p.textSize = dp(13f).toFloat(); p.typeface = Typeface.DEFAULT_BOLD
                var t = text.ifBlank { "이름 없음" }
                val maxW = dp(150f)
                while (p.measureText(t) > maxW - dp(20f) && t.length > 2) t = t.dropLast(2) + "…"
                val w = (p.measureText(t) + dp(20f)).roundToInt(); val h = dp(26f)
                val b = Bitmap.createBitmap(w + dp(4f), h + dp(4f), Bitmap.Config.ARGB_8888); val cv = Canvas(b)
                val rect = RectF(dp(2f).toFloat(), dp(2f).toFloat(), w + dp(2f).toFloat(), h + dp(2f).toFloat())
                val bg = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = if (multi) primary else Color.WHITE }
                cv.drawRoundRect(rect, h / 2f, h / 2f, bg)
                val st = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = dp(1.5f).toFloat(); this.color = primary }
                cv.drawRoundRect(rect, h / 2f, h / 2f, st)
                p.color = if (multi) Color.WHITE else primary
                val fm = p.fontMetrics
                cv.drawText(t, rect.left + dp(10f), rect.centerY() - (fm.ascent + fm.descent) / 2, p); b
            }
        }
    }

    private fun safeColor(s: String) = try { Color.parseColor(s) } catch (_: Exception) { Color.parseColor("#2563EB") }

    // ───────────────────────── 경로선 ─────────────────────────

    private fun addLine(map: KakaoMap, c: JSONObject) {
        val id = c.optString("id")
        remove(id)
        val arr = c.optJSONArray("pts") ?: return
        val pts = ArrayList<LatLng>(arr.length())
        for (i in 0 until arr.length()) {
            val p = arr.optJSONArray(i) ?: continue
            pts.add(LatLng.from(p.optDouble(0), p.optDouble(1)))
        }
        if (pts.size < 2) return
        val base = safeColor(c.optString("color", "#2563EB"))
        val alpha = (c.optDouble("opacity", 0.9).coerceIn(0.1, 1.0) * 255).roundToInt()
        val color = Color.argb(alpha, Color.red(base), Color.green(base), Color.blue(base))
        val width = dp(c.optDouble("width", 6.0).toFloat())
        // 흰 테두리를 둘러 지도 바탕(도로색)과 구분한다. 점선(대중교통 구간)은 이번 범위에서 실선으로 그린다.
        val style = RouteLineStyle.from(width.toFloat(), color, dp(1.5f).toFloat(), Color.WHITE)
        val line = map.routeLineManager?.layer?.addRouteLine(
            RouteLineOptions.from(RouteLineSegment.from(pts, style))
        ) ?: return
        lines[id] = line
        if (!c.optBoolean("dash", false)) linePts[id] = pts      // 진행 방향 계산은 도보(실선) 구간만
    }

    // ───────────────────────── 카메라 ─────────────────────────

    private fun fitBounds(map: KakaoMap, arr: JSONArray?) {
        if (arr == null || arr.length() == 0) return
        val pts = Array(arr.length()) { i -> val p = arr.getJSONArray(i); LatLng.from(p.getDouble(0), p.getDouble(1)) }
        // 전체 경로 보기는 북쪽 위로 — 돌아간 지도로 범위를 맞추면 어디가 어디인지 알기 어렵다
        appliedDeg = null
        rotateTo(0.0, 0)
        if (pts.size == 1) map.moveCamera(CameraUpdateFactory.newCenterPosition(pts[0]), CameraAnimation.from(300))
        else map.moveCamera(CameraUpdateFactory.fitMapPoints(pts, dp(48f)), CameraAnimation.from(300))
    }

    private fun rotateTo(deg: Double, ms: Int) {
        val map = kmap ?: return
        val cur = map.cameraPosition ?: return
        val pos = CameraPosition.from(CameraPosition.Builder(cur).setRotationAngle(camRad(deg)))
        if (ms > 0) map.moveCamera(CameraUpdateFactory.newCameraPosition(pos), CameraAnimation.from(ms))
        else map.moveCamera(CameraUpdateFactory.newCameraPosition(pos))
        rotateArrows(camRad(deg))
    }

    /**
     * 현재 위치·모의 주행 화살표가 지도 위 진행 방위(appliedDeg)를 가리키게 한다. 방위를 아직 모르면 북쪽.
     * 라벨은 화면 기준으로 돌기 때문에, 화면에서의 각도 = 방위 + 카메라 회전각 으로 돌린다(위 규칙 주석 참조).
     * camRot: 지금(또는 곧 적용될) 카메라 회전각(라디안). 생략하면 지도의 현재 회전각.
     */
    private fun rotateArrows(camRot: Double? = kmap?.cameraPosition?.rotationAngle) {
        val full = 2 * PI
        val screen = Math.toRadians(appliedDeg ?: 0.0) + (camRot ?: 0.0)
        val rad = (((screen % full) + full) % full).toFloat()   // 0 ≤ 값 < 2π
        roles.keys.forEach { id -> labels[id]?.rotateTo(rad) }
    }

    /**
     * 안내 중 현재 위치 따라가기 — 화면이 위치가 바뀔 때마다 부른다.
     * 진행 방향: 경로 위(30m 안)면 가장 가까운 경로 구간의 방향, 경로 밖이면 직전 위치에서 움직인 방향.
     * 방향이 15° 이상 바뀔 때만 부드럽게 돌린다. '북쪽 위' 모드에서는 돌리지 않고 가운데만 맞춘다.
     */
    private fun follow(map: KakaoMap, pos: LatLng) {
        var deg: Double? = routeBearing(pos)
        val prev = lastFollow
        if (deg == null && prev != null && distM(prev, pos) >= MIN_MOVE_M) deg = bearing(prev, pos)
        if (deg != null) lastFollow = pos else if (prev == null) lastFollow = pos
        if (deg != null) {
            val cur = appliedDeg
            if (cur == null || angleDiff(cur, deg) >= TURN_THRESHOLD_DEG) appliedDeg = deg
        }
        val target = if (headingUp) (appliedDeg ?: 0.0) else 0.0
        val cam = map.cameraPosition
        if (cam == null) { map.moveCamera(CameraUpdateFactory.newCenterPosition(pos), CameraAnimation.from(400)); return }
        val next = CameraPosition.from(CameraPosition.Builder(cam).setPosition(pos)
            .setRotationAngle(camRad(target)))
        map.moveCamera(CameraUpdateFactory.newCameraPosition(next), CameraAnimation.from(500))
        rotateArrows(camRad(target))
        if (BuildConfig.DEBUG) Log.d(TAG, "FOLLOW pos=${pos.latitude},${pos.longitude} routeDeg=${routeBearing(pos)} deg=$deg applied=$appliedDeg " +
            "target=$target camRotBefore=${cam.rotationAngle} sent=${next.rotationAngle} headingUp=$headingUp")
    }

    /** 현재 위치에서 가장 가까운 경로 구간의 방위(도). 경로에서 ON_ROUTE_M 보다 멀면 null. */
    private fun routeBearing(pos: LatLng): Double? {
        var best = Double.MAX_VALUE; var bestDeg: Double? = null
        for (pts in linePts.values) {
            for (i in 0 until pts.size - 1) {
                val a = pts[i]; val b = pts[i + 1]
                if (distM(a, b) < 0.5) continue
                val d = distToSegM(pos, a, b)
                if (d < best) { best = d; bestDeg = bearing(a, b) }
            }
        }
        return if (best <= ON_ROUTE_M) bestDeg else null
    }

    private fun setHeadingUp(on: Boolean) {
        headingUp = on
        activity.getPreferences(Context.MODE_PRIVATE).edit().putBoolean(PREF_HEADING, on).apply()
        updateHeadBtn()
        if (!on) rotateTo(0.0, 400) else appliedDeg?.let { rotateTo(it, 400) }
    }

    private fun updateHeadBtn() {
        headBtn.text = if (headingUp) "▲ 진행 방향" else "N 북쪽 위"
        headBtn.contentDescription = if (headingUp)
            "지도 방향: 진행 방향이 위. 누르면 북쪽이 위로 바뀝니다"
        else "지도 방향: 북쪽이 위. 누르면 진행 방향이 위로 바뀝니다"
    }

    // ───────────────────────── 생명주기 ─────────────────────────

    fun resume() { try { mapView?.resume() } catch (_: Exception) {} }
    fun pause() { try { mapView?.pause() } catch (_: Exception) {} }
    fun destroy() {
        try { mapView?.finish() } catch (_: Exception) {}
        mapView = null; kmap = null
        pending.clear(); forget()
        (box.parent as? ViewGroup)?.removeView(box)
    }

    /** 화면을 새로 만들었을 때(화면 프로세스 재시작) — 그려진 것을 비우고 화면이 다시 보낼 명령을 기다린다. */
    fun resetForNewPage() {
        pending.clear()
        if (kmap != null) clearAll() else forget()
        box.visibility = View.GONE
    }

    /** 지도 없이 기록만 비운다(지도가 이미 없을 때). 그림 캐시도 놓아 준다. */
    private fun forget() {
        labels.clear(); labelIds.clear(); clickable.clear(); roles.clear()
        lines.clear(); linePts.clear(); bitmapCache.clear()
        lastFollow = null; appliedDeg = null
    }

    /** 화면에 사건을 알린다. 지도 엔진 콜백은 엔진 스레드에서 올 수 있어 항상 화면 스레드로 넘겨 실행한다. */
    private fun emit(type: String, data: JSONObject) {
        val js = "(function(){try{if(window.__NMAP&&__NMAP.emit)__NMAP.emit(${JSONObject.quote(type)},$data);}catch(e){}})()"
        activity.runOnUiThread { webProvider()?.evaluateJavascript(js, null) }
    }

    // ───────────────────────── 거리·방위 ─────────────────────────

    private fun distM(a: LatLng, b: LatLng): Double {
        val r = 6371008.8; val t = PI / 180
        val dLat = (b.latitude - a.latitude) * t; val dLng = (b.longitude - a.longitude) * t
        val x = sin(dLat / 2) * sin(dLat / 2) + cos(a.latitude * t) * cos(b.latitude * t) * sin(dLng / 2) * sin(dLng / 2)
        return 2 * r * atan2(sqrt(x), sqrt(1 - x))
    }

    /** 점 p 에서 선분 ab 까지 거리(m) — 짧은 구간이라 평면 근사. */
    private fun distToSegM(p: LatLng, a: LatLng, b: LatLng): Double {
        val k = cos(Math.toRadians(p.latitude))
        val ax = (a.longitude - p.longitude) * k; val ay = a.latitude - p.latitude
        val bx = (b.longitude - p.longitude) * k; val by = b.latitude - p.latitude
        val dx = bx - ax; val dy = by - ay
        val len2 = dx * dx + dy * dy
        val tt = if (len2 == 0.0) 0.0 else (-(ax * dx + ay * dy) / len2).coerceIn(0.0, 1.0)
        val cx = ax + tt * dx; val cy = ay + tt * dy
        return sqrt(cx * cx + cy * cy) * 111_195.0
    }

    /** a → b 방위(도, 정북 0 · 시계방향). */
    private fun bearing(a: LatLng, b: LatLng): Double {
        val t = PI / 180
        val y = sin((b.longitude - a.longitude) * t) * cos(b.latitude * t)
        val x = cos(a.latitude * t) * sin(b.latitude * t) - sin(a.latitude * t) * cos(b.latitude * t) * cos((b.longitude - a.longitude) * t)
        return (Math.toDegrees(atan2(y, x)) + 360) % 360
    }

    /**
     * 카메라 회전각(라디안, 0 ≤ 값 < 2π). 지도 엔진은 음수 회전각을 받지 않고 0(북쪽 위)으로 처리한다
     * (실기기 2026-10-19: -2.44 를 보내니 카메라가 0 으로 끝났다) — 같은 각도를 양수 범위로 바꿔 보낸다.
     */
    private fun camRad(deg: Double): Double {
        val r = ROTATION_SIGN * Math.toRadians(deg)
        val full = 2 * PI
        return ((r % full) + full) % full
    }

    private fun angleDiff(a: Double, b: Double): Double { val d = abs(a - b) % 360; return if (d > 180) 360 - d else d }
}
