/**
 * 세션 유지 런타임 검증 (jsdom) — v1.59.0
 *
 *   1) 연결이 끊기면 화면을 건드리지 않고 같은 sid + resume=1 로 다시 붙는다
 *   2) 이용자가 끝내거나 서버가 끝내면 다시 붙지 않는다
 *   3) 상담·안내 중 뒤로 가기는 페이지를 떠나지 않고 종료 확인을 띄운다
 *   4) 안내 도중 닫혔다 다시 열면 이어서 안내할지 묻는다
 *   5) 안내 재개 문장 — 구간 중간이면 "계속 직진", 끝이면 다음 안내
 *   6) '지금 말하기' 는 마이크 차단을 잠시 푼다
 *   7) 첫 연결이 열리기 전에 닫혔다 다시 붙으면 일반 시작(컨트롤·'상담 시작'·resume 없음)으로 처리한다
 *   8) "fatal": false 오류는 재연결을 끄지 않는다 · 멈춤 중에는 기기 음성이 답변을 읽지 않는다 · 화면 켜짐 유지는 하나만 잡는다
 *   9) 경로 이탈 재탐색이 실패해도 기존 경로로 안내를 계속한다
 *  10) 이동경로 안내에서 말로 요청한 화장실·식당·긴급지원 결과는 시트가 바로 열린다
 *  11) 측위·위치 오류 안내 문구 · 경로 요약 '목록으로'의 경로선 정리
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
const ROUTE = { status: "success", route_id: "r_s", ui_action: { action: "show_route", route: { route_id: "r_s", destination: { type: "building", poi_id: null, lat: P(8)[0], lng: P(8)[1] }, routes: [{
  summary: { total_distance_m: 900, duration_sec: 800, max_slope_deg: 2, stairs_cnt: 0, crossing_cnt: 1 },
  geometry: [P(0), P(1), P(7), P(8)],
  steps: [
    { idx: 0, maneuver: "depart", instruction: "중앙로를 따라 110m 앞으로 이동합니다.", distance_m: 110, coord: P(0), link_type: "sidewalk", link_name: "중앙로", warnings: [] },
    { idx: 1, maneuver: "left", instruction: "좌회전 후 안양로를 따라 673m 이동합니다.", distance_m: 673, coord: P(1), link_type: "road", link_name: "안양로", warnings: ["경사 주의"] },
    { idx: 2, maneuver: "crossing", instruction: "횡단보도를 건너 12m 이동합니다.", distance_m: 12, coord: P(7), link_type: "crossing", warnings: [] },
    { idx: 3, maneuver: "arrive", instruction: "목적지에 도착했습니다.", distance_m: 0, coord: P(8), warnings: [] },
  ] }] } } };
const SPOTS = { status: "success", results: [{ poi_id: "T1", name: "테스트 박물관", addr: "안양시", facilities: [], score: 0.9 }] };

function boot(preset, url) {
  const dom = new JSDOM(HTML, { runScripts: "dangerously", pretendToBeVisual: true, url: url || "https://example.test/navi",
    beforeParse(w) {
      if (preset) preset(w);
      const sockets = [];
      class FakeWS {
        constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
        send(d) { this.sent.push(d); }
        close() { if (this.readyState === 3) return; this.readyState = 3; setTimeout(() => this.onclose && this.onclose({ code: 1000, wasClean: true }), 0); }
        _open() { this.readyState = 1; return this.onopen && this.onopen(); }
        _drop() { this.readyState = 3; this.onclose && this.onclose({ code: 1006, wasClean: false }); }
        _msg(o) { return this.onmessage && this.onmessage({ data: JSON.stringify(o) }); }
      }
      FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;
      w.WebSocket = FakeWS; w.__sockets = sockets;
      w.fetch = async (url) => { const u = String(url);
        const body = u.includes("/api/v1/config") ? (w.__cfg || CONFIG) : u.includes("find_bf_tour_spots") ? SPOTS : u.includes("plan_accessible_route") ? ROUTE : {};
        (w.__fetchLog = w.__fetchLog || []).push(u);
        // 경로 요청만 가로채는 훅 — 실패 응답·통신 예외·지연 응답을 흉내 낸다(없으면 기본 ROUTE)
        if (u.includes("plan_accessible_route") && w.__planHook) return w.__planHook(u);
        return { ok: true, json: async () => body }; };
      w.navigator.geolocation = { watchPosition: (ok, err) => { w.__watch = ok; w.__watchErr = err; return 1; }, clearWatch: () => {} };
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

// ───────── 1) 2) 조용한 재연결 ─────────
{
  const w = boot(null, "https://example.test/policy");
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  w.document.querySelector('[data-go="text"]').click();
  await sleep(50);
  const s0 = w.__sockets[0];
  check("첫 연결: sid 가 붙고 resume 은 없다", () => { assert.ok(s0, "소켓 없음"); assert.match(s0.url, /[?&]sid=s[a-z0-9]+/); assert.doesNotMatch(s0.url, /resume=1/); });
  await s0._open(); await sleep(20);
  check("연결되면 상담 화면이 열린다", () => { assert.ok($("controls").classList.contains("active")); assert.ok($("view-chat").classList.contains("active")); });
  const bubbles0 = w.document.querySelectorAll("#chat .bubble").length;
  s0._drop();
  check("끊김 직후: 화면·입력창은 그대로, 오류 말풍선 없음", () => {
    assert.ok($("controls").classList.contains("active")); assert.equal($("textInput").disabled, false);
    assert.ok($("view-chat").classList.contains("active"));
    assert.equal(w.document.querySelectorAll("#chat .bubble.err").length, 0);
    assert.equal(w.__CHAT._net().reconnecting, true);
  });
  await sleep(500);
  const s1 = w.__sockets[1];
  const sid = (s0.url.match(/sid=([a-z0-9]+)/) || [])[1];
  check("0.3초 뒤 같은 sid + resume=1 로 다시 붙는다", () => { assert.ok(s1, "재연결 소켓 없음"); assert.ok(s1.url.includes("sid=" + sid)); assert.match(s1.url, /resume=1/); });
  await s1._open(); await sleep(20);
  check("재연결 성공: '상담 시작' 말풍선을 다시 넣지 않고 상태가 풀린다", () => {
    assert.equal(w.document.querySelectorAll("#chat .bubble").length, bubbles0);
    const n = w.__CHAT._net(); assert.equal(n.reconnecting, false); assert.equal(n.retry, 0); assert.equal(n.open, true);
  });
  // 재연결이 거듭 실패하는 동안에도 화면은 유지
  s1._drop(); await sleep(450); const s2 = w.__sockets[2]; s2._drop();
  check("재연결 실패가 이어져도 화면 유지 · 다음 시도 대기", () => { assert.ok($("controls").classList.contains("active")); assert.equal(w.__CHAT._net().retry, 2); });
  // 이용자 종료 — 대기 중인 재연결을 버리고 끝낸다
  $("endBtn").click(); await sleep(30);
  check("이용자가 끝내면 다시 붙지 않는다", () => {
    assert.equal($("controls").classList.contains("active"), false);
    assert.equal(w.__CHAT._net().reconnecting, false); assert.equal(w.__CHAT._net().sid, null);
  });
  const cnt = w.__sockets.length; await sleep(1300);
  check("종료 뒤 예약된 재연결이 실행되지 않는다", () => assert.equal(w.__sockets.length, cnt));
  // 서버 자동 종료
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  const s3 = w.__sockets[w.__sockets.length - 1];
  check("새 상담은 새 sid", () => { assert.doesNotMatch(s3.url, /resume=1/); assert.ok(!s3.url.includes("sid=" + sid)); });
  await s3._open(); await s3._msg({ type: "auto_close", message: "자동 종료" }); s3._drop(); await sleep(600);
  check("서버가 끝낸 세션(auto_close)은 다시 붙지 않는다", () => { assert.equal(w.__sockets[w.__sockets.length - 1], s3); assert.equal($("controls").classList.contains("active"), false); });
  w.close();
}

// ───────── 연결 준비 중 종료 ─────────
{
  const w = boot(null, "https://example.test/policy");
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  let release; w.__TRIAL = { enabled: false, wsQuery: () => new Promise((r) => { release = r; }), ev() {} };
  w.document.querySelector('[data-go="text"]').click(); await sleep(30);
  check("연결 준비 중에는 아직 소켓이 없다", () => assert.equal(w.__sockets.length, 0));
  w.__CHAT.endSession(); await sleep(10);
  release(""); await sleep(50);
  check("준비 중에 끝낸 세션은 뒤늦게 열리지 않는다", () => assert.equal(w.__sockets.length, 0));
  w.close();
}

// ───────── 3) 뒤로 가기 ─────────
{
  const w = boot(null, "https://example.test/policy");
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  check("상담 전에는 뒤로 가기를 가로채지 않는다", () => assert.equal(w.__BACK.armed(), false));
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  await w.__sockets[0]._open(); await sleep(20);
  check("상담이 시작되면 뒤로 가기 대비", () => assert.equal(w.__BACK.armed(), true));
  w.history.back(); await sleep(60);
  check("상담 중 뒤로 가기 → 종료 확인 창, 상담은 그대로", () => {
    assert.equal($("endModal").hidden, false); assert.ok($("controls").classList.contains("active")); assert.equal(w.__BACK.armed(), true);
  });
  w.history.back(); await sleep(60);
  check("확인 창에서 다시 뒤로 가기 = 취소", () => { assert.equal($("endModal").hidden, true); assert.ok($("controls").classList.contains("active")); });
  w.history.back(); await sleep(60);
  $("endConfirmBtn").click(); await sleep(60);
  check("이용자 종료는 서버에 알린다(bye) — 이어받기 정보 즉시 삭제", () => assert.ok(w.__sockets[0].sent.some((d) => /"type":"bye"/.test(d))));
  check("확인을 누르면 상담 종료 · 처음 화면", () => { assert.equal($("controls").classList.contains("active"), false); assert.ok($("view-mode").classList.contains("active")); assert.equal(w.__BACK.armed(), false); });
  w.close();
}

// ───────── 4) 5) 6) 여정 복원 · 재개 문장 · 지금 말하기 ─────────
{
  const w = boot();
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(200);
  const N = () => w.NAVI._internals();
  check("저장된 여정이 없으면 묻지 않는다", () => assert.equal($("tripResumeModal"), null));
  w.document.querySelector('[data-go="navi"]').click(); await sleep(80);
  await w.__sockets[0]._open(); await sleep(30);
  N().setHere({ lat: P(0)[0], lng: P(0)[1] });
  N().showRoute(ROUTE.ui_action.route, "테스트 박물관"); await sleep(30);
  N().startGuidance(); await sleep(30);
  check("안내를 시작하면 여정이 저장된다", () => { const t = JSON.parse(w.localStorage.getItem("acc_trip_v1")); assert.ok(t && t.dest && t.dest.name === "테스트 박물관"); assert.ok(w.NAVI.isBusy()); });
  check("길안내 줄에 '지금 말하기' 버튼", () => assert.ok($("naviTalkBtn")));
  $("naviTalkBtn").click();
  check("지금 말하기: 8초 동안 마이크 차단 해제", () => assert.equal(w.__CHAT._net().forced, true));
  // 재개 문장
  N().gotoStep ? N().gotoStep(1) : null;
  const at = (k, f) => N().setHere({ lat: P(k)[0] + (f || 0), lng: P(k)[1] });
  at(1);
  check("재개: 구간 시작점이면 원문(회전·거리 포함)", () => assert.match(N().resumeUtterance(1), /좌회전 후 안양로를 따라 673m/));
  at(4);
  check("재개: 구간 중간이면 회전·거리 없이 '계속 직진' + 경고 유지", () => assert.equal(N().resumeUtterance(1), "안양로를 따라 계속 직진하세요. (경사 주의)"));
  at(7, -0.0001);
  check("재개: 구간 끝이면 다음 안내를 말한다", () => assert.match(N().resumeUtterance(1), /^곧 다음 안내입니다\. .*횡단보도/));
  at(7, 0.0004);
  check("재개: 횡단보도 스텝은 원문 그대로", () => assert.match(N().resumeUtterance(2), /횡단보도를 건너/));
  // 안내 중 뒤로 가기 → 길안내 종료 확인
  w.history.back(); await sleep(60);
  check("안내 중 뒤로 가기 → 길안내 종료 확인 창", () => assert.equal($("naviEndModal").hidden, false));
  $("naviEndConfirmBtn").click(); await sleep(60);
  check("확인하면 안내를 끝내고 경로·저장된 여정을 지운다 — 화면과 세션은 그대로 (v2.0.2)", () => {
    assert.equal(w.NAVI.isBusy(), false); assert.equal(w.localStorage.getItem("acc_trip_v1"), null);
    assert.ok($("view-navi").classList.contains("active")); assert.ok(!$("view-mode").classList.contains("active"));
    assert.equal(N().routeLines().length, 0); assert.ok($("controls").classList.contains("active"));
  });
  w.close();
}
{
  // 안내 도중 닫혔다가 다시 연 상황
  const fresh = JSON.stringify({ dest: { poi_id: "T1", name: "테스트 박물관" }, profile: "wheelchair_electric", ts: Date.now() - 60000 });
  const w = boot((win) => win.localStorage.setItem("acc_trip_v1", fresh));
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(250);
  check("다시 열면 이어서 안내할지 묻는다", () => { assert.ok($("tripResumeModal")); assert.match($("tripResumeText").textContent, /테스트 박물관까지/); });
  $("tripResumeYes").click(); await sleep(80);
  if (w.__sockets[0]) await w.__sockets[0]._open();
  w.NAVI._internals().setHere({ lat: P(0)[0], lng: P(0)[1] });
  await sleep(1300);
  check("이어서 안내: 길안내 화면에서 같은 도착지로 경로를 다시 찾고 안내를 시작한다", () => {
    assert.ok($("view-navi").classList.contains("active"));
    assert.ok((w.__fetchLog || []).some((u) => u.includes("plan_accessible_route") && u.includes("destination_poi_id=T1")));
    assert.equal(w.NAVI.isBusy(), true);
  });
  check("이어서 안내로 시작한 세션은 인사말을 받지 않는다 (v2.0.1)", () => assert.match(w.__sockets[0].url, /greet=0/));
  w.close();
}
{
  const old = JSON.stringify({ dest: { poi_id: "T1", name: "테스트 박물관" }, profile: "wheelchair_electric", ts: Date.now() - 30 * 60000 });
  const w = boot((win) => win.localStorage.setItem("acc_trip_v1", old));
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(250);
  check("20분이 지난 여정은 묻지 않고 지운다", () => { assert.equal(w.document.getElementById("tripResumeModal"), null); assert.equal(w.localStorage.getItem("acc_trip_v1"), null); });
  w.close();
}

// ───────── 7) 첫 연결이 열리기 전에 닫힌 경우 ─────────
{
  const w = boot(null, "https://example.test/policy");
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  w.__skipGreeting = true;   // 첫 연결에 실으려던 '인사말 생략' 요청 — 재시도에도 그대로 실려야 한다
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  const s0 = w.__sockets[0];
  const sid = (s0.url.match(/sid=([a-z0-9]+)/) || [])[1];
  s0._drop();                // 열리기 전에 닫힘
  await sleep(500);
  const s1 = w.__sockets[1];
  check("열리기 전에 닫히면 다시 시도한다 — 같은 sid, resume 없음(이어받을 세션이 없다)", () => {
    assert.ok(s1, "재시도 소켓 없음"); assert.ok(s1.url.includes("sid=" + sid));
    assert.doesNotMatch(s1.url, /resume=1/);
  });
  check("첫 연결의 인사말 생략 요청(greet=0)은 재시도에도 실린다", () => { assert.match(s0.url, /greet=0/); assert.match(s1.url, /greet=0/); });
  await s1._open(); await sleep(20);
  check("재시도로 처음 열리면 일반 시작 — 컨트롤·입력창 활성, '상담 시작' 말풍선", () => {
    assert.ok($("controls").classList.contains("active"), "컨트롤이 켜지지 않음");
    assert.equal($("textInput").disabled, false); assert.equal($("sendTextBtn").disabled, false);
    assert.ok([...w.document.querySelectorAll("#chat .bubble")].some((b) => /상담 시작/.test(b.textContent)), "'상담 시작' 없음");
    const n = w.__CHAT._net(); assert.equal(n.reconnecting, false); assert.equal(n.retry, 0); assert.equal(n.everOpened, true);
  });
  const bubbles = w.document.querySelectorAll("#chat .bubble").length;
  s1._drop(); await sleep(500);
  const s2 = w.__sockets[2];
  check("한 번 열린 뒤의 끊김은 종전대로 조용한 재연결(resume=1, greet 없음)", () => { assert.ok(s2); assert.match(s2.url, /resume=1/); assert.doesNotMatch(s2.url, /greet=0/); });
  await s2._open(); await sleep(20);
  check("조용한 재연결은 말풍선을 더 넣지 않는다", () => assert.equal(w.document.querySelectorAll("#chat .bubble").length, bubbles));
  // 세션을 끝내고 새로 시작 — '한 번 열렸다'는 표시가 초기화돼야 한다
  $("endBtn").click(); await sleep(30);
  check("세션이 끝나면 '열린 적 있음' 표시를 되돌린다", () => assert.equal(w.__CHAT._net().everOpened, false));
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  const s3 = w.__sockets[w.__sockets.length - 1];
  s3._drop(); await sleep(500);
  const s4 = w.__sockets[w.__sockets.length - 1];
  await s4._open(); await sleep(20);
  check("다음 세션에서도 열리기 전 끊김은 일반 시작으로 처리된다", () => {
    assert.notEqual(s4, s3); assert.doesNotMatch(s4.url, /resume=1/);
    assert.ok($("controls").classList.contains("active")); assert.equal($("textInput").disabled, false);
  });
  w.close();
}

// ───────── 8) 비치명 오류 · 멈춤 중 기기 음성 · 화면 켜짐 유지 ─────────
{
  const w = boot(null, "https://example.test/policy");
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  const s0 = w.__sockets[0];
  await s0._open(); await sleep(20);
  await s0._msg({ type: "error", message: "도구 호출에 실패했습니다.", fatal: false });
  check("\"fatal\": false 오류 — 말풍선은 보이고 재연결은 꺼지지 않는다", () => {
    assert.ok([...w.document.querySelectorAll("#chat .bubble--err")].some((b) => /도구 호출에 실패/.test(b.textContent)));
    assert.equal(w.__CHAT._net().noReconnect, false);
  });
  s0._drop(); await sleep(500);
  const s1 = w.__sockets[1];
  check("비치명 오류 뒤 연결이 끊기면 다시 붙는다", () => { assert.ok(s1, "재연결 소켓 없음"); assert.match(s1.url, /resume=1/); assert.ok($("controls").classList.contains("active")); });
  if (s1) {
    await s1._open(); await sleep(20);
    await s1._msg({ type: "error", message: "세션을 계속할 수 없습니다." });   // fatal 필드 없음 = 종전대로 치명
    s1._drop(); await sleep(600);
  }
  check("fatal 필드가 없는 오류는 종전대로 세션을 끝낸다(다시 붙지 않음)", () => {
    assert.ok(s1, "재연결 소켓 없음");
    assert.equal(w.__sockets[w.__sockets.length - 1], s1); assert.equal($("controls").classList.contains("active"), false);
  });
  w.close();
}
{
  // 음성 상담(텍스트 전용 아님) — 서버 음성이 없던 턴은 기기 내장 음성이 읽는다. 멈춤(통화·앱 넘김) 중에는 읽지 않는다.
  const w = boot(null, "https://example.test/policy");
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  w.document.querySelector(".agent-btn.female").click(); await sleep(50);
  const s0 = w.__sockets[0];
  await s0._open(); await sleep(20);
  const said = () => (w.__spoken || []).join(" | ");
  const PCM = Buffer.from(new Uint8Array(480)).toString("base64");
  await s0._msg({ type: "ai_transcript", content: "첫 번째 답변입니다." });
  await s0._msg({ type: "turn_complete" });
  check("기준 동작: 서버 음성이 없던 턴은 기기 음성으로 읽는다", () => assert.match(said(), /첫 번째 답변입니다/));
  w.__CHAT.hold(true, "call");
  await s0._msg({ type: "audio", data: PCM, mime_type: "audio/pcm;rate=24000" });
  await s0._msg({ type: "ai_transcript", content: "통화 중에 온 답변입니다." });
  await s0._msg({ type: "turn_complete" });
  check("멈춤 중 서버 음성이 온 턴 — 기기 음성이 답변을 읽지 않는다", () => assert.doesNotMatch(said(), /통화 중에 온 답변/));
  await s0._msg({ type: "ai_transcript", content: "음성 없이 온 답변입니다." });
  await s0._msg({ type: "turn_complete" });
  check("멈춤 중 서버 음성이 없던 턴 — 기기 음성 폴백도 말하지 않는다", () => assert.doesNotMatch(said(), /음성 없이 온 답변/));
  await s0._msg({ type: "audio", data: PCM, mime_type: "audio/pcm;rate=24000" });
  await s0._msg({ type: "ai_transcript", content: "멈춤이 풀린 직후 끝난 답변입니다." });
  w.__CHAT.hold(false, "call");
  await s0._msg({ type: "turn_complete" });
  check("멈춤 중에 서버 음성이 왔던 턴은 멈춤이 풀린 뒤 끝나도 기기 음성으로 읽지 않는다", () => assert.doesNotMatch(said(), /멈춤이 풀린 직후/));
  await s0._msg({ type: "ai_transcript", content: "다시 정상 답변입니다." });
  await s0._msg({ type: "turn_complete" });
  check("멈춤이 풀리면 기기 음성 폴백은 종전대로 동작한다", () => assert.match(said(), /다시 정상 답변입니다/));
  w.close();
}
{
  // 화면 켜짐 유지(WakeLock) — 재연결·화면 복귀마다 새로 잡지 않고, 세션이 끝나면 전부 놓는다
  const locks = [];
  const w = boot((win) => {
    Object.defineProperty(win.navigator, "wakeLock", { configurable: true, value: {
      request: async () => { const s = { released: false, release() { this.released = true; return Promise.resolve(); } }; locks.push(s); return s; } } });
  }, "https://example.test/policy");
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  await w.__sockets[0]._open(); await sleep(20);
  check("연결되면 화면 켜짐 유지를 한 번 요청한다", () => assert.equal(locks.length, 1));
  w.__sockets[0]._drop(); await sleep(500);
  await w.__sockets[1]._open(); await sleep(20);
  check("재연결해도 살아 있는 것이 있으면 다시 요청하지 않는다", () => assert.equal(locks.length, 1));
  w.document.dispatchEvent(new w.Event("visibilitychange")); w.document.dispatchEvent(new w.Event("visibilitychange")); await sleep(20);
  check("화면 복귀 이벤트가 거듭 와도 하나만 유지한다", () => assert.equal(locks.length, 1));
  locks[0].released = true;   // 화면이 가려져 브라우저가 스스로 해제한 상황
  w.document.dispatchEvent(new w.Event("visibilitychange")); w.document.dispatchEvent(new w.Event("visibilitychange")); await sleep(20);
  check("브라우저가 해제한 뒤 돌아오면 한 번만 새로 요청한다", () => assert.equal(locks.length, 2));
  $("endBtn").click(); await sleep(30);
  check("세션이 끝나면 잡고 있던 것을 모두 놓는다", () => assert.ok(locks.every((s) => s.released), JSON.stringify(locks.map((s) => s.released))));
  w.close();
}

// ───────── 9) 경로 이탈 재탐색 실패 — 기존 경로로 안내 계속 ─────────
{
  const w = boot();
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(200);
  const N = () => w.NAVI._internals();
  w.document.querySelector('[data-go="navi"]').click(); await sleep(80);
  await w.__sockets[0]._open(); await sleep(30);
  w.__watch({ coords: { latitude: P(0)[0], longitude: P(0)[1], accuracy: 5 } });
  await N().requestRoute({ poi_id: "T1", name: "테스트 박물관" }); await sleep(30);
  N().startGuidance(); await sleep(30);
  const plans = () => (w.__fetchLog || []).filter((u) => u.includes("plan_accessible_route"));
  const said = (from) => (w.__spoken || []).slice(from).join(" | ");
  // 경로(경도 126.9500 남북선)에서 동쪽으로 약 175m — 45m 초과를 4회 연속·12초 이상이면 이탈로 본다
  const realNow = w.Date.now; let vnow = realNow();
  const offRoute = () => {
    w.Date.now = () => vnow;
    for (let i = 0; i < 4; i++) { w.__watch({ coords: { latitude: 37.3935 + 0.00001 * i, longitude: 126.9520, accuracy: 5 } }); vnow += 5000; }
    w.Date.now = realNow;
  };
  const stillGuiding = (label) => {
    assert.equal(w.NAVI.isBusy(), true, label + ": 안내가 끝남");
    assert.equal(N().steps.length, 4, label + ": 스텝이 지워짐");
    assert.ok(N().routeLines().length > 0, label + ": 경로선이 지워짐");
    assert.equal(N().tripDest().poi_id, "T1", label + ": 도착지를 잊음");
    assert.ok(w.document.querySelector(".step-now"), label + ": 스텝 카드가 사라짐");
    assert.equal($("naviEndBtn").textContent, "안내 종료", label);
  };

  // (가) 서버가 경로를 만들지 못함
  w.__planHook = async () => ({ ok: true, json: async () => ({ status: "error", message: "경로를 만들지 못했습니다." }) });
  let base = (w.__spoken || []).length, cnt = plans().length;
  offRoute(); await sleep(150);
  check("이탈 재탐색 요청이 나간다(reason=off_route)", () => { assert.equal(plans().length, cnt + 1); assert.match(plans()[plans().length - 1], /reason=off_route/); });
  check("재탐색 실패(서버 오류 응답) — 기존 경로·스텝·안내 상태 유지", () => stillGuiding("서버 오류"));
  check("재탐색 실패를 상태 줄(경고 표시)과 음성으로 알린다", () => {
    assert.match($("naviStatus").textContent, /새 경로를 찾지 못해 기존 경로로 계속 안내합니다/);
    assert.ok($("naviStatus").classList.contains("navi-status--warn"));
    assert.match(said(base), /새 경로를 찾지 못해 기존 경로로 계속 안내합니다/);
  });
  check("재탐색이 끝나면 '진행 중' 표시가 풀린다", () => assert.equal(N().rerouting(), false));

  // (나) 쿨다운(30초) 뒤 다시 시도 — 이번에는 통신 예외
  w.__planHook = async () => { throw new Error("network down"); };
  base = (w.__spoken || []).length; cnt = plans().length;
  // 실패 뒤에는 평소 쿨다운(30초)만 지나서는 다시 요청하지 않는다 — 실패 안내가 30초마다 되풀이되지 않게
  vnow += 31000; offRoute(); await sleep(150);
  check("재탐색 실패 직후에는 30초가 지나도 다시 요청하지 않는다", () => assert.equal(plans().length, cnt));
  vnow += 60000; offRoute(); await sleep(150);
  check("실패 뒤 약 90초가 지나면 재탐색을 다시 시도한다", () => assert.equal(plans().length, cnt + 1));
  check("재탐색 실패(통신 예외) — 기존 경로·스텝·안내 상태 유지", () => stillGuiding("통신 예외"));
  check("통신 예외도 상태 줄과 음성으로 알린다", () => {
    assert.match($("naviStatus").textContent, /새 경로를 찾지 못해 기존 경로로 계속 안내합니다/);
    assert.match(said(base), /새 경로를 찾지 못해 기존 경로로 계속 안내합니다/);
  });

  // (다) 응답을 기다리는 동안 — 기존 안내 유지 · 중복 요청 없음 · 새 경로가 오면 교체하고 안내를 잇는다
  let release = null;
  w.__planHook = () => new Promise((r) => { release = r; });
  base = (w.__spoken || []).length; cnt = plans().length;
  vnow += 91000; offRoute(); await sleep(50);   // 직전이 실패였으므로 실패 뒤 대기 시간(약 90초)을 넘긴다
  check("응답 대기 중: 요청 1건 · 진행 표시 · 기존 안내 유지", () => {
    assert.equal(plans().length, cnt + 1); assert.equal(N().rerouting(), true);
    assert.ok($("naviStatus").classList.contains("navi-status--busy"), "진행 표시 없음");
    stillGuiding("대기 중");
  });
  vnow += 31000; offRoute(); await sleep(50);
  check("응답 대기 중에는 쿨다운이 지나도 재탐색을 겹쳐 보내지 않는다", () => assert.equal(plans().length, cnt + 1));
  const NEW_ROUTE = JSON.parse(JSON.stringify(ROUTE)); NEW_ROUTE.route_id = "r_new"; NEW_ROUTE.ui_action.route.route_id = "r_new";
  NEW_ROUTE.ui_action.route.routes[0].steps[0].instruction = "새로 찾은 길로 90m 앞으로 이동합니다.";
  if (release) release({ ok: true, json: async () => NEW_ROUTE });
  await sleep(150);
  check("새 경로를 받으면 교체하고 안내를 자동으로 잇는다", () => {
    assert.equal(w.NAVI.isBusy(), true); assert.equal(N().rerouting(), false);
    assert.match(N().steps[0].instruction, /새로 찾은 길/);
    assert.match(said(base), /새로 찾은 길로 90m/);
    assert.ok(!$("naviStatus").classList.contains("navi-status--busy"));
  });

  // (라) 응답을 기다리는 사이 이용자가 안내를 끝냄 — 늦게 온 경로로 화면을 바꾸지 않는다
  release = null;
  w.__planHook = () => new Promise((r) => { release = r; });
  vnow += 31000; offRoute(); await sleep(50);
  const pendingReroute = !!release;
  w.history.back(); await sleep(60);
  $("naviEndConfirmBtn").click(); await sleep(60);
  if (release) release({ ok: true, json: async () => ROUTE });
  await sleep(150);
  check("안내를 끝낸 뒤 늦게 온 재탐색 결과는 버린다", () => {
    assert.ok(pendingReroute, "재탐색 요청이 나가지 않음");
    assert.equal(w.NAVI.isBusy(), false); assert.equal(N().routeLines().length, 0); assert.equal(N().steps.length, 0);
  });
  w.close();
}

// ───────── 10) 말로 요청한 화장실·식당·긴급지원 — 시트를 바로 연다 ─────────
{
  const w = boot();
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(200);
  const N = () => w.NAVI._internals();
  w.document.querySelector('[data-go="navi"]').click(); await sleep(80);
  const s0 = w.__sockets[0];
  await s0._open(); await sleep(30);
  w.__watch({ coords: { latitude: P(0)[0], longitude: P(0)[1], accuracy: 5 } });
  await s0._msg({ type: "ui_action", action: "show_toilets", payload: { action: "show_toilets", payload: { items: [
    { name: "역 앞 공중화장실", type: "공중화장실", dist_m: 80, accessible: true, open_time: "24시간", lat: 37.3901, lng: 126.9501 } ] } } });
  await sleep(30);
  check("이동경로 안내: 말로 요청한 화장실 결과는 시트가 바로 열린다", () => {
    assert.equal($("sosSheet").hidden, false, "시트가 열리지 않음");
    assert.equal($("sosTabs").querySelector("button[data-kind='toilet']").getAttribute("aria-pressed"), "true");
    assert.match($("sosList").textContent, /역 앞 공중화장실/);
    assert.ok($("view-navi").classList.contains("active"));
  });
  check("갈 수 없는 상담 화면에 '보기' 버튼을 만들지 않는다", () => assert.equal(w.document.querySelectorAll("#chat .navjump").length, 0));
  $("sosCancelBtn").click(); await sleep(20);
  // 안내 주행 중
  await N().requestRoute({ poi_id: "T1", name: "테스트 박물관" }); await sleep(30);
  N().startGuidance(); await sleep(30);
  await s0._msg({ type: "ui_action", action: "show_restaurants", payload: { action: "show_restaurants", payload: { items: [
    { name: "경사로식당", addr: "안양시 1", dist_m: 220, cuisine: "한식", entry_status: "yes", entry_label: "접근로·경사로 확인",
      facilities: ["접근로·경사로"], lat: 37.393, lng: 126.95 } ], total: 1, confirmed: 1 } } });
  await sleep(30);
  check("안내 중에 식당 결과가 와도 시트가 열리고 안내는 계속된다", () => {
    assert.equal($("sosSheet").hidden, false);
    assert.equal($("sosTabs").querySelector("button[data-kind='food']").getAttribute("aria-pressed"), "true");
    assert.match($("sosList").textContent, /경사로식당/);
    assert.equal(w.NAVI.isBusy(), true, "안내가 끊김"); assert.equal(N().steps.length, 4); assert.ok(N().routeLines().length > 0);
  });
  N().stopSpeak();   // 출발 안내 문장을 마친 상태로 맞춘다(재생 중이면 다음 안내가 대기열에서 기다린다)
  const base = (w.__spoken || []).length;
  w.__watch({ coords: { latitude: P(1)[0], longitude: P(1)[1], accuracy: 5 } }); await sleep(300);
  check("시트가 열려 있어도 다음 안내 지점에서 스텝이 넘어가고 음성 안내가 나온다", () => {
    assert.equal(N().stepIdx, 1);
    assert.match((w.__spoken || []).slice(base).join(" | "), /안양로/);
  });
  await s0._msg({ type: "ui_action", action: "show_support", payload: { action: "show_support", payload: { types: "charge", items: [
    { support_type: "charge", name: "만안구청 충전소", dist_m: 180, lat: 37.3866, lng: 126.9324 } ] } } });
  await sleep(30);
  check("긴급지원(충전소) 결과도 같은 시트에서 바로 보인다", () => {
    assert.equal($("sosSheet").hidden, false);
    assert.equal($("sosTabs").querySelector("button[data-kind='charge']").getAttribute("aria-pressed"), "true");
    assert.match($("sosList").textContent, /만안구청 충전소/);
    assert.equal(w.document.querySelectorAll("#chat .navjump").length, 0);
  });
  w.close();
}
{
  // 정책상담 화면은 종전대로 — 이동경로 안내로 넘기는 버튼만 만든다
  const w = boot(null, "https://example.test/policy");
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150);
  w.document.querySelector('[data-go="text"]').click(); await sleep(50);
  await w.__sockets[0]._open(); await sleep(20);
  await w.__sockets[0]._msg({ type: "ui_action", action: "show_toilets", payload: { action: "show_toilets", payload: { items: [
    { name: "역 앞 공중화장실", dist_m: 80, lat: 37.3901, lng: 126.9501 } ] } } });
  await sleep(30);
  check("정책상담: 화장실 결과는 종전대로 '이동경로 안내 열기' 버튼 · 시트는 열지 않는다", () => {
    const btns = [...w.document.querySelectorAll("#chat .navjump")];
    assert.equal(btns.length, 1); assert.match(btns[0].textContent, /이동경로 안내 열기/);
    assert.equal($("sosSheet").hidden, true); assert.ok($("view-chat").classList.contains("active"));
  });
  w.close();
}

// ───────── 11) 측위 상태 줄 · 위치 오류 문구 · '목록으로' 경로선 정리 ─────────
{
  const AREA = { ...CONFIG, service_area: { region: "안양시", bbox: { min_lat: 37.357, min_lng: 126.8775, max_lat: 37.449, max_lng: 126.9819 } } };
  const w = boot((win) => { win.__cfg = AREA; });
  const $ = (id) => w.document.getElementById(id);
  await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(200);
  const N = () => w.NAVI._internals();
  const st = () => $("naviStatus");
  const fixIn = () => w.__watch({ coords: { latitude: P(0)[0], longitude: P(0)[1], accuracy: 5 } });
  const fixOut = () => w.__watch({ coords: { latitude: 37.5665, longitude: 126.9780, accuracy: 5 } });   // 서울시청

  // 위치 오류 — 아직 위치를 한 번도 못 잡은 상태
  w.__watchErr({ code: 3, message: "Timeout expired" });
  check("위치 시간 초과(code 3) — '찾는 중'으로 안내하고 권한 문제로 말하지 않는다", () => {
    assert.match(st().textContent, /현재 위치를 찾는 중입니다/); assert.match(st().textContent, /실내나 역 안/);
    assert.doesNotMatch(st().textContent, /권한/);
  });
  w.__watchErr({ code: 2, message: "Position unavailable" });
  check("위치 확인 불가(code 2) — 권한 문제로 말하지 않는다", () => {
    assert.match(st().textContent, /현재 위치를 확인할 수 없습니다/); assert.doesNotMatch(st().textContent, /권한/);
  });
  w.__watchErr({ code: 1, message: "User denied Geolocation" });
  check("권한 거부(code 1) — 권한 허용을 안내한다", () => assert.match(st().textContent, /위치 권한이 없어 .* 권한을 허용해 주세요/));

  w.document.querySelector('[data-go="navi"]').click(); await sleep(80);
  await w.__sockets[0]._open(); await sleep(30);
  fixIn();
  check("첫 측위 성공 — '현재 위치를 확인했습니다'", () => assert.equal(st().textContent, "현재 위치를 확인했습니다."));
  // 경로 실패 사유(경고 표시)가 떠 있는 동안 측위가 이어진다
  w.__planHook = async () => ({ ok: true, json: async () => ({ status: "error", message: "경로를 만들지 못했습니다." }) });
  await N().requestRoute({ poi_id: "T1", name: "테스트 박물관" }); await sleep(30);
  const failText = st().textContent;
  check("(준비) 경로 실패 사유가 경고로 표시됨", () => { assert.match(failText, /경로를 만들지 못했습니다/); assert.ok(st().classList.contains("navi-status--warn")); });
  fixIn(); fixIn(); fixIn();
  check("이어지는 측위가 상태 줄 문구·경고 표시를 덮어쓰지 않는다", () => {
    assert.equal(st().textContent, failText); assert.ok(st().classList.contains("navi-status--warn"));
  });
  w.__watchErr({ code: 3, message: "Timeout expired" });
  check("위치를 잡은 뒤의 시간 초과는 상태 줄을 건드리지 않는다(마지막 위치로 계속 동작)", () => assert.equal(st().textContent, failText));
  fixOut();
  check("범위 밖으로 나가면 범위 안내로 바뀐다", () => assert.match(st().textContent, /안양시 지역만 안내합니다/));
  fixIn();
  check("범위 밖 → 안으로 돌아오면 다시 한 번 알리고 경고를 푼다", () => {
    assert.equal(st().textContent, "현재 위치를 확인했습니다."); assert.ok(!st().classList.contains("navi-status--warn"));
  });

  // 경로 요약 '목록으로' — 구간별 경로선(도보·버스·도보 3줄)을 모두 지운다
  const MM = JSON.parse(JSON.stringify(ROUTE));
  MM.ui_action.route.routes[0].legs = [
    { kind: "walk", to_label: "정류장", summary: { total_distance_m: 110, duration_sec: 90 }, geometry: [P(0), P(1)] },
    { kind: "bus", route: { route_id: "1", name: "2", type: "마을버스" }, board: { name: "가" }, alight: { name: "나" }, stop_cnt: 3, warnings: [], geometry: [P(1), P(7)] },
    { kind: "walk", to_label: "목적지", summary: { total_distance_m: 110, duration_sec: 90 }, geometry: [P(7), P(8)] } ];
  w.__planHook = async () => ({ ok: true, json: async () => MM });
  await N().requestRoute({ poi_id: "T1", name: "테스트 박물관" }); await sleep(30);
  check("(준비) 구간별 경로선 3줄이 그려짐", () => assert.equal(N().routeLines().length, 3));
  const back = [...$("naviSheetBody").querySelectorAll("button")].find((b) => b.textContent === "목록으로");
  back.click(); await sleep(30);
  check("'목록으로' — 구간 경로선을 전부 지운다", () => assert.equal(N().routeLines().length, 0));
  w.close();
}

// ───────── 소스 레벨 가드 ─────────
check("연결 준비 중 세션이 끝나면 뒤늦게 소켓을 열지 않는다(세대 번호)", () => {
  assert.match(HTML, /if \(gen !== wsGen \|\| ws\) \{ wsConnecting = false; return; \}/);
  assert.match(HTML, /wsGen\+\+;/);
});
check("답변 음성이 시작되면 '지금 말하기' 개방을 끝낸다", () => assert.match(HTML, /micForceUntil = 0;   \/\/ 답변 음성이 시작되면/));
check("조사: 숫자·받침", () => {
  assert.match(HTML, /"013678"\.indexOf\(ch\)/);
});

let failed = 0;
for (const [st, name] of results) { console.log(`${st === "PASS" ? "  ok" : "FAIL"}  ${name}`); if (st === "FAIL") failed++; }
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
