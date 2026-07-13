/**
 * accessistant.html 런타임 검증 (jsdom)
 *
 * 구문 검사(node --check)만으로는 화면 전환·경로 렌더·스텝 진행 회귀를 잡지 못한다.
 * 실제 DOM 위에서 다음을 실행해 확인한다.
 *   1) 기능 플래그가 켜지면 '이동·관광' 진입점이 노출된다
 *   2) 무장애 관광지 목록이 카드로 렌더된다
 *   3) 관광지를 선택하면 경로 요약(거리·시간·최대경사·계단)이 표시된다
 *   4) 안내 시작 -> 턴바이턴 스텝 카드 + 음성 발화
 *   5) 다음 안내 지점에 근접하면 스텝이 자동 전환된다
 *   6) 상담 세션의 ui_action(show_route)으로 지도 화면이 열린다
 *   7) 답변 텍스트가 제목·항목·유의사항 카드로 파싱된다
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

const HTML = readFileSync(new URL("../../static/accessistant.html", import.meta.url), "utf8");

const SPOTS = {
  status: "success",
  results: [
    { poi_id: "TBF-1", name: "테스트 무장애 공원", addr: "경기도 안양시 만안구 1",
      facilities: ["장애인 화장실", "엘리베이터", "경사로"], score: 0.9 },
    { poi_id: "TBF-2", name: "안양예술공원", addr: "경기도 안양시 만안구 2",
      facilities: ["경사로"], score: 0.5 },
  ],
};

const ROUTE = {
  status: "success",
  route_id: "r_test",
  ui_action: {
    action: "show_route",
    route: {
      route_id: "r_test",
      fallback: { used: true, reason: "권장 경사를 만족하는 경로가 없어 6도까지 완화했습니다" },
      routes: [{
        summary: { total_distance_m: 320, duration_sec: 457, max_slope_deg: 3.6,
                   stairs_cnt: 0, crossing_cnt: 2, warnings: ["턱낮춤 없는 횡단보도 구간이 있습니다"] },
        geometry: [[37.3900, 126.9500], [37.3905, 126.9505], [37.3909, 126.9511]],
        steps: [
          { idx: 0, maneuver: "depart", instruction: "중앙로를 따라 120m 앞으로 이동합니다.",
            distance_m: 120, coord: [37.3900, 126.9500], warnings: [] },
          { idx: 1, maneuver: "crossing", instruction: "횡단보도를 건너 60m 이동합니다.",
            distance_m: 60, coord: [37.3905, 126.9505], warnings: ["턱낮춤 없음"] },
          { idx: 2, maneuver: "arrive", instruction: "목적지에 도착했습니다.",
            distance_m: 0, coord: [37.3909, 126.9511], warnings: [] },
        ],
      }],
    },
  },
};

const CONFIG = {
  kakao_js_key: "test-key",
  features: { route: true, tour: true },
  map: { default_center: { lat: 37.3943, lng: 126.9568 }, default_level: 5 },
};

function fakeKakao() {
  const noop = () => {};
  class LatLng { constructor(a, b) { this.a = a; this.b = b; } }
  class Bounds { extend() {} }
  const mk = () => ({ setMap: noop, setPosition: noop });
  return {
    maps: {
      load: (cb) => cb(),
      Map: class { constructor() {} setBounds() {} },
      LatLng,
      LatLngBounds: Bounds,
      Marker: function () { return mk(); },
      Circle: function () { return mk(); },
      Polyline: function () { return mk(); },
      event: { addListener: noop },
    },
  };
}

const spoken = [];
const results = [];
function check(name, fn) {
  try { fn(); results.push(["PASS", name]); }
  catch (e) { results.push(["FAIL", name + " — " + e.message]); }
}

const dom = new JSDOM(HTML, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://example.test/static/accessistant.html" });
const { window } = dom;

// ── 외부 의존성 스텁 ──
window.fetch = async (url) => {
  const u = String(url);
  const body = u.includes("/api/v1/config") ? CONFIG
    : u.includes("find_bf_tour_spots") ? SPOTS
    : u.includes("plan_accessible_route") ? ROUTE
    : {};
  return { ok: true, json: async () => body };
};
let watchCb = null;
window.navigator.geolocation = {
  watchPosition: (ok) => { watchCb = ok; return 1; },
  clearWatch: () => {},
};
window.SpeechSynthesisUtterance = function (t) { this.text = t; };
window.speechSynthesis = {
  cancel: () => {},
  speak: (u) => { spoken.push(u.text); if (u.onend) u.onend(); },
};
window.kakao = fakeKakao();
// 카카오 SDK <script> 는 jsdom 이 로드하지 않으므로 onload 를 직접 발화시킨다
const origAppend = window.document.head.appendChild.bind(window.document.head);
window.document.head.appendChild = (el) => {
  const r = origAppend(el);
  if (el.tagName === "SCRIPT" && String(el.src).includes("dapi.kakao.com") && el.onload) {
    setTimeout(() => el.onload(), 0);
  }
  return r;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await sleep(50);
window.document.dispatchEvent(new window.Event("DOMContentLoaded"));
await sleep(120);

const $ = (id) => window.document.getElementById(id);

// 1) 진입점 노출
check("기능 플래그 ON -> '이동·관광' 모드 버튼 노출", () => {
  assert.equal($("modeNaviBtn").style.display, "");
  assert.equal($("chatModebar").style.display, "");
});

// 위치 확보
watchCb({ coords: { latitude: 37.3900, longitude: 126.9500 } });
await sleep(20);
check("현재 위치 확인 상태 표시", () => {
  assert.match($("naviStatus").textContent, /현재 위치를 확인했습니다/);
});

// 2) 관광지 목록
window.NAVI.openNavi();
await sleep(60);
check("무장애 관광지 목록 렌더", () => {
  const spots = window.document.querySelectorAll("#naviSpots .spot");
  assert.equal(spots.length, 2);
  assert.match(spots[0].textContent, /테스트 무장애 공원/);
  assert.match(spots[0].textContent, /엘리베이터/);
});
check("이동·관광 화면 활성화", () => {
  assert.ok($("view-navi").classList.contains("active"));
});

// 3) 관광지 선택 -> 경로 요약
window.document.querySelectorAll("#naviSpots .spot")[0].dispatchEvent(new window.Event("click"));
await sleep(80);
check("경로 요약(거리·시간·최대경사·계단) 표시", () => {
  const t = $("naviSheetBody").textContent;
  assert.match(t, /320m/);
  assert.match(t, /8분/);          // 457초 -> 8분
  assert.match(t, /3\.6°/);
  assert.match(t, /0곳/);
});
check("경고·제약 완화 사유를 사용자에게 고지", () => {
  const t = $("naviSheetBody").textContent;
  assert.match(t, /턱낮춤 없는 횡단보도/);
  assert.match(t, /6도까지 완화/);
});

// 4) 안내 시작 -> 스텝 + 음성
const startBtn = [...$("naviSheetBody").querySelectorAll("button")].find((b) => b.textContent === "안내 시작");
check("'안내 시작' 버튼 존재", () => assert.ok(startBtn));
startBtn.dispatchEvent(new window.Event("click"));
await sleep(30);
check("첫 턴바이턴 스텝 카드 + 음성 발화", () => {
  const card = window.document.querySelector(".step-now .ins");
  assert.match(card.textContent, /중앙로를 따라 120m/);
  assert.equal(spoken.at(-1), "중앙로를 따라 120m 앞으로 이동합니다.");
  assert.equal(window.document.querySelector(".step-now").getAttribute("aria-live"), "assertive");
});

// 5) 다음 지점 근접 -> 스텝 자동 전환
watchCb({ coords: { latitude: 37.39051, longitude: 126.95051 } });
await sleep(30);
check("다음 안내 지점 근접 시 스텝 자동 전환 + 재발화", () => {
  const card = window.document.querySelector(".step-now .ins");
  assert.match(card.textContent, /횡단보도를 건너/);
  assert.equal(spoken.at(-1), "횡단보도를 건너 60m 이동합니다.");
  assert.match(window.document.querySelector(".step-now .wr").textContent, /턱낮춤 없음/);
});

// 6) 상담 세션 ui_action -> 지도 화면 자동 전환
window.NAVI.onUiAction({ type: "ui_action", action: "show_route", payload: ROUTE.ui_action });
await sleep(20);
check("ui_action(show_route) 수신 시 지도 화면 전환", () => {
  assert.ok($("view-navi").classList.contains("active"));
  assert.match($("naviSheetBody").textContent, /320m/);
});

// 7) 답변 카드 렌더러
const target = window.document.createElement("div");
window.renderAnswerCard(target, [
  "장애인 지하철 요금은 **전액 면제**입니다.",
  "",
  "## 필요 서류",
  "- 복지카드",
  "- 신분증",
  "※ 동반 보호자 1인까지 함께 면제됩니다.",
].join("\n"), { title: "지하철 요금 감면" });
check("답변 카드: 제목·강조·체크항목·유의사항 파싱", () => {
  assert.equal(target.querySelector(".c-title").textContent, "지하철 요금 감면");
  assert.equal(target.querySelector("mark").textContent, "전액 면제");
  assert.equal(target.querySelector(".c-sub").textContent, "필요 서류");
  assert.equal(target.querySelectorAll("li").length, 2);
  assert.match(target.querySelector(".c-note").textContent, /동반 보호자/);
});
check("답변 카드: HTML 주입 방지(이스케이프)", () => {
  const t2 = window.document.createElement("div");
  window.renderAnswerCard(t2, "<img src=x onerror=alert(1)>");
  assert.equal(t2.querySelectorAll("img").length, 0);
});

// ── 결과 ──
let failed = 0;
for (const [st, name] of results) {
  console.log(`${st === "PASS" ? "  ok" : "FAIL"}  ${name}`);
  if (st === "FAIL") failed++;
}
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
