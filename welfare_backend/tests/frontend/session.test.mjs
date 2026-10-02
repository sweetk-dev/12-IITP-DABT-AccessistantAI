/**
 * 세션 유지 런타임 검증 (jsdom) — v1.59.0
 *
 *   1) 연결이 끊기면 화면을 건드리지 않고 같은 sid + resume=1 로 다시 붙는다
 *   2) 이용자가 끝내거나 서버가 끝내면 다시 붙지 않는다
 *   3) 상담·안내 중 뒤로 가기는 페이지를 떠나지 않고 종료 확인을 띄운다
 *   4) 안내 도중 닫혔다 다시 열면 이어서 안내할지 묻는다
 *   5) 안내 재개 문장 — 구간 중간이면 "계속 직진", 끝이면 다음 안내
 *   6) '지금 말하기' 는 마이크 차단을 잠시 푼다
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

function boot(preset) {
  const dom = new JSDOM(HTML, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://example.test/navi",
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
        const body = u.includes("/api/v1/config") ? CONFIG : u.includes("find_bf_tour_spots") ? SPOTS : u.includes("plan_accessible_route") ? ROUTE : {};
        (w.__fetchLog = w.__fetchLog || []).push(u);
        return { ok: true, json: async () => body }; };
      w.navigator.geolocation = { watchPosition: (ok) => { w.__watch = ok; return 1; }, clearWatch: () => {} };
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
  const w = boot();
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
  const w = boot();
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
  const w = boot();
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
  check("확인하면 안내·세션 종료 + 저장된 여정 삭제", () => { assert.equal(w.NAVI.isBusy(), false); assert.equal(w.localStorage.getItem("acc_trip_v1"), null); assert.ok($("view-mode").classList.contains("active")); });
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
