/**
 * 앱 지도 어댑터(NMAP, v2.11.0 #363) 런타임 검증 (jsdom)
 *
 * 이동경로 안내 앱 안에서는 길안내 지도 칸을 앱 지도(카카오맵 Android SDK)로 그린다.
 * 화면은 지도 호출을 명령(JSON)으로 바꿔 AccessNaviApp.mapCmd 로 보내고, 앱이 보낸 사건(__NMAP.emit)을 받는다.
 * 여기서는 가짜 AccessNaviApp 으로 명령·사건 흐름과 되돌리기(웹 지도 복귀)를 확인한다.
 *   실행: node nmap.test.mjs
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

const HTML = readFileSync(new URL("../../static/accessistant.html", import.meta.url), "utf8");
const results = [];
function check(name, fn) {
  try { fn(); results.push(["PASS", name]); } catch (e) { results.push(["FAIL", name + " — " + e.message]); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CONFIG = (nativeMap) => ({
  kakao_js_key: "test-key",
  features: { route: true, tour: true },
  service_area: { region: "안양시", bbox: { min_lat: 37.357, min_lng: 126.8775, max_lat: 37.449, max_lng: 126.9819 } },
  map: { default_center: { lat: 37.3943, lng: 126.9568 }, default_level: 5, native_map: nativeMap },
});
const SPOTS = { status: "success", results: [
  { poi_id: "TBF-1", name: "테스트 무장애 공원", addr: "안양시 1", facilities: ["경사로"], score: 0.9, lat: 37.391, lng: 126.951 },
] };
const ROUTE = {
  route_id: "r_nm", destination: { poi_id: "TBF-1" }, fallback: { used: false },
  routes: [{
    summary: { total_distance_m: 180, duration_sec: 200, max_slope_deg: 2, stairs_cnt: 0, crossing_cnt: 1, warnings: [] },
    geometry: [[37.3900, 126.9500], [37.3905, 126.9505], [37.3909, 126.9511]],
    crosswalks: [{ lat: 37.3905, lng: 126.9505, role: "cross", length_m: 12 }, { lat: 37.3907, lng: 126.9509, role: "near" }],
    steps: [
      { idx: 0, maneuver: "depart", instruction: "앞으로 이동합니다.", distance_m: 80, coord: [37.3900, 126.9500], warnings: [] },
      { idx: 1, maneuver: "arrive", instruction: "도착했습니다.", distance_m: 0, coord: [37.3909, 126.9511], warnings: [] },
    ],
  }],
};

// 웹 지도 SDK 가짜 — 되돌리기(웹 지도 복귀)에서 실제로 쓰였는지 센다
function fakeKakao(stats) {
  const mk = (kind) => function () { stats[kind] = (stats[kind] || 0) + 1; return { setMap() {}, setPosition() {} }; };
  return { maps: {
    load: (cb) => cb(),
    Map: class { constructor() { stats.Map = (stats.Map || 0) + 1; } getLevel() { return 5; } setLevel() {} setBounds() {} setCenter() {} relayout() {} getCenter() { return null; } },
    LatLng: class { constructor(a, b) { this.a = a; this.b = b; } getLat() { return this.a; } getLng() { return this.b; } },
    LatLngBounds: class { extend() {} },
    Marker: mk("Marker"), Circle: mk("Circle"), Polyline: mk("Polyline"),
    CustomOverlay: class { constructor() { stats.CustomOverlay = (stats.CustomOverlay || 0) + 1; } setMap() {} },
    event: { addListener() {} },
    services: { Places: class { keywordSearch(q, cb) { cb([], "ZERO_RESULT"); } }, Status: { OK: "OK" } },
  } };
}

async function boot({ nativeMap = true, app = true } = {}) {
  const cmds = [], rects = [], stats = {}, spoken = [];
  const dom = new JSDOM(HTML, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://example.test/navi",
    beforeParse(win) {
      Object.defineProperty(win.navigator, "userAgent", { value: "Mozilla/5.0 (Linux; Android 15) AccessNaviApp/2.1.0" });
      if (app) win.AccessNaviApp = {
        setBusy() {}, micGranted: () => true, version: () => "2.1.0",
        mapAvailable: () => true,
        mapCmd: (j) => { JSON.parse(j).forEach((c) => cmds.push(c)); },
        mapRect: (x, y, w, h) => rects.push([x, y, w, h]),
      };
    } });
  const w = dom.window;
  w.fetch = async (url) => {
    const u = String(url);
    const body = u.includes("/api/v1/config") ? CONFIG(nativeMap) : u.includes("find_bf_tour_spots") ? SPOTS : {};
    return { ok: true, json: async () => body };
  };
  let watchCb = null;
  w.navigator.geolocation = { watchPosition: (ok) => { watchCb = ok; return 1; }, clearWatch() {} };
  w.SpeechSynthesisUtterance = function (t) { this.text = t; };
  w.speechSynthesis = { cancel() {}, speak(u) { spoken.push(u.text); if (u.onend) u.onend(); } };
  w.kakao = fakeKakao(stats);
  // 지도 칸 크기 — jsdom 은 배치 계산을 하지 않으므로 지도 칸만 크기를 준다
  const origRect = w.Element.prototype.getBoundingClientRect;
  w.Element.prototype.getBoundingClientRect = function () {
    if (this.id === "naviMap") return { left: 0, top: 60, width: 360, height: 400, right: 360, bottom: 460 };
    return origRect.call(this);
  };
  w.Element.prototype.getClientRects = function () { return this.hidden || this.closest("[hidden]") ? [] : [1]; };
  await sleep(30);
  w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
  await sleep(120);
  return { w, cmds, rects, stats, spoken, fix: (lat, lng) => watchCb && watchCb({ coords: { latitude: lat, longitude: lng, accuracy: 5 } }) };
}
const ops = (cmds, op) => cmds.filter((c) => c.op === op);

// ── 1) 앱 안 + 서버 설정 on → 앱 지도 ──
const A = await boot();
const $ = (id) => A.w.document.getElementById(id);
const NV = A.w.NAVI._internals();
A.w.NAVI.openNavi();
await sleep(40);
$("naviTypeGo").dispatchEvent(new A.w.Event("click"));   // 장애 유형 게이트 → 관광지 목록·태그
await sleep(80);

check("앱 지도 사용 — html.nmap 배치·init 명령", () => {
  assert.equal(NV.nmap().on, true);
  assert.ok(A.w.document.documentElement.classList.contains("nmap"));
  const init = ops(A.cmds, "init")[0];
  assert.ok(init, "init 명령이 없음");
  assert.equal(init.level, 5);
});
check("웹 지도 SDK 의 Map 은 만들지 않는다", () => assert.equal(A.stats.Map || 0, 0));
check("지도 칸 위치를 실제 픽셀로 보고", () => {
  const k = A.w.devicePixelRatio || 1;
  assert.deepEqual(A.rects.at(-1), [0, Math.round(60 * k), Math.round(360 * k), Math.round(400 * k)]);
});

A.fix(37.3900, 126.9500);
await sleep(30);
A.fix(37.3901, 126.9501);
await sleep(30);
check("현재 위치 — me 표시 1개 + 위치 이동", () => {
  const me = ops(A.cmds, "label").filter((c) => c.kind === "me");
  assert.equal(me.length, 1);
  assert.ok(ops(A.cmds, "move").some((c) => c.id === me[0].id && Math.abs(c.lat - 37.3901) < 1e-9));
});

check("관광지 이름 태그 — 누를 수 있는 tag 표시", () => {
  const tags = ops(A.cmds, "label").filter((c) => c.kind === "tag");
  assert.ok(tags.length >= 1, "태그 없음");
  assert.equal(tags[0].clickable, true);
  assert.match(tags[0].text, /테스트 무장애 공원/);
});

const before = A.cmds.length;
NV.showRoute(ROUTE, "테스트 무장애 공원");
await sleep(40);
const after = A.cmds.slice(before);
check("경로 표시 — 경로선·범위 맞춤·횡단보도 2종", () => {
  const ln = ops(after, "line");
  assert.equal(ln.length, 1);
  assert.equal(ln[0].pts.length, 3);
  assert.equal(ln[0].dash, false);
  assert.ok(ops(after, "bounds").length >= 1, "범위 맞춤 없음");
  const kinds = ops(after, "label").map((c) => c.kind);
  assert.ok(kinds.includes("cw") && kinds.includes("cw-near"), "횡단보도 표시: " + kinds.join(","));
});

check("관광지 태그를 앱에서 누르면 웹 태그의 처리(선택 창)가 열린다", () => {
  const tag = ops(A.cmds, "label").filter((c) => c.kind === "tag").at(-1);
  A.w.__NMAP.emit("label", { id: tag.id });
  const pop = A.w.document.querySelector('[role="dialog"][aria-modal="true"]');
  assert.ok(pop && !pop.closest("[hidden]"), "선택 창이 열리지 않음");
});
await sleep(60);
check("팝업이 떠 있으면 지도 칸을 0 으로 보고(앱 지도 숨김)", () => assert.deepEqual(A.rects.at(-1), [0, 0, 0, 0]));
NV.cancelArrivalReset();   // 선택 창 닫기
await sleep(60);
check("팝업이 닫히면 지도 칸을 다시 보고", () => assert.ok(A.rects.at(-1)[2] > 0, JSON.stringify(A.rects.at(-1))));

NV.startGuidance();
await sleep(30);
A.fix(37.3903, 126.9503);
await sleep(30);
check("안내 중 위치 갱신 → follow(진행 방향 회전) 명령", () => {
  const f = ops(A.cmds, "follow").at(-1);
  assert.ok(f, "follow 없음");
  assert.ok(Math.abs(f.lat - 37.3903) < 1e-9);
});

A.w.__NMAP.emit("dragstart", {});
await sleep(10);
check("앱 지도를 끌면 따라가기 해제·목록 접기", () => {
  assert.equal(NV.followMe(), false);
  assert.ok($("naviSheet").classList.contains("collapsed"));
});

A.w.__NMAP.emit("camera", { lat: 37.39, lng: 126.95, level: 3 });
check("카메라 사건으로 확대 단계·중심 갱신", () => {
  assert.equal(NV.mapObj().getLevel(), 3);
  assert.ok(Math.abs(NV.mapObj().getCenter().getLat() - 37.39) < 1e-9);
});

const lineId = ops(A.cmds, "line").at(-1).id;
NV.clearRouteDisplay();
await sleep(30);
check("경로 지우기 → 경로선 remove 명령", () => assert.ok(ops(A.cmds, "remove").some((c) => c.id === lineId)));

A.w.__NMAP.emit("click", { lat: 37.3920, lng: 126.9520 });
await sleep(20);
check("앱 지도 빈 곳 누르기 → 웹 지도와 같은 처리(도착지가 있으면 출발지 변경 확인 창)", () => {
  const pop = [...A.w.document.querySelectorAll('[role="dialog"][aria-modal="true"]')].find((d) => !d.closest("[hidden]"));
  assert.ok(pop, "확인 창 없음");
  assert.match(pop.textContent, /출발지를 변경할까요/);
});
[...A.w.document.querySelectorAll("button")].find((b) => b.textContent.includes("이 지점을 출발지로")).dispatchEvent(new A.w.Event("click"));
await sleep(30);
check("출발지 지정 → 초록 origin 표시", () => {
  assert.ok(ops(A.cmds, "label").some((c) => c.kind === "origin" && Math.abs(c.lat - 37.392) < 1e-9));
});

// 앱 지도 오류 → 웹 지도 복귀
NV.showRoute(ROUTE, "테스트 무장애 공원");
await sleep(30);
A.w.__NMAP.emit("error", { code: 401 });
await sleep(30);
check("앱 지도 오류 → 웹 지도로 복귀하고 경로를 다시 그린다", () => {
  assert.equal(NV.nmap().on, false);
  assert.ok(!A.w.document.documentElement.classList.contains("nmap"));
  assert.equal(A.stats.Map, 1, "웹 지도 Map 생성 수");
  assert.ok((A.stats.Polyline || 0) >= 1, "웹 지도 경로선 다시 그리기");
  assert.deepEqual(A.rects.at(-1), [0, 0, 0, 0]);
});

// ── 중간 안내 거리는 경로를 따라 잰다 (직선거리로 재면 돌아가는 길에서 틀린다) ──
{
  const D = await boot({ nativeMap: false });
  const NI = D.w.NAVI._internals();
  D.w.NAVI.openNavi();
  await sleep(40);
  D.fix(37.3900, 126.9500);
  await sleep(30);
  // U자 경로: 북쪽 400m → 동쪽 약 106m → 남쪽 400m. 출발점과 도착점은 직선으로 약 106m, 길로는 약 900m
  const U = { route_id: "r_u", destination: { poi_id: "TBF-1" }, fallback: { used: false },
    routes: [{ summary: { total_distance_m: 906, duration_sec: 900, max_slope_deg: 1, stairs_cnt: 0, crossing_cnt: 0, warnings: [] },
      geometry: [[37.3900, 126.9500], [37.3936, 126.9500], [37.3936, 126.9512], [37.3900, 126.9512]],
      steps: [
        { idx: 0, maneuver: "depart", instruction: "북쪽으로 이동합니다.", distance_m: 906, coord: [37.3900, 126.9500], warnings: [] },
        { idx: 1, maneuver: "arrive", instruction: "도착했습니다.", distance_m: 0, coord: [37.3900, 126.9512], warnings: [] },
      ] }] };
  NI.showRoute(U, "U자 경로");
  await sleep(60);
  const go = [...D.w.document.querySelectorAll("#naviSheetBody button")].find((b) => b.textContent === "안내 시작");
  if (go) go.dispatchEvent(new D.w.Event("click")); else NI.startGuidance();
  await sleep(2500);   // 첫 안내 문장이 끝날 때까지
  D.spoken.length = 0;
  D.fix(37.3901, 126.9500);   // 출발 직후 — 도착점까지 직선 약 106m, 길로는 약 890m
  await sleep(40);
  check("출발 직후(직선 106m·길 890m) — '다음 안내까지 150미터'를 말하지 않는다", () => {
    assert.ok(!D.spoken.some((t) => /다음 안내까지/.test(t)), D.spoken.join(" | "));
  });
  // 길을 따라 약 20m 간격으로 걷는다(한 번에 멀리 뛰면 측위 튐으로 걸러진다)
  const pts = [];
  for (let la = 37.3903; la <= 37.3936; la += 0.00018) pts.push([la, 126.9500]);
  for (let ln = 126.9502; ln <= 126.9512; ln += 0.00022) pts.push([37.3936, ln]);
  for (let la = 37.3934; la >= 37.39120; la -= 0.00018) pts.push([la, 126.9512]);
  for (const [la, ln] of pts) { D.fix(la, ln); await sleep(15); }
  await sleep(4500);   // 안내 음성은 추정 길이만큼 차례를 기다린다 — 앞 문장이 끝난 뒤 나온다
  check("도착까지 길로 약 140m — '다음 안내까지 약 150미터입니다'", () => {
    assert.ok(D.spoken.some((t) => /다음 안내까지 약 150미터/.test(t)), D.spoken.join(" | "));
  });
}

// ── 대중교통 경로 뒤에 도보 경로(mode 없음)를 받으면 '추천' 카드 이름도 도보로 바뀐다 ──
{
  const E = await boot({ nativeMap: false });
  const NE = E.w.NAVI._internals();
  E.w.NAVI.openNavi();
  await sleep(40);
  E.fix(37.3900, 126.9500);
  await sleep(400);   // 첫 측위 뒤 비동기 화면 갱신(목록 패널)이 끝난 다음 경로를 그린다 — 경로 패널을 덮지 않게
  const geom = [[37.3900, 126.9500], [37.3905, 126.9505], [37.3909, 126.9511]];
  const st = [
    { idx: 0, maneuver: "depart", instruction: "이동합니다.", distance_m: 100, coord: [37.3900, 126.9500], warnings: [] },
    { idx: 1, maneuver: "arrive", instruction: "도착했습니다.", distance_m: 0, coord: [37.3909, 126.9511], warnings: [] }];
  const sum = { total_distance_m: 300, duration_sec: 300, max_slope_deg: 1, stairs_cnt: 0, crossing_cnt: 0, warnings: [] };
  NE.showRoute({ route_id: "r_bus", mode: "walk_bus", destination: { poi_id: "TBF-9" }, fallback: { used: false },
    routes: [{ summary: sum, geometry: geom, steps: st, legs: [
      { kind: "walk", geometry: geom.slice(0, 2), summary: {} },
      { kind: "bus", geometry: geom.slice(1, 3), route: { name: "2" }, board: { name: "가" }, alight: { name: "나" }, stop_cnt: 3 }] }] }, "박물관");
  await sleep(30);
  const autoLabel = () => { const b = E.w.document.querySelector('.mode-cards button[data-mode="auto"] small'); return b ? b.textContent : null; };
  await sleep(40);
  check("대중교통 경로 — '추천' 카드에 도보+버스", () => assert.equal(autoLabel(), "도보+버스"));
  NE.showRoute({ route_id: "r_walk", destination: { poi_id: "TBF-9" }, fallback: { used: false },
    routes: [{ summary: sum, geometry: geom, steps: st }] }, "박물관");
  await sleep(30);
  check("이어서 도보 경로(mode 없음) — '추천' 카드 이름이 도보로 바뀐다", () => assert.equal(autoLabel(), "도보"));
  // 말로 "도보로만" — 상담원 도구가 mode_requested=walk 를 실어 보낸다 → '도보' 카드가 선택된다
  E.w.NAVI.onUiAction({ action: "show_route", payload: { action: "show_route", mode_requested: "walk",
    route: { route_id: "r_walk2", destination: { poi_id: "TBF-9" }, fallback: { used: false },
      routes: [{ summary: sum, geometry: geom, steps: st }] } } });
  await sleep(40);
  const pressed = () => { const b = E.w.document.querySelector('.mode-cards button[aria-pressed="true"]'); return b ? b.getAttribute("data-mode") : null; };
  check("말로 도보 요청 → '도보' 카드 선택", () => assert.equal(pressed(), "walk"));
  E.w.NAVI.onUiAction({ action: "show_route", payload: { action: "show_route", mode_requested: null,
    route: { route_id: "r_walk3", destination: { poi_id: "TBF-9" }, fallback: { used: false },
      routes: [{ summary: sum, geometry: geom, steps: st }] } } });
  await sleep(40);
  check("말로 자동 추천 → '추천' 카드 선택", () => assert.equal(pressed(), "auto"));
}

// ── 손잡이로 목록을 펼친 직후 같은 자리 click 이 펼쳐진 버튼(이전·다음)을 누르지 않는다 ──
{
  const G = await boot({ nativeMap: false });
  const NG = G.w.NAVI._internals();
  G.w.NAVI.openNavi();
  await sleep(40);
  G.fix(37.3900, 126.9500);
  await sleep(400);
  NG.showRoute(ROUTE, "테스트 무장애 공원");
  await sleep(40);
  NG.startGuidance();
  await sleep(60);
  const gd = G.w.document.getElementById("naviGrip");
  const tap = (el, y) => {
    el.dispatchEvent(new G.w.MouseEvent("pointerdown", { clientX: 200, clientY: y, bubbles: true }));
    el.dispatchEvent(new G.w.MouseEvent("pointerup", { clientX: 200, clientY: y, bubbles: true }));
  };
  tap(gd, 700);   // 접기
  await sleep(500);
  const nextBtn = () => [...G.w.document.querySelectorAll("#naviSheetBody button")].find((b) => /다음/.test(b.textContent));
  const idx0 = G.w.NAVI._internals().stepIdx;
  tap(gd, 700);   // 펼치기 — 곧바로 같은 자리 click 이 '다음' 버튼에 떨어진 상황
  nextBtn().dispatchEvent(new G.w.MouseEvent("click", { clientX: 205, clientY: 703, bubbles: true, cancelable: true }));
  await sleep(30);
  check("목록 펼친 직후의 click 은 '다음' 버튼을 누르지 않는다", () => assert.equal(G.w.NAVI._internals().stepIdx, idx0));
  await sleep(450);
  nextBtn().dispatchEvent(new G.w.MouseEvent("click", { bubbles: true, cancelable: true }));
  await sleep(30);
  check("잠시 뒤 일부러 누른 '다음'은 정상 동작", () => assert.equal(G.w.NAVI._internals().stepIdx, idx0 + 1));
}

// ── 모의 주행은 도착 안내 뒤에도 경로 끝까지 움직이고 나서 끝난다 ──
{
  const H = await boot({ nativeMap: true });
  const NH = H.w.NAVI._internals();
  H.w.NAVI.openNavi();
  await sleep(40);
  H.fix(37.3900, 126.9500);
  await sleep(400);
  // 북쪽으로 약 33m 뒤 도착 — 도착 전환 판정 여유는 min(25, max(5, 33×0.5)) ≈ 17m
  const END = [37.3903, 126.9500];
  NH.showRoute({ route_id: "r_sim", destination: { poi_id: "TBF-1" }, fallback: { used: false },
    routes: [{ summary: { total_distance_m: 33, duration_sec: 30, max_slope_deg: 0, stairs_cnt: 0, crossing_cnt: 0, warnings: [] },
      geometry: [[37.3900, 126.9500], END],
      steps: [
        { idx: 0, maneuver: "depart", instruction: "북쪽으로 이동합니다.", distance_m: 33, coord: [37.3900, 126.9500], warnings: [] },
        { idx: 1, maneuver: "arrive", instruction: "도착했습니다.", distance_m: 0, coord: END, warnings: [] }] }] }, "가까운 곳");
  await sleep(60);
  const simBtn = [...H.w.document.querySelectorAll("#naviSheetBody button")].find((b) => b.textContent === "모의 주행");
  simBtn.dispatchEvent(new H.w.Event("click"));
  for (let i = 0; i < 120 && !/모의 주행이 끝났습니다/.test(H.w.document.getElementById("naviStatus").textContent); i++) await sleep(250);
  check("모의 주행 종료 시 주황 점이 도착 지점까지 갔다(앞에서 멈추지 않음)", () => {
    assert.match(H.w.document.getElementById("naviStatus").textContent, /모의 주행이 끝났습니다/);
    const sim = ops(H.cmds, "label").find((c) => c.kind === "sim");
    const last = ops(H.cmds, "move").filter((c) => c.id === sim.id).at(-1);
    const dM = Math.hypot((last.lat - END[0]) * 110540, (last.lng - END[1]) * 88200);
    assert.ok(dM < 3, "도착 지점까지 남은 거리 " + dM.toFixed(1) + "m");
  });
}

// ── 2) 서버 설정 off → 앱 안이어도 웹 지도(되돌리기 스위치) ──
const B = await boot({ nativeMap: false });
check("서버 설정 off → 웹 지도, 앱 명령 없음", () => {
  assert.equal(B.w.NAVI._internals().nmap().on, false);
  assert.equal(B.cmds.length, 0);
  assert.equal(B.stats.Map, 1);
});
// ── 3) 구버전 앱(지도 다리 없음) → 웹 지도 ──
const C = await boot({ app: false });
check("앱 지도 다리가 없으면 웹 지도", () => {
  assert.equal(C.w.NAVI._internals().nmap().on, false);
  assert.equal(C.stats.Map, 1);
});

for (const [st, n] of results) console.log((st === "PASS" ? "  ok  " : "  FAIL ") + n);
const fail = results.filter((r) => r[0] !== "PASS").length;
console.log(`\n${results.length - fail} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
