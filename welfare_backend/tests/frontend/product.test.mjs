/**
 * 제품 분리 런타임 검증 (jsdom) — v2.0.0
 *
 *   정책상담(기본 주소)
 *     1) 지도·위치 감시를 하지 않는다 · 화면 사이 탭이 없다 · 상담원이 물을 때만 위치를 한 번 보낸다(v2.19.0)
 *     2) 길안내 요청 결과(handoff_navi)는 넘기기 버튼으로 뜨고, 누르면 목적지·프로필만 실어 연다
 *     3) 넘어가 있는 동안 마이크·음성을 멈추고, 돌아오면 잇는다
 *   이동경로 안내(/navi)
 *     4) 시작 화면이 길안내 전용이다
 *     5) 주소로 넘겨받은 목적지로 경로를 찾는다 · 주소는 지운다
 *     6) 떠 있는 화면에 새 목적지가 들어와도 경로를 찾는다
 *     7) 통화 중에는 안내 음성을 내지 않는다
 *   하위 경로 배포 (v2.1.0)
 *     8) /iitp/accessistantai/… 에서 열어도 서버 호출·WebSocket·아이콘·넘기기 주소가 그 경로 아래로 간다 · 루트 배포는 종전과 같다
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

const HTML = readFileSync(new URL("../../static/accessistant.html", import.meta.url), "utf8");
const results = [];
function check(name, fn) {
  try { fn(); results.push(["PASS", name]); } catch (e) { results.push(["FAIL", name + " — " + (e && e.message)]); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CONFIG = { features: { route: true, tour: true }, kakao_js_key: "k", region: { name: "안양시", bbox: [37.33, 126.87, 37.45, 127.0] } };
const P = (k) => [37.3900 + 0.0010 * k, 126.9500];
const ROUTE = { status: "success", route_id: "r_p", ui_action: { action: "show_route", route: { route_id: "r_p", destination: { type: "building", poi_id: null, lat: P(8)[0], lng: P(8)[1] }, routes: [{
  summary: { total_distance_m: 900, duration_sec: 800, max_slope_deg: 2, stairs_cnt: 0, crossing_cnt: 0 },
  geometry: [P(0), P(8)],
  steps: [
    { idx: 0, maneuver: "depart", instruction: "중앙로를 따라 900m 앞으로 이동합니다.", distance_m: 900, coord: P(0), link_type: "sidewalk", link_name: "중앙로", warnings: [] },
    { idx: 1, maneuver: "arrive", instruction: "목적지에 도착했습니다.", distance_m: 0, coord: P(8), warnings: [] },
  ] }] } } };

function boot(url, opts) {
  opts = opts || {};
  const dom = new JSDOM(HTML, { runScripts: "dangerously", pretendToBeVisual: true, url,
    beforeParse(w) {
      if (opts.ua) Object.defineProperty(w.navigator, "userAgent", { value: opts.ua, configurable: true });
      if (opts.preset) opts.preset(w);
      const sockets = [];
      class FakeWS {
        constructor(u) { this.url = u; this.readyState = 0; this.sent = []; sockets.push(this); }
        send(d) { this.sent.push(d); }
        close() { if (this.readyState === 3) return; this.readyState = 3; setTimeout(() => this.onclose && this.onclose({ code: 1000, wasClean: true }), 0); }
        _open() { this.readyState = 1; return this.onopen && this.onopen(); }
        _msg(o) { return this.onmessage && this.onmessage({ data: JSON.stringify(o) }); }
      }
      FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;
      w.WebSocket = FakeWS; w.__sockets = sockets;
      w.fetch = async (u0) => { const u = String(u0);
        (w.__fetchLog = w.__fetchLog || []).push(u);
        if (u.includes("/app/latest.json")) { const L = w.__latest; return L ? { ok: true, json: async () => L } : { ok: false, json: async () => ({}) }; }
        const body = u.includes("/api/v1/config") ? CONFIG : u.includes("plan_accessible_route") ? ROUTE : { status: "success", results: [] };
        return { ok: true, json: async () => body }; };
      w.navigator.geolocation = { watchPosition: (ok) => { w.__watch = ok; return 1; }, clearWatch: () => {},
        getCurrentPosition: (ok, err, o) => { (w.__once = w.__once || []).push({ ok, err, o }); } };
      w.navigator.mediaDevices = { getUserMedia: async () => { throw new Error("no mic"); } };
      w.SpeechSynthesisUtterance = function (t) { this.text = t; };
      w.speechSynthesis = { cancel() {}, speak(u) { (w.__spoken = w.__spoken || []).push(u.text); if (u.onend) u.onend(); } };
      w.HTMLMediaElement.prototype.play = async () => {}; w.HTMLMediaElement.prototype.pause = () => {};
      const mk = () => new Proxy(function () {}, { get: (t, k) => (k === "then" ? undefined : mk()), apply: () => mk(), construct: () => mk() });
      w.kakao = { maps: new Proxy({ load: (cb) => cb() }, { get: (t, k) => (k in t ? t[k] : mk()) }) };
    } });
  const { window } = dom;
  const origAppend = window.document.head.appendChild.bind(window.document.head);
  window.document.head.appendChild = (el) => { const r = origAppend(el);
    if (el.tagName === "SCRIPT" && String(el.src).includes("dapi.kakao.com") && el.onload) setTimeout(() => el.onload(), 0); return r; };
  return window;
}
const ready = async (w, ms) => { await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(ms || 200); };
const setVisible = (w, v) => { Object.defineProperty(w.document, "visibilityState", { value: v, configurable: true }); w.document.dispatchEvent(new w.Event("visibilitychange")); };

// ───────── 정책상담 ─────────
{
  const w = boot("https://example.test/static/accessistant.html");
  const $ = (id) => w.document.getElementById(id);
  await ready(w);
  const opened = [];
  w.__HANDOFF.open = (h) => { opened.push(h); if (w.__CHAT && w.__CHAT.hold) w.__CHAT.hold(true, "handoff"); };
  check("정책상담: 제품 표시 · 지도·위치를 쓰지 않는다", () => {
    assert.equal(w.__PRODUCT, "policy");
    assert.equal(w.document.documentElement.getAttribute("data-product"), "policy");
    assert.equal(w.__watch, undefined, "위치 감시를 시작했다");
    assert.ok(!(w.__fetchLog || []).some((u) => u.includes("find_bf_tour_spots")));
    assert.equal($("tripResumeModal"), null);
  });
  check("정책상담: 시작 화면 이동경로 버튼은 '앱 열기'", () => {
    assert.equal($("modeNaviBtn").querySelector(".lab").textContent, "이동경로 안내 (앱 열기)");
    assert.equal(w.document.querySelector("#view-mode h1").textContent, "AI 정책상담원에게 물어보세요");
  });
  $("modeNaviBtn").click(); await sleep(30);
  check("정책상담: 이동경로 버튼 → 세션을 열지 않고 넘긴다", () => { assert.equal(opened.length, 1); assert.equal(opened[0], null); assert.equal(w.__sockets.length, 0); });

  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  const s0 = w.__sockets[0]; await s0._open(); await sleep(20);
  check("정책상담: 세션 주소에 navi 모드가 없다", () => assert.doesNotMatch(s0.url, /mode=navi/));
  {
    const locs = () => s0.sent.map((d) => { try { return JSON.parse(d); } catch (e) { return {}; } }).filter((m) => m.type === "location");
    const unav = () => s0.sent.filter((d) => { try { return JSON.parse(d).type === "location_unavailable"; } catch (e) { return false; } }).length;
    const st0 = ($("naviStatus") || {}).textContent;
    check("정책상담: 상담 시작 누름에서 위치 권한만 미리 묻는다 · 결과는 보내지 않는다 (v2.19.1)", () => {
      assert.equal((w.__once || []).length, 1); assert.equal(w.__watch, undefined); assert.equal(locs().length, 0);
      w.__once[0].ok({ coords: { latitude: 37.1, longitude: 127.1, accuracy: 5 } });
      assert.equal(locs().length, 0, "미리 묻기 결과를 보냈다");
    });
    w.__once = [];
    await s0._msg({ type: "location_request" }); await sleep(20);
    check("정책상담: 서버가 물으면 한 번 측위한다", () => { assert.equal(w.__once.length, 1); assert.equal(w.__once[0].o.enableHighAccuracy, true); });
    w.__once[0].ok({ coords: { latitude: 37.3943, longitude: 126.9568, accuracy: 9 } }); await sleep(10);
    check("정책상담: 측위 결과를 세션에 보낸다", () => { const L = locs(); assert.equal(L.length, 1); assert.equal(L[0].lat, 37.3943); assert.equal(L[0].lng, 126.9568); });
    await s0._msg({ type: "location_request" }); await sleep(20);
    w.__once[1].ok({ coords: { latitude: 37.40, longitude: 126.96, accuracy: 900 } }); await sleep(10);
    check("정책상담: 정확도가 나쁜 측위(150m 초과)는 보내지 않고 못 잡았다고만 알린다", () => { assert.equal(locs().length, 1); assert.equal(unav(), 1); });
    await s0._msg({ type: "location_request" }); await sleep(20);
    w.__once[2].err({ code: 1 }); await sleep(10);
    check("정책상담: 권한 거부는 조용히 알린다 · 위치 감시를 시작하지 않는다 · 상태 줄 그대로", () => {
      assert.equal(locs().length, 1); assert.equal(unav(), 2); assert.equal(w.__watch, undefined);
      assert.equal(($("naviStatus") || {}).textContent, st0);
    });
    // 권한을 아직 묻지 않은 상태(prompt) — 버튼을 띄우고 서버에는 바로 '없음'
    w.navigator.permissions = { query: async () => ({ state: "prompt" }) };
    await s0._msg({ type: "location_request" }); await sleep(20);
    check("정책상담: 권한 미결정이면 [현재 위치 사용] 버튼을 띄우고 서버엔 바로 없음을 알린다 (v2.19.1)", () => {
      assert.ok($("locAskBtn"), "버튼이 없다"); assert.equal(unav(), 3); assert.equal(w.__once.length, 3, "버튼 전에 측위했다");
    });
    await s0._msg({ type: "location_request" }); await sleep(20);
    check("정책상담: 버튼이 남은 채 다시 물으면 이전 버튼을 지우고 지금 자리에 하나만 띄운다", () => {
      assert.equal(w.document.querySelectorAll("#locAskBtn").length, 1); assert.equal(w.document.querySelectorAll(".bubble--loc").length, 1);
      assert.equal(w.document.querySelector("#chat").lastElementChild, $("locAskBtn").closest(".bubble")); assert.equal(unav(), 4);
    });
    $("locAskBtn").click(); await sleep(10);
    await s0._msg({ type: "location_request" }); await sleep(20);
    check("정책상담: 측위 중 재요청 — 버튼 유지 · 없음만 알림", () => { assert.equal(w.document.querySelectorAll("#locAskBtn").length, 1); assert.ok($("locAskBtn").disabled); assert.equal(unav(), 5); });
    w.__once[3].ok({ coords: { latitude: 37.40, longitude: 126.96, accuracy: 900 } }); await sleep(10);
    check("정책상담: 버튼 측위 정확도 미달 — 보내지 않고 버튼을 다시 누를 수 있게 남긴다", () => {
      assert.equal(locs().length, 1); assert.ok($("locAskBtn") && !$("locAskBtn").disabled); assert.match(w.document.querySelector(".bubble--loc").textContent, /정확하지 않습니다/);
    });
    $("locAskBtn").click(); await sleep(10);
    w.__once[4].err({ code: 1 }); await sleep(10);
    check("정책상담: 버튼 측위 거부 — 버튼 살려 두고 설정 안내", () => { assert.ok(!$("locAskBtn").disabled); assert.match(w.document.querySelector(".bubble--loc").textContent, /거부/); });
    $("locAskBtn").click(); await sleep(10);
    w.__once[5].ok({ coords: { latitude: 37.3953, longitude: 126.9568, accuracy: 7 } }); await sleep(10);
    check("정책상담: 버튼으로 받은 위치를 보내고 다시 물어보라고 안내한다", () => {
      const L = locs(); assert.equal(L.length, 2); assert.equal(L[1].lat, 37.3953);
      assert.equal($("locAskBtn"), null); assert.match(w.document.querySelector(".bubble--loc").textContent, /다시 한 번 물어봐/);
    });
    w.navigator.permissions = { query: async () => ({ state: "denied" }) };
    await s0._msg({ type: "location_request" }); await sleep(20);
    check("정책상담: 권한이 막혀 있으면 설정 안내를 띄운다", () => { assert.equal(unav(), 6); assert.match(w.document.querySelector("#chat").textContent, /사이트 설정/); });
    // 미리 묻기 — 권한 상태별 분기
    for (const [state, n] of [["granted", 0], ["denied", 0], ["prompt", 1]]) {
      w.navigator.permissions = { query: async () => ({ state }) }; w.__once = [];
      w.NAVI.primeLocation(); await sleep(10);
      check("정책상담: 미리 묻기 — 권한 " + state + "이면 측위 " + n + "번", () => assert.equal(w.__once.length, n));
    }
    delete w.navigator.permissions;
  }
  check("정책상담: 상담 중에는 뒤로 가기를 붙잡는다(종전대로)", () => { assert.equal(w.__BACK.busy(), true); assert.equal(w.__BACK.armed(), true); assert.equal(w.__reconnectNavi(), false); });
  const dest = { name: "안양시청", kind: "building", lat: 37.3943, lng: 126.9568 };
  await s0._msg({ type: "ui_action", action: "handoff_navi", payload: { action: "handoff_navi", dest, profile: "visual" } });
  const btn = () => [...w.document.querySelectorAll("#chat .bubble--nav .navjump")].pop();
  check("길안내 요청 결과 → 넘기기 버튼(목적지 이름 포함)", () => { assert.ok(btn(), "버튼 없음"); assert.match(btn().textContent, /이동경로 안내에서 안내 받기 — 안양시청/); });
  check("넘기기 버튼을 눌러도 길안내 화면으로 바뀌지 않는다", () => { btn().click(); assert.ok($("view-chat").classList.contains("active")); assert.ok(!$("view-navi").classList.contains("active")); });
  check("넘길 때 목적지·프로필만 싣는다", () => { const h = opened[1]; assert.equal(JSON.stringify(h.dest), JSON.stringify(dest)); assert.equal(h.profile, "visual"); assert.equal(Object.keys(h).sort().join(","), "dest,profile"); });
  check("넘어가 있는 동안 멈춤", () => assert.equal(w.__CHAT.held(), "handoff"));
  setVisible(w, "hidden"); setVisible(w, "visible");
  check("돌아오면 상담을 잇는다", () => assert.equal(w.__CHAT.held(), ""));

  // 늦게 열린 앱 — 멈춤이 5초 뒤 풀린 다음에 화면이 가려지는 경우
  btn().click();
  w.__CHAT.hold(false, "handoff");                       // (5초 경과로 풀린 상태를 흉내)
  setVisible(w, "hidden");
  check("넘긴 직후 화면이 가려지면 다시 멈춘다", () => assert.equal(w.__CHAT.held(), "handoff"));
  setVisible(w, "visible");
  check("다시 돌아오면 풀리고, 그 뒤 가려져도 멈추지 않는다", () => { assert.equal(w.__CHAT.held(), ""); setVisible(w, "hidden"); assert.equal(w.__CHAT.held(), ""); setVisible(w, "visible"); });
  check("넘기기 버튼에 초점이 간다", () => assert.equal(w.document.activeElement, btn()));
  // 정책상담 세션의 평소 흐름 — 길안내 쪽 준비가 없어도 오류가 없어야 한다
  let threw = null;
  try {
    await s0._msg({ type: "ai_transcript", content: "장애인 활동지원은 " });
    await s0._msg({ type: "ai_transcript", content: "주민센터에서 신청합니다." });
    await s0._msg({ type: "turn_complete" });
    await s0._msg({ type: "user_transcript", content: "고마워" });
  } catch (e) { threw = e; }
  check("정책상담: 답변·턴 종료 처리에 오류가 없다", () => { assert.equal(threw, null); assert.ok([...w.document.querySelectorAll("#chat .bubble")].some((b) => /주민센터에서 신청합니다/.test(b.textContent))); });

  await s0._msg({ type: "ui_action", action: "show_tour_spots", payload: { action: "show_tour_spots", items: [{ poi_id: "T1", name: "박물관" }] } });
  check("관광지·편의시설 결과 → '이동경로 안내 열기' 버튼", () => assert.match(btn().textContent, /이동경로 안내 열기/));
  await s0._msg({ type: "ui_action", action: "show_culture", payload: { action: "show_culture", items: [{ name: "도서관" }] } });
  check("문화·체육 시설 결과(v2.4.0)도 정책상담에서는 '이동경로 안내 열기' 버튼", () => assert.match(btn().textContent, /이동경로 안내 열기/));
  await s0._msg({ type: "ui_action", action: "route_unavailable", payload: { action: "route_unavailable", reason: "place_not_found", place: "어디" } });
  check("경로 불가 알림은 화면을 건드리지 않는다", () => assert.ok($("view-chat").classList.contains("active")));
  await s0._msg({ type: "tool_call", name: "plan_accessible_route", args: {} });
  check("정책상담 화면에는 '찾는 중' 막대를 켜지 않는다", () => assert.ok(!$("naviStatus").classList.contains("navi-status--busy")));
  await s0._msg({ type: "ui_action", action: "handoff_navi", payload: { action: "handoff_navi", dest, profile: "hacker" } });
  btn().click();
  check("모르는 프로필 값은 싣지 않는다", () => assert.equal(opened[opened.length - 1].profile, ""));
  w.close();
}
{
  // 넘기기 주소 만들기 · 열리지 않았을 때 멈춤 해제
  const w = boot("https://example.test/policy", { ua: "Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile" });
  await ready(w);
  const h = { dest: { name: "안양 시청", kind: "building", lat: 37.39430001, lng: 126.95680001 }, profile: "wheelchair_manual" };
  check("/policy 도 정책상담", () => assert.equal(w.__PRODUCT, "policy"));
  check("웹 주소: /navi + 목적지·프로필", () => {
    const u = new URL(w.__HANDOFF.webUrl(h));
    assert.equal(u.pathname, "/navi"); assert.equal(u.searchParams.get("dest_name"), "안양 시청");
    assert.equal(u.searchParams.get("dest_lat"), "37.394300"); assert.equal(u.searchParams.get("dest_lng"), "126.956800");
    assert.equal(u.searchParams.get("profile"), "wheelchair_manual"); assert.equal(u.searchParams.get("dest_kind"), "building");
  });
  check("앱 주소: intent + 패키지 + 웹 대체 주소", () => {
    const a = w.__HANDOFF.appUrl(h);
    assert.match(a, /^intent:\/\/navi\?dest_name=/); assert.match(a, /#Intent;scheme=accessnavi;package=kr\.co\.sweetk\.accessnavi;S\.browser_fallback_url=https%3A%2F%2Fexample\.test%2Fnavi%3F/); assert.match(a, /;end$/);
  });
  check("목적지 없이 열기: 주소에 값이 없다", () => { assert.equal(w.__HANDOFF.query(null), ""); assert.equal(w.__HANDOFF.webUrl({}), "https://example.test/navi"); });
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  await w.__sockets[0]._open(); await sleep(20);
  w.__CHAT.hold(true, "call");
  check("통화 멈춤은 화면 복귀로 풀리지 않는다", () => { setVisible(w, "hidden"); setVisible(w, "visible"); assert.equal(w.__CHAT.held(), "call"); });
  w.__APP.onAudioFocus(true);
  check("통화가 끝나면 풀린다", () => assert.equal(w.__CHAT.held(), ""));
  // 넘기기와 통화가 겹칠 때 — 한쪽이 끝나도 다른 쪽이 남아 있으면 멈춘 채로
  w.__CHAT.hold(true, "handoff"); setVisible(w, "hidden"); w.__CHAT.hold(true, "call");
  check("겹침: 통화가 앞선다", () => assert.equal(w.__CHAT.held(), "call"));
  w.__CHAT.hold(false, "call");
  check("겹침: 통화가 끝나도 넘어가 있는 동안은 멈춤 유지", () => assert.equal(w.__CHAT.held(), "handoff"));
  setVisible(w, "visible");
  check("겹침: 돌아오면 모두 풀린다", () => assert.equal(w.__CHAT.held(), ""));
  w.close();
}

// ───────── 이동경로 안내 ─────────
{
  const w = boot("https://example.test/navi");
  const $ = (id) => w.document.getElementById(id);
  await ready(w);
  check("이동경로 안내: 시작 화면이 길안내 전용", () => {
    assert.equal(w.__PRODUCT, "navi");
    assert.equal(w.document.querySelector("#view-mode h1").textContent, "이동경로 안내");
    assert.equal($("modeNaviBtn").querySelector(".lab").textContent, "길안내 시작");
    assert.equal(typeof w.__watch, "function", "위치 감시가 시작되지 않았다");
  });
  check("브라우저에서는 저절로 시작하지 않는다", () => assert.equal(w.__sockets.length, 0));
  $("modeNaviBtn").click(); await sleep(80);
  await w.__sockets[0]._open(); await sleep(30);
  check("이동경로 안내: 세션은 navi 모드 · 평소에는 인사말을 받는다", () => { assert.match(w.__sockets[0].url, /mode=navi/); assert.doesNotMatch(w.__sockets[0].url, /greet=0/); });
  check("길안내 화면이 열린다", () => assert.ok($("view-navi").classList.contains("active")));
  // 떠 있는 화면에 새 목적지
  w.NAVI._internals().setHere({ lat: P(0)[0], lng: P(0)[1] });
  const ok = w.NAVI.handoff({ dest: { name: "안양시청", kind: "building", lat: 37.3943, lng: 126.9568 }, profile: "visual" });
  await sleep(700);
  check("떠 있는 화면에 목적지가 들어오면 경로를 찾는다", () => {
    assert.equal(ok, true);
    const u = (w.__fetchLog || []).filter((x) => x.includes("plan_accessible_route")).pop();
    assert.ok(u, "경로 요청 없음");
    const q = new URL(u, "https://example.test").searchParams;
    assert.equal(q.get("destination_lat"), "37.3943"); assert.equal(q.get("destination_type"), "building");
    assert.equal(q.get("destination_place"), "안양시청"); assert.equal(q.get("profile"), "visual");
  });
  // 찾는 중 표시
  const st = $("naviStatus");
  const before = st.textContent;
  await w.__sockets[0]._msg({ type: "tool_call", name: "plan_accessible_route", args: {} });
  check("말로 경로를 물으면 '찾는 중' 막대가 켜진다", () => { assert.ok(st.classList.contains("navi-status--busy")); assert.equal(st.textContent, "경로를 찾는 중입니다…"); assert.equal(st.getAttribute("aria-busy"), "true"); });
  await w.__sockets[0]._msg({ type: "ai_transcript", content: "경로를 찾았어요." });
  check("답변이 시작되면 꺼지고 문구를 되돌린다", () => { assert.ok(!st.classList.contains("navi-status--busy")); assert.equal(st.textContent, before); assert.equal(st.getAttribute("aria-busy"), null); });
  await w.__sockets[0]._msg({ type: "tool_call", name: "search_by_keyword", args: {} });
  check("정책 검색 도구에는 켜지 않는다", () => assert.ok(!st.classList.contains("navi-status--busy")));
  await w.__sockets[0]._msg({ type: "tool_call", name: "find_toilet", args: {} });
  await w.__sockets[0]._msg({ type: "ui_action", action: "show_toilets", payload: { action: "show_toilets", items: [] } });
  check("결과(ui_action)가 오면 꺼진다", () => assert.ok(!st.classList.contains("navi-status--busy")));
  check("목적지가 없는 넘겨받기는 무시", () => { assert.equal(w.NAVI.handoff({}), false); assert.equal(w.NAVI.handoff({ dest: { name: "x" } }), false); });
  // 진행 중 판정 · 끝내기 (v2.0.2)
  check("이동경로 안내: 세션만 열려 있으면 '진행 중'이 아니다(뒤로 가기를 붙잡지 않는다)", () => { assert.equal(w.__BACK.busy(), false); assert.equal(w.__BACK.armed(), false); });
  {
    const N2 = w.NAVI._internals();
    await sleep(900);   // 경로 요청 결과 반영
    N2.startGuidance(); await sleep(1200);
    check("안내가 시작되면 뒤로 가기를 붙잡는다", () => { assert.equal(w.__BACK.busy(), true); assert.equal(w.__BACK.armed(), true); });
    w.history.back(); await sleep(60);
    check("안내 중 뒤로 가기 → '안내를 끝낼까요?'", () => { assert.equal($("naviEndModal").hidden, false); assert.equal($("naviEndTitle").textContent, "안내를 끝낼까요?"); });
    $("naviEndConfirmBtn").click(); await sleep(1200);
    check("확인 → 경로를 지우고 이 화면에 머문다 · 세션 유지 · 뒤로 가기 놓음", () => {
      assert.ok($("view-navi").classList.contains("active")); assert.equal(N2.routeLines().length, 0);
      assert.ok($("controls").classList.contains("active")); assert.equal(w.__BACK.armed(), false);
      assert.equal($("naviEndBtn").textContent, "초기화");
    });
    // 세션이 끝난 채 화면에 머무는 경우 — 화면을 만지면 조용히 다시 붙는다
    const n0 = w.__sockets.length;
    await w.__sockets[n0 - 1]._msg({ type: "auto_close", message: "응답이 없어 종료합니다." });   // 오래 말이 없어 서버가 끝낸 경우
    w.__sockets[n0 - 1].readyState = 3; w.__sockets[n0 - 1].onclose && w.__sockets[n0 - 1].onclose({ code: 1000, wasClean: true }); await sleep(80);
    check("세션이 끝나도 길안내 화면에 머문다", () => { assert.ok($("view-navi").classList.contains("active")); assert.ok(!$("controls").classList.contains("active")); });
    const re1 = w.__reconnectNavi(); await sleep(150);
    check("화면을 만지면 인사말 없이 다시 붙는다", () => {
      assert.equal(re1, true);
      const sN = w.__sockets[w.__sockets.length - 1];
      assert.ok(w.__sockets.length > n0, "새 연결 없음"); assert.match(sN.url, /mode=navi/); assert.match(sN.url, /greet=0/);
      assert.equal(w.__reconnectNavi(), false, "연달아 다시 붙으려 함");
    });
    await w.__sockets[w.__sockets.length - 1]._open(); await sleep(30);
  }
  // 통화
  const N = w.NAVI._internals();
  w.__spoken = [];
  w.__APP.onAudioFocus(false);
  N.speak("통화 중 안내");
  await sleep(30);
  check("통화 중에는 안내 음성을 내지 않고 마이크도 멈춘다", () => { assert.ok(!(w.__spoken || []).includes("통화 중 안내")); assert.equal(w.__CHAT.held(), "call"); });
  w.__APP.onAudioFocus(true);
  check("통화가 끝나면 멈춤 해제", () => assert.equal(w.__CHAT.held(), ""));
  w.close();
}
{
  // 주소로 넘겨받은 목적지
  const w = boot("https://example.test/navi?dest_name=" + encodeURIComponent("테스트 박물관") + "&dest_poi=T1&dest_kind=tour&profile=wheelchair_manual&x=1",
    { preset: (win) => win.localStorage.setItem("acc_trip_v1", JSON.stringify({ dest: { poi_id: "OLD", name: "옛 목적지" }, profile: "visual", ts: Date.now() - 60000 })) });
  const $ = (id) => w.document.getElementById(id);
  await ready(w, 250);
  check("주소의 목적지: 안내 문구가 뜨고 주소에서 지운다", () => {
    assert.ok($("handoffNote"), "안내 문구 없음"); assert.match($("handoffNote").textContent, /테스트 박물관까지 안내를 준비했습니다/);
    assert.equal(w.location.search, ""); assert.equal(w.location.pathname, "/navi");
  });
  check("넘겨받은 목적지가 '이어서 안내'보다 앞선다", () => { assert.equal($("tripResumeModal"), null); assert.equal(w.localStorage.getItem("acc_trip_v1"), null); });
  $("modeNaviBtn").click(); await sleep(80);
  await w.__sockets[0]._open(); await sleep(30);
  w.NAVI._internals().setHere({ lat: P(0)[0], lng: P(0)[1] });
  await sleep(700);
  check("넘겨받아 시작하는 세션은 인사말을 받지 않는다", () => assert.match(w.__sockets[0].url, /greet=0/));
  check("시작하면 넘겨받은 목적지로 경로를 찾는다(poi·프로필)", () => {
    const u = (w.__fetchLog || []).filter((x) => x.includes("plan_accessible_route")).pop();
    assert.ok(u, "경로 요청 없음");
    const q = new URL(u, "https://example.test").searchParams;
    assert.equal(q.get("destination_poi_id"), "T1"); assert.equal(q.get("destination_type"), "tour"); assert.equal(q.get("profile"), "wheelchair_manual");
    assert.equal($("handoffNote"), null);
  });
  w.close();
}
{
  // 잘못된 값 · 앱 안
  const w = boot("https://example.test/navi?dest_name=x&dest_lat=abc&dest_lng=999&profile=root");
  await ready(w);
  check("좌표가 잘못된 주소는 넘겨받지 않는다", () => { assert.equal(w.document.getElementById("handoffNote"), null); assert.equal(w.NAVI.parseHandoff("?dest_lat=91&dest_lng=10"), null); });
  check("값 다듬기: 이름 80자 · 모르는 프로필 버림 · 종류", () => {
    const h = w.NAVI.parseHandoff("?dest_name=" + "가".repeat(200) + "&dest_lat=37.4&dest_lng=126.9&profile=root&dest_kind=evil");
    assert.equal(h.dest.name.length, 80); assert.equal(h.profile, ""); assert.equal(h.dest.kind, "building");
    assert.equal(w.NAVI.parseHandoff("?dest_poi=S1&dest_kind=transit_station").dest.kind, "transit_station");
  });
  w.close();
}
{
  const w = boot("https://example.test/navi?dest_name=A&dest_lat=37.39&dest_lng=126.95", { ua: "Mozilla/5.0 (Linux; Android 14) AccessNaviApp/2.0.0" });
  await ready(w, 700);
  check("앱 안에서는 넘겨받으면 바로 시작한다", () => { assert.equal(w.__APP_SHELL, true); assert.equal(w.__sockets.length, 1); assert.match(w.__sockets[0].url, /mode=navi/); });
  w.close();
}
{
  const calls = [];
  const w = boot("https://example.test/navi", { ua: "Mozilla/5.0 (Linux; Android 14) AccessNaviApp/2.0.0", preset: (win) => { win.AccessNaviApp = { setBusy: (b) => calls.push(b) }; } });
  await ready(w, 1300);
  check("앱 안: 넘겨받은 것이 없어도 바로 길안내 화면으로 · 상태를 앱에 알린다", () => { assert.equal(w.__sockets.length, 1); assert.ok(calls.length >= 1); });
  check("앱에 로그아웃 기능이 없으면 버튼을 보이지 않는다", () => assert.equal(w.document.getElementById("naviLogoutBtn").hidden, true));
  check("앱 안에서는 화면이 뒤로 가기를 붙잡지 않는다(앱이 직접 묻는다)", () => { w.__BACK.arm(); assert.equal(w.__BACK.armed(), false); });
  w.close();
}
{
  // 앱 안 — 로그아웃 버튼 · 뒤로 가기 키 (v2.0.3)
  let out = 0;
  const w = boot("https://example.test/navi", { ua: "Mozilla/5.0 (Linux; Android 15) AccessNaviApp/2.0.3", preset: (win) => { win.AccessNaviApp = { setBusy() {}, logout: () => { out++; } }; } });
  const $ = (id) => w.document.getElementById(id);
  await ready(w, 700);
  await w.__sockets[0]._open(); await sleep(30);
  const lo = $("naviLogoutBtn");
  check("앱 안: 길안내 화면 위쪽에 '로그아웃' — 초기화 버튼 바로 앞, 같은 모양", () => {
    assert.equal(lo.hidden, false); assert.equal(lo.textContent, "로그아웃");
    assert.equal(lo.className, $("naviEndBtn").className); assert.equal(lo.nextElementSibling, $("naviEndBtn"));
    lo.click(); assert.equal(out, 1);
  });
  check("뒤로 가기 키: 안내 중이 아니면 화면이 처리하지 않는다 → 앱이 종료를 묻는다", () => { assert.equal(w.NAVI.backKey(), false); assert.equal($("naviEndModal").hidden, true); });
  $("naviSosBtn").click(); await sleep(20);
  check("뒤로 가기 키: 열려 있는 창(긴급·편의)부터 닫는다", () => { assert.equal($("sosSheet").hidden, false); assert.equal(w.NAVI.backKey(), true); assert.equal($("sosSheet").hidden, true); assert.equal(w.NAVI.backKey(), false); });
  const N3 = w.NAVI._internals();
  N3.setHere({ lat: P(0)[0], lng: P(0)[1] });
  N3.showRoute(ROUTE.ui_action.route, "테스트"); await sleep(30);
  N3.startGuidance(); await sleep(30);
  check("뒤로 가기 키: 안내 중이면 '안내를 끝낼까요?'", () => { assert.equal(w.NAVI.backKey(), true); assert.equal($("naviEndModal").hidden, false); assert.ok(w.NAVI.isBusy()); });
  check("뒤로 가기 키: 확인 창이 떠 있으면 닫는다(안내 유지)", () => { assert.equal(w.NAVI.backKey(), true); assert.equal($("naviEndModal").hidden, true); assert.ok(w.NAVI.isBusy()); });
  $("naviEndBtn").click();
  lo.click();
  check("안내 중에는 로그아웃을 막는다", () => { assert.equal(out, 1); assert.match($("naviStatus").textContent, /안내를 끝낸 뒤에 로그아웃/); });
  check("안내 종료 버튼도 먼저 묻는다", () => { assert.equal($("naviEndModal").hidden, false); assert.ok(w.NAVI.isBusy(), "묻지 않고 끝냈다"); });
  $("naviEndCancelBtn").click();
  check("취소하면 안내가 이어진다", () => { assert.equal($("naviEndModal").hidden, true); assert.ok(w.NAVI.isBusy()); });
  $("naviEndBtn").click(); $("naviEndConfirmBtn").click(); await sleep(30);
  check("확인하면 끝내고 경로를 지운다 · 버튼은 '초기화'", () => { assert.equal(w.NAVI.isBusy(), false); assert.equal(N3.routeLines().length, 0); assert.equal($("naviEndBtn").textContent, "초기화"); });
  w.close();
}
{
  const w = boot("https://example.test/navi");
  await ready(w, 300);
  let centered = 0;
  w.kakao.maps.LatLng = function (a, b) { this.a = a; this.b = b; };
  const N = w.NAVI._internals();
  const map = N.map ? N.map() : null;
  check("브라우저: 로그아웃 버튼은 보이지 않는다", () => assert.equal(w.document.getElementById("naviLogoutBtn").hidden, true));
  check("첫 측위에 한 번만 내 위치로 옮긴다(소스)", () => {
    assert.match(HTML, /if\(!firstFixCentered && _naviOn\)\{[^\n]*\n\s*firstFixCentered = true;\s*if\(followMe && !guiding && !simActive && !routeLine && !originOverride\) recenter\(\);/);
  });
  w.close();
}

// ───────── 하위 경로 배포 (v2.1.0) ─────────
// 같은 화면을 https://www.sweetk.ai/iitp/accessistantai/… 처럼 하위 경로에서 열어도
// 서버 호출·WebSocket·아이콘·넘기기 주소가 그 경로 아래로 가야 한다. 루트 배포는 종전과 같아야 한다.
const linkHrefs = (w) => [...w.document.querySelectorAll('link[rel="manifest"],link[rel="icon"],link[rel="apple-touch-icon"]')].map((l) => l.getAttribute("href"));
{
  const w = boot("https://example.test/iitp/accessistantai/static/accessistant.html");
  await ready(w);
  check("하위 경로(정책상담): 기준 경로를 알아낸다", () => { assert.equal(w.APP_BASE, "/iitp/accessistantai"); assert.equal(w.__PRODUCT, "policy"); });
  check("하위 경로(정책상담): 서버 호출이 모두 기준 경로 아래로 간다", () => {
    const log = w.__fetchLog || []; assert.ok(log.length > 0, "호출 기록 없음");
    for (const u of log) assert.match(u, /^\/iitp\/accessistantai\/api\//, u);
  });
  check("하위 경로: 매니페스트·아이콘 링크 5개가 기준 경로 아래를 가리킨다", () => {
    const h = linkHrefs(w); assert.equal(h.length, 5);
    for (const x of h) assert.match(x, /^\/iitp\/accessistantai\/static\//, x);
  });
  check("하위 경로: 넘기기 웹 주소도 기준 경로 아래 /navi", () => assert.equal(w.__HANDOFF.webUrl({}), "https://example.test/iitp/accessistantai/navi"));
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  check("하위 경로: 상담 WebSocket 주소", () => { assert.ok(w.__sockets[0], "세션 없음"); assert.match(w.__sockets[0].url, /^wss:\/\/example\.test\/iitp\/accessistantai\/ws\/live-chat/); });
  w.close();
}
{
  const w = boot("https://example.test/iitp/accessistantai/navi");
  await ready(w, 300);
  check("하위 경로(이동경로 안내): /navi 를 알아본다", () => { assert.equal(w.APP_BASE, "/iitp/accessistantai"); assert.equal(w.__PRODUCT, "navi"); });
  check("하위 경로(이동경로 안내): 서버 호출이 모두 기준 경로 아래로 간다", () => {
    const log = w.__fetchLog || []; assert.ok(log.some((u) => u.includes("/api/v1/config")), "설정 호출 없음");
    for (const u of log) assert.match(u, /^\/iitp\/accessistantai\/(api\/|app\/latest\.json$)/, u);   // 앱 배포본 정보(v2.10.0)도 기준 경로 아래
  });
  w.close();
}
{
  const w = boot("https://example.test/static/accessistant.html");
  await ready(w);
  check("루트 배포: 기준 경로가 비어 있고 주소는 종전 그대로", () => {
    assert.equal(w.APP_BASE, "");
    for (const x of linkHrefs(w)) assert.match(x, /^\/static\//, x);
    for (const u of (w.__fetchLog || [])) assert.match(u, /^\/api\//, u);
    assert.equal(w.__HANDOFF.webUrl({}), "https://example.test/navi");
  });
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  check("루트 배포: 상담 WebSocket 주소는 종전 그대로", () => assert.match(w.__sockets[0].url, /^wss:\/\/example\.test\/ws\/live-chat/));
  w.close();
}
{
  // 교차검토 반영 — "//다른호스트/navi" 처럼 열려도 기준 경로로 삼지 않는다(서버 호출이 밖으로 새면 안 된다)
  const w = boot("https://example.test//evil.test/navi");
  await ready(w, 300);
  check("이상한 주소로 열려도 서버 호출이 다른 호스트로 가지 않는다", () => {
    assert.equal(w.APP_BASE, "");
    for (const u of (w.__fetchLog || [])) assert.match(u, /^\/api\//, u);
    for (const x of linkHrefs(w)) assert.match(x, /^\/static\//, x);
  });
  w.close();
}
check("서버로 보내는 주소에 루트 고정 문자열이 남지 않았다(소스)", () => {
  // withBase("…") 로 감싼 것과, 링크 목록(L 배열 — 실행 시 withBase 로 감싼다)의 항목은 제외
  const bad = [...HTML.matchAll(/[\x60'"(]\/(api|static|ws)\/[^\x60'")]*/g)].filter((m) =>
    !/withBase\($/.test(HTML.slice(Math.max(0, m.index - 10), m.index)) &&
    !HTML.startsWith('"]', m.index + m[0].length));
  assert.equal(bad.length, 0, bad.map((m) => m[0]).join(", "));
  assert.match(HTML, /href="'\+withBase\(L\[i\]\[3\]\)/, "링크 목록을 withBase 로 감싸 쓰지 않는다");
});

// ───────── 앱 내려받기 (v2.10.0) ─────────
const LATEST = { versionCode: 20004, versionName: "2.0.4", file: "accessnavi-2.0.4.apk", sha256: "00" };
{
  const w = boot("https://example.test/iitp/accessistantai/navi", { preset: (win) => { win.__latest = LATEST; } });
  const $ = (id) => w.document.getElementById(id);
  await ready(w);
  check("앱 내려받기: 이동경로 안내 웹 — 길안내 시작 바로 아래에 최신 설치 파일 링크", () => {
    const a = $("appDownloadBtn");
    assert.equal(a.style.display, "");
    assert.equal(a.previousElementSibling.id, "modeNaviBtn");
    assert.equal(a.getAttribute("href"), "/iitp/accessistantai/app/download/accessnavi-2.0.4.apk");
    assert.equal(a.getAttribute("download"), "accessnavi-2.0.4.apk");
    assert.match(a.querySelector(".appver").textContent, /최신 v2\.0\.4/);
  });
  w.close();
}
{
  const w = boot("https://example.test/navi");
  await ready(w);
  check("앱 내려받기: 배포본 정보가 없으면 숨긴다", () => assert.equal(w.document.getElementById("appDownloadBtn").style.display, "none"));
  w.close();
}
{
  const w = boot("https://example.test/navi", { preset: (win) => { win.__latest = { versionName: "<b>", file: "../x.apk" }; } });
  await ready(w);
  check("앱 내려받기: 잘못된 파일 이름이면 숨긴다", () => assert.equal(w.document.getElementById("appDownloadBtn").style.display, "none"));
  w.close();
}
{
  const w = boot("https://example.test/navi", { ua: "Mozilla/5.0 (Linux; Android 15) AccessNaviApp/2.0.4", preset: (win) => { win.__latest = LATEST; win.AccessNaviApp = { setBusy() {} }; } });
  await ready(w);
  check("앱 내려받기: 앱 안에서는 보이지 않는다", () => {
    assert.equal(w.document.getElementById("appDownloadBtn").style.display, "none");
    assert.ok(!(w.__fetchLog || []).some((u) => u.includes("/app/latest.json")));
  });
  w.close();
}
{
  const w = boot("https://example.test/static/accessistant.html", { preset: (win) => { win.__latest = LATEST; } });
  await ready(w);
  check("앱 내려받기: 정책상담에서는 보이지 않는다", () => assert.equal(w.document.getElementById("appDownloadBtn").style.display, "none"));
  w.close();
}

// ───────── 소스 가드 ─────────
// 정책상담·이동경로 안내는 따로 여는 제품이라 화면 사이를 오가는 탭(마크업·스타일·핸들러)을 두지 않는다
check("화면 사이 탭은 두지 않는다", () => assert.doesNotMatch(HTML, /modebar|id="bar(Chat|Navi)C?"/));
check("멈춘 동안 마이크 프레임을 보내지 않는다", () => assert.match(HTML, /if \(micHold\) return;/));
check("멈춘 동안 온 음성은 내지 않는다", () => assert.match(HTML, /if \(micHold\) break;/));

let failed = 0;
for (const [st, name] of results) { console.log(`${st === "PASS" ? "  ok" : "FAIL"}  ${name}`); if (st === "FAIL") failed++; }
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
