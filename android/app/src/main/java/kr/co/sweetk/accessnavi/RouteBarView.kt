package kr.co.sweetk.accessnavi

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.view.View
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.max
import kotlin.math.min

/**
 * 전체 경로 진행 바 — 앱 지도 위 오른쪽에 세로로 얹는다 (앱 2.2.0).
 *
 * 웹 화면이 만든 값을 그대로 그린다(계산은 웹 updateGuideHud 가 한다).
 *   mapCmd {op:"progress", pct, profile, segs?:[{a,b,kind}], marks?:[{pct,icon}]}
 *     · pct     지나온 비율 0~100 — 아래가 출발, 위가 도착
 *     · profile electric | manual | visual — 내 위치 표식 색·모양(안내 중 바꿀 수 없는 경로 기준이 무엇인지 보이게)
 *     · segs    승차 구간(지하철·버스) 띠 — 경로가 바뀔 때만 온다(없으면 종전 것 유지)
 *     · marks   주의 지점 표식(그림 문자) — 경로가 바뀔 때만 온다
 *   mapCmd {op:"progress_off"}  바를 숨긴다(경로를 지웠을 때)
 *
 * 웹 지도에서는 같은 것을 웹 요소(#routeBar)로 띄운다. 앱 지도는 웹 화면 위에 겹쳐 그려져 웹 요소를
 * 지도 위에 올릴 수 없어서(2026-10-28 사용자 결정 — 지도 옆 칸으로 두면 지도가 좁아진다) 앱이 직접 그린다.
 * 모양은 웹 바(v2.21.1)와 같다: 배경 없이 선만, 지나온 곳 회색·남은 곳 파랑, 표식 12sp, 내 위치 24dp.
 */
class RouteBarView(context: Context) : View(context) {
    private val d = context.resources.displayMetrics.density
    private val sp = context.resources.displayMetrics.scaledDensity

    private var pct = 0f
    private var profile = "electric"
    private val segs = ArrayList<Triple<Float, Float, String>>()      // a%, b%, kind
    private val marks = ArrayList<Pair<Float, String>>()             // pct, icon

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER; textSize = 12 * sp }
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND }
    private val rect = RectF()

    companion object {
        private const val TRACK_DP = 8f
        private const val ME_DP = 24f
        private val COLOR_TRACK = Color.parseColor("#2563EB")      // 남은 곳
        private val COLOR_DONE = Color.parseColor("#CBD5E1")       // 지나온 곳
        private val COLOR_SUBWAY = Color.parseColor("#A78BFA")
        private val COLOR_BUS = Color.parseColor("#FDBA74")
        private val COLOR_ELECTRIC = Color.parseColor("#0F766E")
        private val COLOR_MANUAL = Color.parseColor("#1D4ED8")
        private val COLOR_VISUAL = Color.parseColor("#7C3AED")
        private val COLOR_VISUAL_BG = Color.parseColor("#F5F3FF")
    }

    /** 화면이 보낸 값으로 갱신한다. segs·marks 는 있을 때만 바꾼다. */
    fun update(c: JSONObject) {
        pct = c.optDouble("pct", pct.toDouble()).toFloat().coerceIn(0f, 100f)
        profile = c.optString("profile", profile)
        c.optJSONArray("segs")?.let { a ->
            segs.clear()
            for (i in 0 until a.length()) {
                val o = a.optJSONObject(i) ?: continue
                segs.add(Triple(o.optDouble("a", 0.0).toFloat(), o.optDouble("b", 0.0).toFloat(), o.optString("kind", "subway")))
            }
        }
        c.optJSONArray("marks")?.let { a ->
            marks.clear()
            for (i in 0 until a.length()) {
                val o = a.optJSONObject(i) ?: continue
                marks.add(o.optDouble("pct", 0.0).toFloat() to o.optString("icon", ""))
            }
        }
        invalidate()
    }

    private fun yOf(p: Float, top: Float, bottom: Float) = bottom - (bottom - top) * (p.coerceIn(0f, 100f) / 100f)

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat(); val h = height.toFloat()
        if (w <= 0 || h <= 0) return
        val cx = w / 2f
        val tw = TRACK_DP * d
        val pad = ME_DP * d / 2 + 2 * d                              // 위·아래 여유 — 내 위치 원이 잘리지 않게
        val top = pad; val bottom = h - pad
        // 남은 곳(파랑) 전체
        paint.style = Paint.Style.FILL; paint.color = COLOR_TRACK
        rect.set(cx - tw / 2, top, cx + tw / 2, bottom)
        canvas.drawRoundRect(rect, tw / 2, tw / 2, paint)
        // 승차 구간 띠
        segs.forEach { (a, b, kind) ->
            paint.color = if (kind == "bus") COLOR_BUS else COLOR_SUBWAY
            val y1 = yOf(b, top, bottom); val y2 = yOf(a, top, bottom)
            if (y2 > y1) { rect.set(cx - tw / 2, y1, cx + tw / 2, y2); canvas.drawRoundRect(rect, tw / 2, tw / 2, paint) }
        }
        // 지나온 곳(회색) — 아래부터 pct 까지
        val yMe = yOf(pct, top, bottom)
        paint.color = COLOR_DONE
        rect.set(cx - tw / 2, yMe, cx + tw / 2, bottom)
        canvas.drawRoundRect(rect, tw / 2, tw / 2, paint)
        // 주의 지점 표식(그림 문자) — 글자 가운데를 그 지점에
        marks.forEach { (p, icon) ->
            if (icon.isEmpty()) return@forEach
            val y = yOf(p, top, bottom)
            val fm = text.fontMetrics
            canvas.drawText(icon, cx, y - (fm.ascent + fm.descent) / 2, text)
        }
        // 내 위치 — 휠체어 아이콘(전동: 청록 바탕 흰 그림 / 수동: 흰 바탕 파랑 / 시각: 연보라 바탕 보라)
        val r = ME_DP * d / 2
        val (bg, fg, border) = when (profile) {
            "manual" -> Triple(Color.WHITE, COLOR_MANUAL, COLOR_MANUAL)
            "visual" -> Triple(COLOR_VISUAL_BG, COLOR_VISUAL, COLOR_VISUAL)
            else -> Triple(COLOR_ELECTRIC, Color.WHITE, COLOR_ELECTRIC)
        }
        paint.color = Color.argb(70, 15, 23, 42)                  // 옅은 그림자
        canvas.drawCircle(cx, yMe + 1.5f * d, r, paint)
        paint.color = bg; canvas.drawCircle(cx, yMe, r, paint)
        stroke.color = border; stroke.strokeWidth = 2 * d
        canvas.drawCircle(cx, yMe, r - d, stroke)
        drawIcon(canvas, cx, yMe, r * 0.68f, fg, profile)
    }

    /** 원 안의 그림 — 전동/수동은 휠체어(머리·몸·큰 바퀴), 시각은 눈. 16px 급이라 선 몇 개로만 그린다. */
    private fun drawIcon(canvas: Canvas, cx: Float, cy: Float, s: Float, color: Int, kind: String) {
        stroke.color = color; stroke.strokeWidth = max(1.5f * d, s * 0.16f)
        paint.color = color; paint.style = Paint.Style.FILL
        if (kind == "visual") {
            val p = Path()
            p.moveTo(cx - s, cy); p.quadTo(cx, cy - s * 0.9f, cx + s, cy); p.quadTo(cx, cy + s * 0.9f, cx - s, cy)
            canvas.drawPath(p, stroke)
            canvas.drawCircle(cx, cy, s * 0.3f, paint)
            return
        }
        // 머리
        canvas.drawCircle(cx - s * 0.15f, cy - s * 0.72f, s * 0.2f, paint)
        // 몸통·무릎·발
        val body = Path()
        body.moveTo(cx - s * 0.15f, cy - s * 0.45f); body.lineTo(cx - s * 0.1f, cy + s * 0.1f)
        body.lineTo(cx + s * 0.4f, cy + s * 0.1f); body.lineTo(cx + s * 0.65f, cy + s * 0.6f)
        canvas.drawPath(body, stroke)
        // 큰 바퀴(호)
        rect.set(cx - s * 0.75f, cy - s * 0.1f, cx + s * 0.25f, cy + s * 0.9f)
        canvas.drawArc(rect, 110f, 300f, false, stroke)
        if (kind != "manual") {
            // 전동: 바퀴 두 개 작게 — 큰 바퀴 호 아래에 점 하나 더
            canvas.drawCircle(cx + s * 0.6f, cy + s * 0.85f, s * 0.16f, paint)
        }
    }
}
