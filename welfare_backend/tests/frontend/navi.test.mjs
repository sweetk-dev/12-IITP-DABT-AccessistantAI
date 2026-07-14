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
      destination: { resolved_by: "facility_centroid",
                     note: "시설 대표 좌표 기준 — 건물 중심일 수 있으니 도착 후 출입구를 확인하세요" },
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
  service_area: {
    region: "안양시",
    bbox: { min_lat: 37.357, min_lng: 126.8775, max_lat: 37.449, max_lng: 126.9819 },
    network_version: "anyang-osm-dem5-2026Q3",
  },
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
      Map: class {
        constructor() { this.__isMap = true; }
        setBounds(b) { mapCalls.push(["setBounds", b]); }
        setCenter(c) { mapCalls.push(["setCenter", c]); }
        relayout() { mapCalls.push(["relayout"]); }
      },
      LatLng,
      LatLngBounds: Bounds,
      Marker: function () { return mk(); },
      Circle: function () { return mk(); },
      Polyline: function () { return mk(); },
      event: {
        addListener: (target, type, cb) => {
          if (type === "click" && target && target.__isMap) mapClickHandlers.push(cb);
        },
      },
    },
  };
}

const spoken = [];
const mapClickHandlers = [];
const mapCalls = [];
const results = [];
function check(name, fn) {
  try { fn(); results.push(["PASS", name]); }
  catch (e) { results.push(["FAIL", name + " — " + e.message]); }
}

const dom = new JSDOM(HTML, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://example.test/static/accessistant.html" });
const { window } = dom;

// ── 외부 의존성 스텁 ──
let lastPlanQuery = null;
window.fetch = async (url) => {
  const u = String(url);
  if (u.includes("plan_accessible_route")) lastPlanQuery = u;
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

// 1-b) 서비스 지역 밖 -> 안내 + 지도에서 출발지 지정
watchCb({ coords: { latitude: 37.5665, longitude: 126.9780 } });   // 서울시청
await sleep(20);
check("서비스 지역 밖이면 출발지 지정을 안내", () => {
  assert.match($("naviStatus").textContent, /안양시 지역만 안내합니다/);
  assert.match($("naviStatus").textContent, /지도를 눌러 출발지를 지정/);
});

window.NAVI.openNavi();
await sleep(60);
check("지역 밖 안내가 목록 패널에도 표시", () => {
  assert.match($("naviSheetBody").textContent, /현재 위치가 안양시 밖입니다/);
});

check("지도 클릭 핸들러 등록됨", () => assert.ok(mapClickHandlers.length >= 1));

check("서비스 지역 밖에서 지도를 현위치(서울)로 옮기지 않음", () => {
  // 현위치로 중심을 잡으면 안양 관광지 핀이 화면 밖으로 나가 아무것도 못 한다.
  const centers = mapCalls.filter((c) => c[0] === "setCenter").map((c) => c[1]);
  const seoul = centers.filter((c) => c && Math.abs(c.a - 37.5665) < 0.01);
  assert.equal(seoul.length, 0, "서울로 중심 이동함");
});
mapClickHandlers[0]({ latLng: { getLat: () => 37.3943, getLng: () => 126.9568 } });  // 안양시청
await sleep(30);
check("지도 클릭으로 출발지 지정", () => {
  assert.match($("naviStatus").textContent, /출발지를 지정했습니다/);
  assert.match($("naviSheetBody").textContent, /지도에서 지정한 출발지에서 안내합니다/);
});

check("지역 밖 지점은 출발지로 거부", () => {
  mapClickHandlers[0]({ latLng: { getLat: () => 37.5665, getLng: () => 126.9780 } });
  assert.match($("naviStatus").textContent, /출발지는 안양시 안에서 선택해 주세요/);
});

// 현재 위치를 안양 안으로 되돌린 뒤 이후 시나리오 진행
watchCb({ coords: { latitude: 37.3900, longitude: 126.9500 } });
await sleep(20);

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

check("지도 로드 성공 시 폴백 오버레이가 숨겨짐", () => {
  // hidden 속성만으로는 부족하다 — CSS 클래스(display:flex)가 명시도에서 이기면
  // 지도가 정상으로 떠도 '불러오지 못했습니다' 오버레이가 계속 덮는다.
  assert.equal($("naviMapFallback").hidden, true);
  const css = window.document.querySelector("style").textContent;
  assert.match(css, /\.map-fallback\[hidden\]\s*\{\s*display:\s*none/);
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

check("도착 지점 해석 근거 고지 (건물 중심 과신 방지)", () => {
  assert.match($("naviSheetBody").textContent, /도착 지점: 시설 대표 좌표 기준/);
});

check("지정한 출발지가 경로 요청에 사용됨", () => {
  const q = lastPlanQuery || "";
  assert.match(q, /origin_lat=37\.3943/);
  assert.match(q, /origin_lng=126\.9568/);
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
