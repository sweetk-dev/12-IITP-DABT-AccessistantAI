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

const MMROUTE = {
  status: "success",
  route_id: "r_mm",
  mode_used: "walk_bus",
  mode_label: "도보+버스",
  ui_action: {
    action: "show_route",
    route: {
      route_id: "r_mm",
      destination: { resolved_by: "accessible_entrance", note: null },
      fallback: { used: false },
      routes: [{
        summary: { total_distance_m: 2400, duration_sec: 1500, walk_distance_m: 300,
                   max_slope_deg: 2.0, stairs_cnt: 0, crossing_cnt: 1,
                   eta_note: "소요시간은 정거장 수 기반 추정이며 차량 대기 시간은 포함되지 않습니다",
                   warnings: [] },
        geometry: [[37.3900, 126.9500], [37.3905, 126.9505], [37.3909, 126.9511]],
        steps: [
          { idx: 0, maneuver: "depart", instruction: "정류장까지 100m 이동합니다.",
            distance_m: 100, coord: [37.3900, 126.9500], link_type: "sidewalk", warnings: [] },
          { idx: 1, maneuver: "bus_board", instruction: "소방서 정류장에서 마을버스 2번 버스에 승차합니다 — 신성중 방면, 8개 정거장 이동",
            distance_m: 2000, coord: [37.3903, 126.9503], link_type: "bus", warnings: [] },
          { idx: 2, maneuver: "bus_alight", instruction: "아르테자이정문 정류장에서 하차합니다",
            distance_m: 0, coord: [37.3907, 126.9509], link_type: "bus", warnings: [] },
          { idx: 3, maneuver: "arrive", instruction: "목적지에 도착했습니다.",
            distance_m: 0, coord: [37.3909, 126.9511], link_type: null, warnings: [] },
        ],
        legs: [
          { kind: "walk", to_label: "소방서 정류장",
            summary: { total_distance_m: 100, duration_sec: 90 }, geometry: [] },
          { kind: "bus", route: { route_id: "241253001", name: "2", type: "마을버스", end_station: "신성중" },
            board: { name: "소방서", mobile_no: "09156", station_seq: 21 },
            alight: { name: "아르테자이정문", mobile_no: "09328", station_seq: 41 },
            stop_cnt: 8, warnings: ["저상버스 정차 여부는 보장되지 않습니다"], geometry: [] },
          { kind: "walk", to_label: "목적지",
            summary: { total_distance_m: 200, duration_sec: 180 }, geometry: [] },
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
        constructor() { this.__isMap = true; this.__level = 5; }
        getLevel() { return this.__level; }
        setLevel(lv) { this.__level = lv; zoomHandlers.forEach((cb) => cb()); }
        setBounds(b) { mapCalls.push(["setBounds", b]); }
        setCenter(c) { mapCalls.push(["setCenter", c]); }
        relayout() { mapCalls.push(["relayout"]); }
      },
      LatLng,
      LatLngBounds: Bounds,
      Marker: function () { return mk(); },
      Circle: function () { return mk(); },
      Polyline: function () { return mk(); },
      CustomOverlay: class {
        constructor(o) { this.content = o.content; this.position = o.position; this.visible = false; overlays.push(this); }
        setMap(m) { this.visible = !!m; }
      },
      event: {
        addListener: (target, type, cb) => {
          if (!target || !target.__isMap) return;
          if (type === "click") mapClickHandlers.push(cb);
          if (type === "zoom_changed") zoomHandlers.push(cb);
        },
      },
    },
  };
}

const spoken = [];
const mapClickHandlers = [];
const mapCalls = [];
const zoomHandlers = [];
const overlays = [];
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
    : u.includes("plan_accessible_route")
      ? (u.includes("mode=walk_bus") ? MMROUTE : ROUTE)
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
await sleep(30);
check("첫 진입: 장애유형 선택 게이트 표시 (목록 미로드)", () => {
  assert.ok($("naviTypeGo"), "'관광지 보기' 버튼 없음");
  assert.equal(window.document.querySelectorAll("#naviFilters .chip").length, 4);
  assert.equal($("naviSpots"), null, "게이트 전에 목록이 로드됨");
});
check("게이트: 기본 선택 = 지체장애", () => {
  const on = [...window.document.querySelectorAll('#naviFilters .chip[aria-pressed="true"]')];
  assert.equal(on.length, 1);
  assert.equal(on[0].getAttribute("data-dis"), "지체장애");
});
check("장애 유형 칩 4개와 '관광지 보기' 버튼이 같은 한 줄에 있다", () => {
  const row = $("naviFilters");
  assert.ok(row.classList.contains("typerow"), "한 줄 레이아웃 클래스(typerow)가 없음");
  assert.equal(row.querySelectorAll("button.chip").length, 4, "유형 칩이 4개가 아님");
  assert.ok(row.querySelector("#naviTypeGo"), "'관광지 보기' 버튼이 같은 줄에 없음");
  assert.equal(row.children.length, 5, "한 줄에 5개가 아님");
});
check("라벨을 두 줄로 접어도 읽히는 이름은 원래 문구 그대로", () => {
  const row = $("naviFilters");
  const labels = [...row.querySelectorAll("button.chip")].map((b) => b.getAttribute("aria-label"));
  assert.deepEqual(labels, ["지체장애", "시각장애", "청각장애", "영유아 동반"]);
  assert.equal(row.querySelector("#naviTypeGo").getAttribute("aria-label"), "이 유형으로 관광지 보기");
});
$("naviTypeGo").dispatchEvent(new window.Event("click"));
await sleep(60);
check("유형 확인 후 목록 로드 + 지역 밖 안내가 목록 패널에도 표시", () => {
  assert.ok($("naviSpots"), "목록 미로드");
  assert.match($("naviSheetBody").textContent, /현재 위치가 안양시 밖입니다/);
});
check("목록 화면에도 유형 칩 유지 (변경 가능)", () => {
  assert.equal(window.document.querySelectorAll("#naviFilters .chip").length, 4);
  assert.ok($("naviFilters").classList.contains("typerow"), "목록 화면 칩이 한 줄 레이아웃이 아님");
});

// 1-c) 범위 밖 안내 — 문장만이 아니라 다음에 할 동작을 함께 준다
check("범위 밖 배너에 '출발지 지정'·'정책 상담' 두 가지 조치 버튼", () => {
  const note = window.document.querySelector("#naviSheetBody .navi-note");
  assert.ok(note, "배너 없음");
  assert.match(note.querySelector(".navi-note__t").textContent, /현재 위치가 안양시 밖입니다/);
  assert.ok($("naviPickOrigin"), "'지도에서 출발지 지정' 버튼 없음");
  assert.ok($("naviBackToChat"), "'정책 상담으로 돌아가기' 버튼 없음");
});
check("범위 밖 상태 표시는 경고 색으로 구분", () => {
  assert.ok($("naviStatus").classList.contains("navi-status--warn"), "경고 표시 미적용");
});
check("'정책 상담으로 돌아가기'로 화면 전환", () => {
  $("naviBackToChat").dispatchEvent(new window.Event("click"));
  assert.ok($("view-chat").classList.contains("active"), "상담 화면으로 못 감");
  window.show("view-navi");   // 이후 검사를 위해 원래 화면으로 되돌린다
});
check("'지도에서 출발지 지정'은 지도를 넓히되 손잡이로 되돌릴 수 있어야 한다", () => {
  $("naviPickOrigin").dispatchEvent(new window.Event("click"));
  assert.ok($("naviSheet").classList.contains("collapsed"), "지도를 넓히지 않음");
  assert.equal($("naviGripLabel").textContent, "목록 펼치기");
  $("naviGrip").dispatchEvent(new window.MouseEvent("pointerdown", { clientY: 700, bubbles: true }));
  $("naviGrip").dispatchEvent(new window.MouseEvent("pointerup", { clientY: 700, bubbles: true }));
  assert.ok(!$("naviSheet").classList.contains("collapsed"), "손잡이로 다시 펼쳐지지 않음");
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

// 3) 관광지 선택 -> 출발/도착 팝업 -> 경로 요약 (#196: 즉시 경로 대신 팝업)
const choiceBtn = (re) =>
  [...window.document.querySelectorAll(".spot-choice__btn")].find((b) => re.test(b.textContent));
window.document.querySelectorAll("#naviSpots .spot")[0].dispatchEvent(new window.Event("click"));
await sleep(20);
check("목록 선택 -> 출발지/도착지 선택 팝업 표시", () => {
  assert.ok(window.document.querySelector(".spot-choice"), "팝업 없음");
  assert.match(window.document.querySelector(".spot-choice__nm").textContent, /테스트 무장애 공원/);
  assert.ok(choiceBtn(/여기로 가기/), "도착지 버튼 없음");
  assert.ok(choiceBtn(/여기서 출발/), "출발지 버튼 없음");
});
choiceBtn(/여기로 가기/).dispatchEvent(new window.Event("click"));
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

// 3-b) 이동 방식 카드 + 멀티모달 렌더 (#251)
check("이동 방식 카드 4개 — 기본 '추천' 선택", () => {
  const cards = [...window.document.querySelectorAll(".mode-cards button")];
  assert.equal(cards.length, 4);
  assert.equal(cards[0].getAttribute("aria-pressed"), "true");
  assert.match(cards[0].textContent, /추천/);
});
const busCard = [...window.document.querySelectorAll(".mode-cards button")]
  .find((b) => b.getAttribute("data-mode") === "walk_bus");
busCard.dispatchEvent(new window.Event("click"));
await sleep(60);
check("'도보+버스' 카드 선택 → mode 파라미터로 재요청", () => {
  assert.match(lastPlanQuery || "", /mode=walk_bus/);
});
check("멀티모달 legs 렌더 — 버스 승차 카드·방면·저상 고지", () => {
  const card = window.document.querySelector(".leg-card");
  assert.ok(card, "버스 leg 카드 없음");
  assert.match(card.textContent, /마을버스 2번/);
  assert.match(card.textContent, /신성중 방면/);
  assert.match(card.textContent, /8개 정거장/);
  assert.match(card.textContent, /저상버스 정차는 보장되지 않습니다/);
});
check("멀티모달 요약 — 추정 표기 + 도보 거리 분리", () => {
  const t = $("naviSheetBody").textContent;
  assert.match(t, /예상 시간\(추정\)/);
  assert.match(t, /추정이며 차량 대기 시간은 포함되지 않습니다/);
});
// 이후 시나리오는 도보 경로 기준 — '추천' 카드로 복귀(도보 응답 재수신)
[...window.document.querySelectorAll(".mode-cards button")]
  .find((b) => b.getAttribute("data-mode") === "auto")
  .dispatchEvent(new window.Event("click"));
await sleep(60);
check("'추천' 복귀 시 mode 파라미터 없이 재요청", () => {
  assert.doesNotMatch(lastPlanQuery || "", /mode=walk_bus/);
});

// 4) 안내 시작 -> 스텝 + 음성
const startBtn = [...$("naviSheetBody").querySelectorAll("button")].find((b) => b.textContent === "안내 시작");
check("'안내 시작' 버튼 존재", () => assert.ok(startBtn));
// ── 이동 중 대화 (#248): 가짜 WS 로 nav_state 송신 관찰 ──
const wsSent = [];
const fakeWs = { readyState: 1, send: (s) => wsSent.push(JSON.parse(s)) };
window.NAVI.attachWs(fakeWs);
check("세션 연결 시 이동 중 질문바 노출", () => {
  assert.equal($("naviAskwrap").hidden, false, "질문바가 숨겨져 있음");
  assert.ok($("naviTextInput"), "질문 입력창 없음");
  assert.ok($("naviMicState"), "마이크 상태 표시 없음");
});
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
check("다음 안내 지점 근접 시 스텝 자동 전환 — 앞 안내는 끊지 않고 큐에 이어진다 (v1.43.0)", () => {
  const card = window.document.querySelector(".step-now .ins");
  assert.match(card.textContent, /횡단보도를 건너/);
  assert.equal(spoken.at(-1), "중앙로를 따라 120m 앞으로 이동합니다.", "앞 안내가 잘렸다");
  assert.equal(window.NAVI._internals().speakBusy(), true, "다음 안내가 대기 중이어야 한다");
});
// 앞 안내의 최소 보장 시간(추정 길이의 60%)이 지나면 대기하던 스텝 안내가 이어진다
for (let i = 0; i < 60 && spoken.at(-1) !== "횡단보도를 건너 60m 이동합니다."; i++) await sleep(50);
window.show("view-navi");   // 이 대기 중 스플래시 1.5초 자동 전환이 뷰를 빼앗을 수 있다 — 되돌린다
check("앞 안내가 끝난 뒤 다음 스텝 안내 재발화", () => {
  const card = window.document.querySelector(".step-now .ins");
  assert.equal(spoken.at(-1), "횡단보도를 건너 60m 이동합니다.");
  assert.match(window.document.querySelector(".step-now .wr").textContent, /턱낮춤 없음/);
});

// 5-b) 이동 중 대화 (#248) — nav_state 송신·답변 스트립·안내 복귀
check("안내 시작·진행이 nav_state 로 세션에 전달", () => {
  const nav = wsSent.filter((m) => m.type === "nav_state");
  assert.ok(nav.length >= 2, "nav_state 미송신");
  const started = nav.find((m) => m.guiding && m.step_idx === 0);
  assert.ok(started, "안내 시작 nav_state 없음");
  assert.equal(started.route_id, "r_test");
  assert.equal(started.total_steps, 3);
  assert.match(started.current, /중앙로를 따라 120m/);
  const last = nav.at(-1);
  assert.equal(last.step_idx, 1, "스텝 전환이 nav_state 에 반영 안 됨");
  assert.match(last.current, /횡단보도를 건너/);
  assert.match(String(last.next), /목적지에 도착/);
});
check("상담원 답변 텍스트가 지도 화면 스트립에 표시", () => {
  window.NAVI.onAiText("2번 마을버스는 ");
  window.NAVI.onAiText("순번으로 방면을 구분해요.");
  const el = $("naviAnswer");
  assert.equal(el.hidden, false);
  assert.match(el.textContent, /순번으로 방면을 구분해요/);
});
check("턴 종료 후 다음 답변은 스트립을 새로 시작", () => {
  window.NAVI.onAiTurnComplete();
  window.NAVI.onAiText("새 답변입니다.");
  assert.equal($("naviAnswer").textContent, "새 답변입니다.");
});
check("길안내 TTS barge-in: bargeStop 이 발화를 즉시 중단", () => {
  window.__NAVI_SPEAKING = true;
  window.NAVI.bargeStop();
  assert.equal(window.__NAVI_SPEAKING, false, "발화가 멈추지 않음");
});
// 답변이 끝나면(턴 종료 + 음성 없음) 직전 안내 문장으로 복귀한다.
// 스플래시의 1.5초 자동 전환(show("view-mode"))이 이 대기 중에 뒤늦게 발화해
// navi 뷰를 빼앗으면 복귀가 (의도대로) 억제되므로, 타이머 경과 후 뷰를 되돌린다.
await sleep(1600);
window.show("view-navi");
window.NAVI.onAiTurnComplete();
await sleep(2400);
check("답변 종료 후 직전 안내 문장 자동 복귀", () => {
  assert.match(spoken.at(-1), /^안내를 이어갈게요\. 횡단보도를 건너/);
});
check("상단 종료 버튼 — 안내 중엔 '안내 종료', 누르면 안내만 종료·버튼은 계속 활성 (v1.35.0)", () => {
  const eb = $("naviEndBtn");
  assert.ok(eb, "종료 버튼 없음");
  assert.equal(eb.disabled, false, "안내 중인데 비활성");
  assert.equal(eb.textContent, "안내 종료");
  eb.dispatchEvent(new window.Event("click"));
  assert.ok($("naviSpots"), "종료 후 목록 패널로 복귀하지 않음");
  assert.equal(eb.disabled, false, "종료 후 비활성 — 항시 활성이어야 함");
  assert.equal(eb.textContent, "종료", "안내 종료 후엔 서비스 종료 모드여야 함");
});
check("음성안내 토글은 스텝 카드로 이동 (상단 토글 제거)", () => {
  assert.equal(window.document.getElementById("naviVoiceBtn"), null);
  assert.match(HTML, /음성 끄기/);
});
check("상담원 답변 스트립은 3줄 제한", () => {
  assert.match(HTML, /\.navi-answer\{max-height:4\.2em/);
});
check("세션 미연결 상태의 텍스트 질문은 안내문으로 거절", () => {
  $("naviTextInput").value = "지금 어디로 가요?";
  $("naviSendBtn").dispatchEvent(new window.Event("click"));
  assert.match($("naviStatus").textContent, /상담 세션이 아직 연결되지 않았습니다/);
});
check("마이크 게이트 barge-in 배선 존재 (소스 레벨 가드)", () => {
  assert.match(HTML, /naviBargeRun/);
  assert.match(HTML, /NAVI\.bargeStop/);
  assert.doesNotMatch(HTML, /if \(window\.__NAVI_SPEAKING\) return;   \/\/ 길안내 음성 발화 중에도 동일하게 차단/);
});

// 6) 상담 세션 ui_action -> 자동 전환 없이 데이터 준비 + 이동 버튼 (#213)
const uiAct = window.NAVI.onUiAction({ type: "ui_action", action: "show_route", payload: ROUTE.ui_action });
await sleep(20);
check("ui_action(show_route): 자동 전환 없이 라벨 반환 + 경로 데이터 준비", () => {
  assert.ok(uiAct && /경로 보기/.test(uiAct.label), "라벨 없음");
  assert.match($("naviSheetBody").textContent, /320m/);   // 시트에는 미리 렌더됨
});
const uiAct2 = window.NAVI.onUiAction({ type: "ui_action", action: "show_tour_spots",
  payload: { items: SPOTS.results } });
check("ui_action(show_tour_spots): 목록 준비 + 개수 포함 라벨", () => {
  assert.ok(uiAct2 && /관광지 보기/.test(uiAct2.label));
  assert.match(uiAct2.label, /2곳/);
});
window.NAVI.showPreparedView();
await sleep(30);
check("버튼(showPreparedView) 선택 시에만 지도 화면 전환", () => {
  assert.ok($("view-navi").classList.contains("active"));
});
// 음성 명령 화면 이동 (#215): open_navi ui_action -> 버튼 없이 즉시 전환
const uiAct3 = window.NAVI.onUiAction({ type: "ui_action", action: "open_navi", payload: {} });
await sleep(30);
check("ui_action(open_navi): 음성 요청 시 즉시 화면 전환 + 버튼 라벨 없음", () => {
  assert.equal(uiAct3, null, "이동 버튼이 뜨면 안 됨");
  assert.ok($("view-navi").classList.contains("active"));
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

// 7-b) 정책 답변 표준 템플릿 (#197) — 핵심 요약 + 5섹션 블록
const polTarget = window.document.createElement("div");
window.renderAnswerCard(polTarget, [
  "장애인 활동지원 서비스는 혼자 일상생활이 어려운 장애인을 돕는 제도입니다.",
  "",
  "## 지원 대상",
  "- **만 6세~65세 미만** 등록 장애인",
  "",
  "## 지원 내용",
  "- 월 **60~480시간** 활동지원급여 바우처",
  "",
  "## 신청 방법",
  "- 주소지 행정복지센터 방문 또는 복지로 온라인",
  "",
  "## 구비 서류",
  "- 사회보장급여 신청서",
  "",
  "## 문의처",
  "- 보건복지상담센터 **129**",
  "※ 65세 이후에는 노인장기요양보험으로 전환될 수 있어요.",
].join("\n"));
check("정책 템플릿: 첫 단락 -> 핵심 요약 카드", () => {
  const sum = polTarget.querySelector(".c-sum");
  assert.ok(sum, "핵심 요약 카드 없음");
  assert.match(sum.textContent, /핵심 요약/);
  assert.match(sum.textContent, /활동지원 서비스/);
});
check("정책 템플릿: 표준 5섹션 아이콘 블록 렌더", () => {
  assert.ok(polTarget.querySelector(".p-sec--target"), "지원 대상 없음");
  assert.ok(polTarget.querySelector(".p-sec--benefit"), "지원 내용 없음");
  assert.ok(polTarget.querySelector(".p-sec--how"), "신청 방법 없음");
  assert.ok(polTarget.querySelector(".p-sec--docs"), "구비 서류 없음");
  assert.ok(polTarget.querySelector(".p-sec--contact"), "문의처 없음");
  assert.match(polTarget.querySelector(".p-sec--target .p-sec__h").textContent, /👥 지원 대상/);
  assert.match(polTarget.querySelector(".p-sec--target .p-sec__b").textContent, /만 6세~65세 미만/);
});
check("정책 템플릿: 섹션 내부 강조·유의사항 유지", () => {
  assert.ok([...polTarget.querySelectorAll("mark")].some((m) => m.textContent === "129"));
  assert.match(polTarget.querySelector(".p-sec--contact .c-note").textContent, /노인장기요양보험/);
});
check("정책 템플릿: 표준 섹션 2개 미만이면 기존 카드 유지", () => {
  const t3 = window.document.createElement("div");
  window.renderAnswerCard(t3, ["요금 안내입니다.", "", "## 필요 서류", "- 복지카드"].join("\n"));
  assert.equal(t3.querySelector(".c-sum"), null, "일반 답변에 요약 카드가 생김");
  assert.ok(t3.querySelector(".c-sub"), "기존 소제목 렌더가 사라짐");
});
check("정책 템플릿: 유사 표기(신청 절차·필요 서류 등) 인식", () => {
  const t4 = window.document.createElement("div");
  window.renderAnswerCard(t4, ["요약입니다.", "", "## 신청 절차", "- 방문", "", "## 필요 서류", "- 신분증"].join("\n"));
  assert.ok(t4.querySelector(".p-sec--how"), "신청 절차 미인식");
  assert.ok(t4.querySelector(".p-sec--docs"), "필요 서류 미인식");
});

// 8) 무장애 목록 — 10개 단위 무한스크롤 + 현위치 거리순 (#194)
let lastSpotsQuery = null;
const page = (offset, n, total) => ({
  status: "success",
  total, offset, has_more: offset + n < total,
  results: Array.from({ length: n }, (_, i) => ({
    poi_id: "TBF-P" + (offset + i),
    name: "관광지 " + (offset + i + 1),
    addr: "경기도 안양시 " + (offset + i + 1),
    lat: 37.39 + (offset + i) * 0.001, lng: 126.95,
    distance_m: (offset + i) === 0 ? 378 : (offset + i) * 120 + 378,
    facilities: ["경사로"], score: 0.5,
  })),
});
window.fetch = async (url) => {
  const u = String(url);
  if (u.includes("find_bf_tour_spots")) {
    lastSpotsQuery = u;
    const m = u.match(/offset=(\d+)/);
    const off = m ? parseInt(m[1], 10) : 0;
    return { ok: true, json: async () => page(off, Math.min(10, 25 - off), 25) };
  }
  if (u.includes("/api/v1/config")) return { ok: true, json: async () => CONFIG };
  if (u.includes("plan_accessible_route")) { lastPlanQuery = u; return { ok: true, json: async () => ROUTE }; }
  return { ok: true, json: async () => ({}) };
};
// jsdom 에는 IntersectionObserver 가 없다 — 콜백을 잡아 수동으로 발화시킨다
const ioObserved = [];
let ioCallback = null;
window.IntersectionObserver = function (cb) {
  ioCallback = cb;
  return { observe: (el) => ioObserved.push(el), disconnect: () => {} };
};
window.NAVI.openNavi();
await sleep(60);
check("무한스크롤: 1페이지 10건 렌더 + 감시 지점 등록", () => {
  assert.equal(window.document.querySelectorAll("#naviSpots .spot").length, 10);
  assert.ok($("naviSpotsMore"), "sentinel 없음");
  assert.ok(ioObserved.length >= 1, "IntersectionObserver 미등록");
});
check("거리순 요청: origin_lat/lng·offset·topk 파라미터 전달", () => {
  assert.match(lastSpotsQuery, /origin_lat=37\.3943/);
  assert.match(lastSpotsQuery, /origin_lng=126\.9568/);
  assert.match(lastSpotsQuery, /offset=0/);
  assert.match(lastSpotsQuery, /topk=10/);
});
check("가까운 곳부터 거리 라벨 표시", () => {
  const d = window.document.querySelectorAll("#naviSpots .spot .dist");
  assert.equal(d[0].textContent, "378m");
});
ioCallback([{ isIntersecting: true }]);
await sleep(30);
check("무한스크롤: 감시 지점 도달 시 다음 10건 이어붙임", () => {
  assert.equal(window.document.querySelectorAll("#naviSpots .spot").length, 20);
  assert.match(lastSpotsQuery, /offset=10/);
});
check("km 단위 거리 라벨", () => {
  const d = window.document.querySelectorAll("#naviSpots .spot .dist");
  assert.match(d[10].textContent, /km$/);
});
ioCallback([{ isIntersecting: true }]);
await sleep(30);
check("무한스크롤: 마지막 페이지(총 25건) 후 감시 지점 제거", () => {
  assert.equal(window.document.querySelectorAll("#naviSpots .spot").length, 25);
  assert.equal($("naviSpotsMore"), null);
});

// 9) 지도 이름 태그 + 겹침 묶음/줌 분리 (#195)
const liveTags = () => overlays.filter((o) => o.visible);
check("이름 태그: 마커 대신 CustomOverlay 태그로 표시", () => {
  assert.ok(liveTags().length > 0, "태그 없음");
  assert.match(liveTags()[0].content.className, /poi-tag/);
  assert.ok(liveTags()[0].content.textContent.length > 0);
});
check("겹침: 대표 1개만 표시 + '외 N' 묶음 태그", () => {
  assert.ok(liveTags().length < 25, "묶이지 않음 — 25개 전부 표시됨");
  assert.ok(liveTags().some((o) => /외 \d+/.test(o.content.textContent)), "'외 N' 태그 없음");
});
const tagsBefore = liveTags().length;
const multiTag = liveTags().find((o) => o.content.className.includes("poi-tag--multi"));
multiTag.content.dispatchEvent(new window.Event("click"));
await sleep(20);
check("묶음 태그 클릭(줌 확대) 시 태그 분리", () => {
  assert.ok(liveTags().length > tagsBefore,
    `분리 안 됨 (${tagsBefore} -> ${liveTags().length})`);
});
// 단일 태그가 나올 때까지 묶음 태그를 눌러 계속 확대 (레벨 1 하한까지)
let singleTag = null;
for (let i = 0; i < 6 && !singleTag; i++) {
  singleTag = liveTags().find((o) => !o.content.className.includes("poi-tag--multi"));
  if (singleTag) break;
  const m = liveTags().find((o) => o.content.className.includes("poi-tag--multi"));
  if (!m) break;
  m.content.dispatchEvent(new window.Event("click"));
  await sleep(10);
}
lastPlanQuery = null;
singleTag.content.dispatchEvent(new window.Event("click"));
await sleep(20);
check("도착지 유지 상태에서 태그 클릭 -> 맥락 팝업 (#210)", () => {
  assert.ok(window.document.querySelector(".spot-choice"), "팝업 없음");
  assert.ok(choiceBtn(/도착지를 여기로 변경/), "도착지 변경 버튼 없음");
});
choiceBtn(/도착지를 여기로 변경/).dispatchEvent(new window.Event("click"));
await sleep(60);
check("도착지 변경(출발지 있음) -> 즉시 경로 요청", () => {
  assert.ok(lastPlanQuery, "경로 요청 안 감");
});

// 10) 출발지/도착지 상호 보완 플로우 (#196)
// 10-a) 출발지 먼저: 태그에서 '여기서 출발' -> 다음 선택이 도착지가 된다
window.NAVI._internals().resetTrip();   // 직전 여정 초기화 (#210: 도착지는 유지되는 상태)
window.NAVI.openNavi();
await sleep(60);
let tag0 = liveTags().filter((o) => !o.content.className.includes("poi-tag--multi"));
if (tag0.length < 2) {
  // 확대해서 단일 태그 2개 확보
  for (let i = 0; i < 6 && tag0.length < 2; i++) {
    const m = liveTags().find((o) => o.content.className.includes("poi-tag--multi"));
    if (!m) break;
    m.content.dispatchEvent(new window.Event("click"));
    await sleep(10);
    tag0 = liveTags().filter((o) => !o.content.className.includes("poi-tag--multi"));
  }
}
tag0[0].content.dispatchEvent(new window.Event("click"));
await sleep(20);
choiceBtn(/여기서 출발/).dispatchEvent(new window.Event("click"));
await sleep(30);
check("출발지 먼저 선택 -> 도착지 선택 안내", () => {
  assert.match($("naviStatus").textContent, /도착지를 선택/);
});
lastPlanQuery = null;
const tag1 = liveTags().filter((o) => !o.content.className.includes("poi-tag--multi"));
tag1[1].content.dispatchEvent(new window.Event("click"));
await sleep(60);
check("이어서 태그 선택 = 도착지 -> 즉시 경로 요청", () => {
  assert.ok(lastPlanQuery, "경로 요청 안 감");
});

// 10-b) 도착지 먼저(출발지 없음): 출발지 물음 -> 지도·목록에서 선택 -> 자동 안내
window.NAVI.openNavi();
await sleep(60);
// 출발지 초기화: '현재 위치로 되돌리기' + 현재 위치 제거
const resetBtn = [...$("naviSheetBody").querySelectorAll("button")]
  .find((b) => /현재 위치로 되돌리기/.test(b.textContent));
if (resetBtn) { resetBtn.dispatchEvent(new window.Event("click")); await sleep(40); }
window.NAVI._internals().setHere(null);
window.NAVI._internals().resetTrip();
let tags = liveTags().filter((o) => !o.content.className.includes("poi-tag--multi"));
tags[0].content.dispatchEvent(new window.Event("click"));
await sleep(20);
choiceBtn(/여기로 가기/).dispatchEvent(new window.Event("click"));
await sleep(20);
check("출발지 없이 도착지 선택 -> 출발지 선택 팝업", () => {
  assert.match(window.document.querySelector(".spot-choice__nm").textContent, /출발지를 선택/);
  assert.ok(choiceBtn(/현재 위치에서 출발/), "현위치 버튼 없음");
});
choiceBtn(/지도·목록에서 출발지 선택/).dispatchEvent(new window.Event("click"));
await sleep(20);
check("지도·목록 선택 모드 안내", () => {
  assert.match($("naviStatus").textContent, /출발지를 선택해 주세요/);
});
lastPlanQuery = null;
tags = liveTags().filter((o) => !o.content.className.includes("poi-tag--multi"));
tags[1].content.dispatchEvent(new window.Event("click"));
await sleep(60);
check("출발지 태그 선택 -> 대기 중 도착지로 자동 안내", () => {
  assert.ok(lastPlanQuery, "경로 요청 안 감");
  assert.match($("naviSheetBody").textContent, /320m/);
});

// 11) 도착지 선택 유지 (#204) — 팝업을 닫아도 도착지를 기억하고, 출발지 선택 시 자동 안내
window.NAVI.openNavi();
await sleep(60);
const resetBtn2 = [...$("naviSheetBody").querySelectorAll("button")]
  .find((b) => /현재 위치로 되돌리기/.test(b.textContent));
if (resetBtn2) { resetBtn2.dispatchEvent(new window.Event("click")); await sleep(40); }
window.NAVI._internals().setHere(null);
window.NAVI._internals().resetTrip();
let tagsB = liveTags().filter((o) => !o.content.className.includes("poi-tag--multi"));
tagsB[0].content.dispatchEvent(new window.Event("click"));
await sleep(20);
choiceBtn(/여기로 가기/).dispatchEvent(new window.Event("click"));
await sleep(20);
check("출발지 팝업 '닫기'가 도착지를 유지함을 안내", () => {
  assert.ok(choiceBtn(/닫기.*도착지는 유지/), "닫기 버튼 없음");
});
choiceBtn(/닫기/).dispatchEvent(new window.Event("click"));
await sleep(20);
tagsB = liveTags().filter((o) => !o.content.className.includes("poi-tag--multi"));
tagsB[1].content.dispatchEvent(new window.Event("click"));
await sleep(20);
check("닫은 뒤 다른 태그 클릭 -> 도착지 유지 맥락 팝업", () => {
  assert.ok(choiceBtn(/여기서 출발 →/), "'여기서 출발 → 도착지' 버튼 없음");
  assert.ok(choiceBtn(/도착지를 여기로 변경/), "도착지 변경 버튼 없음");
  assert.ok(choiceBtn(/선택 초기화/), "초기화 버튼 없음");
});
lastPlanQuery = null;
choiceBtn(/여기서 출발 →/).dispatchEvent(new window.Event("click"));
await sleep(60);
check("맥락 팝업에서 출발지 선택 -> 도착지 재선택 없이 자동 안내", () => {
  assert.ok(lastPlanQuery, "경로 요청 안 감");
  assert.match($("naviSheetBody").textContent, /320m/);
});

// 11-b) #210 핵심 시나리오: 안내 이후 '지도 빈 곳 클릭'으로 출발지를 바꿔도
//       태그로 바꿀 때와 동일하게 같은 도착지로 자동 재안내되어야 한다
lastPlanQuery = null;
mapClickHandlers[0]({ latLng: { getLat: () => 37.3950, getLng: () => 126.9550 } });
await sleep(60);
check("지도 클릭 출발지 변경 -> 같은 도착지로 자동 재안내 (#210)", () => {
  assert.ok(lastPlanQuery, "재안내 안 감");
  assert.match(lastPlanQuery, /origin_lat=37\.395/);
  assert.match($("naviSheetBody").textContent, /320m/);
});
check("재안내 음성/상태 안내", () => {
  assert.match(spoken.at(-1) || "", /다시 안내/);
});
// 선택 초기화 후에는 지도 클릭 = 출발지만 지정(목록 갱신), 재안내 없음
window.NAVI._internals().resetTrip();
lastPlanQuery = null;
mapClickHandlers[0]({ latLng: { getLat: () => 37.3960, getLng: () => 126.9560 } });
await sleep(60);
check("초기화 후 지도 클릭 -> 재안내 없이 목록 화면 (#210)", () => {
  assert.equal(lastPlanQuery, null, "초기화 후에도 재안내됨");
  assert.ok($("naviSpots"), "목록 미표시");
});

// 12) 유휴 방지·카드 배선 존재 검증 (#203·#205) — 소스 레벨 회귀 가드
check("이동·관광 화면 체류 하트비트 코드 존재", () => {
  assert.match(HTML, /navi_view_heartbeat/);
  assert.match(HTML, /guidance_heartbeat/);
});
check("answer_card 수신 배선 존재", () => {
  assert.match(HTML, /case "answer_card"/);
  assert.match(HTML, /applyAnswerCard/);
});

// 13) 정책 카드 선표시 + 전사 병행 배치 (#208)
const C = window.__CHAT;
check("__CHAT 테스트 훅 노출", () => assert.ok(C && C.applyAnswerCard && C.appendAiTranscript));
// (a) 카드가 전사보다 먼저 도착 (도구 기반 선표시 경로)
C.applyAnswerCard("활동지원 요약입니다.\n\n## 지원 대상\n- **등록 장애인**\n\n## 문의처\n- 129");
C.appendAiTranscript("장애인 활동지원 서비스는 ");
C.appendAiTranscript("행정복지센터에서 신청하실 수 있어요.");
check("선도착 카드: 말풍선 생성 시 전사 '위'에 부착", () => {
  const bubbles = window.document.querySelectorAll("#chat .bubble--ai");
  const b = bubbles[bubbles.length - 1];
  const kids = [...b.children];
  const cardIdx = kids.findIndex((k) => k.querySelector && k.querySelector(".card"));
  const txtIdx = kids.indexOf(b._textNode);
  assert.ok(cardIdx >= 0, "카드 없음");
  assert.ok(txtIdx > cardIdx, "카드가 전사 아래에 있음");
  assert.match(b.querySelector(".c-sum").textContent, /활동지원 요약/);
  assert.match(b._textNode.textContent, /행정복지센터/);
});
C.finalizeAiBubble();
// (b) 턴 종료 후 늦게 도착한 카드(전사 폴백 경로)도 그 말풍선 상단에
C.appendAiTranscript("보청기는 최대 백삼십일만원까지 지원됩니다.");
C.finalizeAiBubble();
C.applyAnswerCard("보청기 요약.\n\n## 지원 내용\n- **131만원**\n\n## 신청 방법\n- 주민센터");
check("턴 종료 후 도착한 카드도 해당 말풍선 상단에 부착", () => {
  const bubbles = window.document.querySelectorAll("#chat .bubble--ai");
  const b = bubbles[bubbles.length - 1];
  assert.ok(b.querySelector(".p-sec--benefit"), "카드 없음");
  const kids = [...b.children];
  const cardIdx = kids.findIndex((k) => k.querySelector && k.querySelector(".p-sec--benefit"));
  assert.ok(kids.indexOf(b._textNode) > cardIdx, "카드가 전사 아래에 있음");
});

// 13-b) 카드 컨테이너·비표준 소제목 카드 (#218)
check("카드 컨테이너: 헤더·테두리로 전사와 구분", () => {
  const wrap = window.document.querySelector("#chat .answer-card-wrap");
  assert.ok(wrap, "컨테이너 없음");
  assert.match(wrap.querySelector(".answer-card-wrap__head").textContent, /정책 요약 카드/);
});
C.appendAiTranscript("여러 제도를 안내드릴게요.");
C.finalizeAiBubble();
C.applyAnswerCard("주거 지원 제도 두 가지를 안내드립니다.\n\n## 장애인연금\n- **기초급여** 지원\n\n## 무주택 특별공급\n- 특별 분양 알선");
check("비표준 소제목(정책명) 카드도 핵심요약 + 중립 섹션 블록으로 렌더", () => {
  const wraps = window.document.querySelectorAll("#chat .answer-card-wrap");
  const w = wraps[wraps.length - 1];
  assert.ok(w.querySelector(".c-sum"), "요약 박스 없음");
  const secs = w.querySelectorAll(".p-sec--etc");
  assert.equal(secs.length, 2, "중립 섹션 블록 미적용");
  assert.match(secs[0].querySelector(".p-sec__h").textContent, /장애인연금/);
});
check("일반(비카드) 답변 렌더는 기존 유지 — assumePolicy 없이 요약 박스 미생성", () => {
  const t5 = window.document.createElement("div");
  window.renderAnswerCard(t5, ["요금 안내입니다.", "", "## 필요 서류", "- 복지카드"].join("\n"));
  assert.equal(t5.querySelector(".c-sum"), null);
});

// 13-c) 내장 음성 중단 배선 (#217) — 소스 레벨 회귀 가드
check("내장 TTS 중단(stopLocalTts)·barge-in 배선 존재", () => {
  assert.match(HTML, /stopLocalTts/);
  assert.match(HTML, /LOCAL_TTS_BARGE_RMS/);
  assert.match(HTML, /서버\(상담원\) 음성이 오면 기기 내장 음성은 즉시 멈춘다/);
});

// 14) 이동·관광 화면 이동 버튼 말풍선 (#213)
C.addNaviJumpBubble("🗺️ 지도에서 무장애 관광지 보기 (5곳)");
check("이동 버튼 말풍선이 대화에 표시", () => {
  const btn = [...window.document.querySelectorAll("#chat .bubble--nav .navjump")].pop();
  assert.ok(btn, "버튼 없음");
  assert.match(btn.textContent, /지도에서 무장애 관광지 보기/);
});

// 15) 하단 시트 접힘 UX — 지도 조작 후에도 목록으로 돌아갈 수 있어야 한다
const sheetEl = $("naviSheet");
const gripEl = $("naviGrip");
const mapEl = $("naviMap");
const ptr = (type, x, y) =>
  mapEl.dispatchEvent(new window.MouseEvent(type, { clientX: x, clientY: y, bubbles: true }));

check("지도를 '탭'만 하면 목록이 접히지 않는다 (한 번 누르는 조작 보호)", () => {
  ptr("pointerdown", 100, 200);
  ptr("pointerup", 100, 200);
  assert.ok(!sheetEl.classList.contains("collapsed"), "탭만으로 접힘");
});

check("지도를 실제로 움직이면(패닝) 목록이 접힌다", () => {
  ptr("pointerdown", 100, 200);
  ptr("pointermove", 100, 120);
  assert.ok(sheetEl.classList.contains("collapsed"), "패닝인데 안 접힘");
});

check("접힘 상태: 본문은 감추고 손잡이는 '펼치기'로 라벨이 바뀐다", () => {
  // 예전에는 max-height:38px 로 잘라 본문 첫 줄이 반쯤 잘린 채 남았다
  assert.match(HTML, /\.sheet\.collapsed #naviSheetBody\{display:none;\}/);
  assert.equal($("naviGripLabel").textContent, "목록 펼치기");
  assert.equal(gripEl.getAttribute("aria-expanded"), "false");
  assert.equal(gripEl.getAttribute("aria-label"), "관광지 목록 펼치기");
});

check("손잡이 터치 영역이 앱 최소 기준(--tap-min)을 따른다", () => {
  assert.match(HTML, /\.gripzone\{[^}]*min-height:var\(--tap-min\)/);
  assert.doesNotMatch(HTML, /\.sheet\.collapsed\{max-height:38px/);
});

check("하단 탭바는 세로 공간이 부족해도 줄어들지 않는다(flex:none)", () => {
  assert.match(HTML, /\.modebar\{flex:none;/);
  assert.match(HTML, /#view-navi>\.appbar\{flex:none;\}/);
});


/* ===== 수집 장치화 (v1.34.0): 접근성 신고 + 주행 트랙 ===== */
const navPosts = [];
window.fetch = async (url, opts) => {
  const u = String(url);
  if (u.includes("/api/v1/nav/")) {
    navPosts.push({ url: u, body: JSON.parse(opts && opts.body || "{}") });
    return { ok: true, json: async () => ({ report_id: 7, stored_points: 3 }) };
  }
  if (u.includes("/api/v1/config")) return { ok: true, json: async () => CONFIG };
  if (u.includes("plan_accessible_route")) { lastPlanQuery = u; return { ok: true, json: async () => ROUTE }; }
  if (u.includes("find_bf_tour_spots")) return { ok: true, json: async () => page(0, 10, 25) };
  return { ok: true, json: async () => ({}) };
};

watchCb({ coords: { latitude: 37.39, longitude: 126.95, accuracy: 8 } });
await sleep(20);

check("신고 버튼이 지도 위에 상시 노출, 시트는 접힘", () => {
  assert.ok($("naviReportBtn"), "신고 버튼 없음");
  assert.equal($("reportSheet").hidden, true);
});
$("naviReportBtn").dispatchEvent(new window.Event("click"));
check("신고 버튼 -> 사유 시트 열림 (6개 사유 + 사진 첨부 + 닫기)", () => {
  assert.equal($("reportSheet").hidden, false);
  assert.equal($("reportSheet").querySelectorAll("button[data-reason]").length, 6);
  assert.ok($("repPhotoInput"), "사진 입력 없음");
  assert.ok($("reportCancelBtn"), "닫기 버튼 없음");
});
$("reportCancelBtn").dispatchEvent(new window.Event("click"));
check("닫기 버튼으로 시트가 닫힘", () => assert.equal($("reportSheet").hidden, true));
$("naviReportBtn").dispatchEvent(new window.Event("click"));
$("reportSheet").querySelector('button[data-reason="curb"]').dispatchEvent(new window.Event("click"));
await sleep(40);
check("사유 원터치 -> 즉시 전송(좌표·사유·route_id) + 접수 안내", () => {
  const p = navPosts.find((x) => x.url.includes("/nav/report"));
  assert.ok(p, "신고 POST 없음");
  assert.equal(p.body.reason, "curb");
  assert.ok(Math.abs(p.body.lat - 37.39) < 0.02, "좌표 이상: " + p.body.lat);
  assert.equal($("reportSheet").hidden, true);
  assert.match($("naviStatus").textContent, /접수되었습니다/);
});

// 트랙: 새 경로 -> 안내 시작 -> 위치 갱신 -> 안내 종료 -> 업로드
window.NAVI.openNavi();
await sleep(60);
window.document.querySelectorAll("#naviSpots .spot")[0].dispatchEvent(new window.Event("click"));
await sleep(20);
const cgo = [...window.document.querySelectorAll(".spot-choice__btn")].find((b) => /여기로 가기/.test(b.textContent));
cgo.dispatchEvent(new window.Event("click"));
await sleep(80);
const sgBtn = [...$("naviSheetBody").querySelectorAll("button")].find((b) => b.textContent === "안내 시작");
check("트랙 검증용 경로에 '안내 시작' 버튼", () => assert.ok(sgBtn));
sgBtn.dispatchEvent(new window.Event("click"));
await sleep(30);
watchCb({ coords: { latitude: 37.3902, longitude: 126.9502, accuracy: 5 } });
watchCb({ coords: { latitude: 37.3905, longitude: 126.9506, accuracy: 5 } });
await sleep(20);
$("naviEndBtn").dispatchEvent(new window.Event("click"));
await sleep(40);
check("안내 종료 -> 주행 트랙 업로드 (points + outcome=canceled + 경로선)", () => {
  const posts = navPosts.filter((x) => x.url.includes("/nav/track"));
  assert.ok(posts.length >= 1, "트랙 POST 없음");
  const p = posts[posts.length - 1];
  assert.ok(p.body.points.length >= 2, "트랙 점 부족: " + p.body.points.length);
  assert.equal(p.body.meta.outcome, "canceled");
  assert.equal(p.body.route_id, "r_test");
  assert.ok(Array.isArray(p.body.meta.geometry) && p.body.meta.geometry.length >= 2, "경로선 없음");
  assert.equal(p.body.meta.planned_dist_m, 320);
  assert.ok(p.body.points[0].ts, "타임스탬프 없음");
});
check("트랙 업로드에 참여자 식별 필드가 없다 (route_id 익명)", () => {
  const p = navPosts.filter((x) => x.url.includes("/nav/track")).pop();
  const keys = Object.keys(p.body).sort().join(",");
  assert.equal(keys, "meta,points,route_id");
});


/* ===== v1.35.0: 종료 버튼 서비스 종료 + 신고 버튼 축소 + 경로 이탈 자동 재안내 ===== */
check("신고 버튼 축소 — 짧은 라벨 + 절반 크기 패딩 (지도 가림 최소화)", () => {
  assert.equal($("naviReportBtn").textContent, "🚧 신고");
  assert.match(HTML, /\.reportbtn\{[^}]*padding:9px 10px/);
});

// 평시(안내 없음) 종료 버튼 = 서비스 종료 -> 홈(모드 선택)
check("평시 종료 버튼 활성 + '종료' 라벨", () => {
  const eb = $("naviEndBtn");
  assert.equal(eb.disabled, false);
  assert.equal(eb.textContent, "종료");
});
// v1.37.0: 홈으로 나가는 것은 세션을 닫는 동작 — 확인 팝업을 한 번 거친다
$("naviEndBtn").dispatchEvent(new window.Event("click"));
check("평시 종료 클릭 -> 곧바로 나가지 않고 확인 팝업이 뜬다 (v1.37.0)", () => {
  const m = $("naviEndModal");
  assert.ok(m, "종료 확인 팝업이 없음");
  assert.equal(m.hidden, false, "팝업이 뜨지 않음");
  assert.ok($("view-navi").classList.contains("active"), "확인 전에 화면을 떠남");
});
check("확인 팝업 문구·구조가 '상담 종료' 팝업과 같은 형식", () => {
  const m = $("naviEndModal");
  assert.equal(m.getAttribute("class"), "modal-backdrop");
  const card = m.querySelector(".modal-card");
  assert.equal(card.getAttribute("role"), "dialog");
  assert.equal(card.getAttribute("aria-modal"), "true");
  assert.equal(card.getAttribute("aria-labelledby"), "naviEndTitle");
  assert.equal($("naviEndCancelBtn").textContent, "취소");
  assert.equal($("naviEndConfirmBtn").textContent, "확인");
});
$("naviEndCancelBtn")?.dispatchEvent(new window.Event("click"));
check("취소 -> 팝업만 닫히고 안내 화면에 그대로 머문다", () => {
  assert.equal($("naviEndModal").hidden, true, "팝업이 닫히지 않음");
  assert.ok($("view-navi").classList.contains("active"), "취소했는데 화면을 떠남");
  assert.equal($("naviAskwrap").hidden, false, "취소했는데 질문 바가 사라짐");
});
$("naviEndBtn").dispatchEvent(new window.Event("click"));
$("naviEndConfirmBtn")?.dispatchEvent(new window.Event("click"));
check("확인 -> 상담 화면 경유 없이 바로 홈(모드 선택)으로", () => {
  assert.equal($("naviEndModal").hidden, true, "팝업이 남아 있음");
  assert.ok($("view-mode").classList.contains("active"), "홈 화면으로 가지 않음");
  assert.equal($("naviAskwrap").hidden, true, "질문 바가 남아 있음");
});

// 경로 이탈 -> 자동 재탐색 -> 안내 자동 재개
window.show("view-navi");
window.NAVI.openNavi();
await sleep(60);
window.document.querySelectorAll("#naviSpots .spot")[0].dispatchEvent(new window.Event("click"));
await sleep(20);
const cgo2 = [...window.document.querySelectorAll(".spot-choice__btn")].find((b) => /여기로 가기/.test(b.textContent));
cgo2.dispatchEvent(new window.Event("click"));
await sleep(80);
const sgBtn2 = [...$("naviSheetBody").querySelectorAll("button")].find((b) => b.textContent === "안내 시작");
sgBtn2.dispatchEvent(new window.Event("click"));
await sleep(30);
check("안내 재시작 -> 버튼이 '안내 종료'로 전환", () => {
  assert.equal($("naviEndBtn").textContent, "안내 종료");
});
const spokenBefore = spoken.length;
const trackPostsBefore = navPosts.filter((x) => x.url.includes("/nav/track")).length;
// 경로(37.390x대)에서 ~600m 북쪽 — 서비스 지역 안, 경로선 밖 30m 초과를 3회 연속
watchCb({ coords: { latitude: 37.3955, longitude: 126.9500, accuracy: 5 } });
watchCb({ coords: { latitude: 37.3956, longitude: 126.9502, accuracy: 5 } });
watchCb({ coords: { latitude: 37.3957, longitude: 126.9504, accuracy: 5 } });
await sleep(120);
check("이탈 3회 연속 -> 이탈 안내 발화 + 트랙 마감(outcome=rerouted)", () => {
  const said = spoken.slice(spokenBefore).join(" | ");
  assert.match(said, /경로를 벗어나셨어요/, "이탈 안내 없음: " + said);
  const posts = navPosts.filter((x) => x.url.includes("/nav/track"));
  assert.ok(posts.length > trackPostsBefore, "이탈 트랙 업로드 없음");
  assert.equal(posts[posts.length - 1].body.meta.outcome, "rerouted");
});
check("재탐색 후 안내 자동 재개 — 스텝 카드 + 첫 안내 발화", () => {
  assert.ok(window.document.querySelector(".step-now"), "스텝 카드 없음 (안내 미재개)");
  const said = spoken.slice(spokenBefore).join(" | ");
  assert.match(said, /중앙로를 따라/, "재개 첫 안내 발화 없음: " + said);
  assert.equal($("naviEndBtn").textContent, "안내 종료", "재개 후 버튼 상태");
});
check("GPS 튐 1~2회는 재탐색을 유발하지 않는다 (연속 3회 조건)", () => {
  // 위에서 정확히 3회 만에 발화 1건 — 추가로 1회 이탈 후 정상 복귀 시 카운터 리셋 확인
  const beforeCnt = spoken.filter((s) => /경로를 벗어나셨어요/.test(s)).length;
  watchCb({ coords: { latitude: 37.3955, longitude: 126.9500, accuracy: 5 } });
  watchCb({ coords: { latitude: 37.3901, longitude: 126.9502, accuracy: 5 } });  // 경로 복귀
  watchCb({ coords: { latitude: 37.3955, longitude: 126.9500, accuracy: 5 } });
  watchCb({ coords: { latitude: 37.3901, longitude: 126.9502, accuracy: 5 } });
  const afterCnt = spoken.filter((s) => /경로를 벗어나셨어요/.test(s)).length;
  assert.equal(afterCnt, beforeCnt, "간헐 이탈에 재탐색이 발화됨");
});

// ── 종료 버튼: 상태 갱신 인터벌이 돈 뒤에도 활성이 유지되어야 한다 ──
// (구 #251 코드가 600ms 마다 disabled 를 되돌려 놓아 홈으로 갈 수 없었다)
window.show("view-navi");
window.NAVI.openNavi();
await sleep(60);
// 안내를 먼저 끝내 '평시' 상태로 만든 뒤에 틱을 지나게 한다 —
// 구 코드는 이 조건(guiding=false)에서 버튼을 다시 잠갔다.
if ($("naviEndBtn").textContent === "안내 종료") {
  $("naviEndBtn").dispatchEvent(new window.Event("click"));
  await sleep(20);
}
await sleep(700);   // 상태 갱신 틱을 최소 한 번 지나게 한다
check("평시에도 종료 버튼은 상태 갱신 틱이 돌아도 잠기지 않는다", () => {
  const eb = $("naviEndBtn");
  assert.equal(eb.disabled, false, "600ms 틱이 종료 버튼을 다시 잠갔음");
  assert.ok(["종료", "안내 종료"].includes(eb.textContent), "라벨이 두 상태 중 하나가 아님");
});

// ── 신고: '기타 문제' 프롬프트를 취소하면 아무것도 보내지 않는다 ──
$("naviReportBtn").dispatchEvent(new window.Event("click"));
await sleep(10);
const repBefore = navPosts.filter((x) => x.url.includes("/nav/report")).length;
const origPrompt = window.prompt;
window.prompt = () => null;                     // 사용자가 '취소'를 누른 상황
$("reportSheet").querySelector('button[data-reason="etc"]').dispatchEvent(new window.Event("click"));
await sleep(40);
check("'기타 문제' 프롬프트 취소 -> 전송하지 않고 시트도 그대로", () => {
  const after = navPosts.filter((x) => x.url.includes("/nav/report")).length;
  assert.equal(after, repBefore, "취소했는데 신고가 전송됨");
  assert.equal($("reportSheet").hidden, false, "취소했는데 시트가 닫힘");
});
window.prompt = () => "보도에 자전거가 세워져 있어요";   // 이번엔 '확인'
$("reportSheet").querySelector('button[data-reason="etc"]').dispatchEvent(new window.Event("click"));
await sleep(40);
check("'기타 문제' 프롬프트 확인 -> detail 과 함께 전송", () => {
  const posts = navPosts.filter((x) => x.url.includes("/nav/report"));
  assert.equal(posts.length, repBefore + 1, "확인했는데 전송되지 않음");
  assert.equal(posts[posts.length - 1].body.reason, "etc");
  assert.equal(posts[posts.length - 1].body.detail, "보도에 자전거가 세워져 있어요");
});
window.prompt = origPrompt;

// ══════════════════════════════════════════════════════════════════
// 목적지 지정 경로 확장 + 안내 실패 시 화면·말 일치 (v1.38.0)
//
// 종전에는 (1) 관광지 태그·목록에 없는 곳은 도착지로 지정할 방법이 아예 없었고,
// (2) 경로를 만들지 못해도 이전 경로가 지도·시트에 그대로 남아 "안내할 수 없다"는
// 말과 화면이 어긋났다.
// ══════════════════════════════════════════════════════════════════
const NAV = window.NAVI._internals();
const SEARCH_HITS = {
  status: "success", query: "안양시청", count: 1,
  items: [{ type: "building", poi_id: null, name: "안양시청",
            addr: null, lat: 37.39429, lng: 126.95687 }],
};
let planResponse = ROUTE;
let searchResponse = SEARCH_HITS;
const origFetch = window.fetch;
window.fetch = async (url, opt) => {
  const u = String(url);
  if (u.includes("search_places")) return { ok: true, json: async () => searchResponse };
  if (u.includes("plan_accessible_route")) {
    lastPlanQuery = u;
    return { ok: true, json: async () => planResponse };
  }
  return origFetch(url, opt);
};

// 깨끗한 상태에서 시작 — 안양 안 현위치 + 목록 화면
window.NAVI.openNavi();
watchCb({ coords: { latitude: 37.3900, longitude: 126.9500 } });
await sleep(30);
NAV.resetTrip();
NAV.renderSpotsPanel();
await sleep(20);

check("목록 화면에 장소 이름 검색과 '지도에서 도착지 지정'이 있다", () => {
  assert.ok($("placeQ"), "장소 검색 입력창 없음");
  assert.ok($("naviDestPick"), "'지도에서 도착지 지정' 버튼 없음");
});

$("placeQ").value = "안양시청";
$("placeQ").parentElement.querySelector("button").dispatchEvent(new window.Event("click"));
await sleep(40);
check("이름으로 찾은 장소가 결과 목록에 뜬다 (관광지가 아니어도)", () => {
  const hits = $("placeResults").querySelectorAll("button.spot");
  assert.equal(hits.length, 1, "검색 결과가 렌더되지 않음");
  assert.match(hits[0].textContent, /안양시청/);
  assert.match(hits[0].textContent, /건물/, "장소 종류 표기 없음");
});

planResponse = ROUTE;
$("placeResults").querySelector("button.spot").dispatchEvent(new window.Event("click"));
await sleep(20);
window.document.querySelector(".spot-choice__btn--dest").dispatchEvent(new window.Event("click"));
await sleep(60);
check("검색 결과를 도착지로 고르면 좌표+building 타입으로 경로를 요청한다", () => {
  assert.match(lastPlanQuery, /destination_lat=37\.39429/);
  assert.match(lastPlanQuery, /destination_lng=126\.95687/);
  assert.match(lastPlanQuery, /destination_type=building/,
    "건물은 출입구 접근점 해석을 거쳐야 하므로 building 이어야 한다");
  assert.ok(!/destination_poi_id=\w/.test(lastPlanQuery), "없는 poi_id 를 보냄");
});

// ── 지도에서 도착지 지정 ──
NAV.resetTrip();
NAV.renderSpotsPanel();
await sleep(20);
$("naviDestPick").dispatchEvent(new window.Event("click"));
await sleep(20);
check("'지도에서 도착지 지정' -> 지도를 넓히고 무엇을 할지 안내", () => {
  assert.ok($("naviSheet").classList.contains("collapsed"), "지도를 넓히지 않음");
  assert.match($("naviStatus").textContent, /도착지로 정합니다/);
});
lastPlanQuery = null;
mapClickHandlers[0]({ latLng: { getLat: () => 37.3960, getLng: () => 126.9577 } });
await sleep(60);
check("도착지 지정 모드에서 지도를 누르면 그 지점으로 경로를 요청한다", () => {
  assert.ok(lastPlanQuery, "경로 요청이 나가지 않음");
  assert.match(lastPlanQuery, /destination_lat=37\.396/);
  assert.match(lastPlanQuery, /destination_type=coord/,
    "지도에서 콕 집은 점은 좌표 그대로 써야 한다");
});
NAV.resetTrip();
check("도착지 지정은 1회성 — 다음 지도 클릭은 다시 출발지다", () => {
  mapClickHandlers[0]({ latLng: { getLat: () => 37.3901, getLng: () => 126.9502 } });
  assert.match($("naviStatus").textContent, /출발지를 지정했습니다/);
});

await sleep(80);   // 앞선 출발지 지정이 부른 목록 재조회가 시트를 덮어쓰지 않도록

// ── 안내 실패: 말과 화면이 어긋나지 않는다 ──
NAV.resetTrip();
planResponse = ROUTE;
NAV.requestRoute({ poi_id: "TBF-1", name: "테스트 무장애 공원" });
await sleep(60);
check("(사전) 경로가 화면에 표시된 상태", () => {
  assert.match($("naviSheetBody").textContent, /테스트 무장애 공원 까지/);
});

planResponse = {
  status: "place_not_found", tool_name: "plan_accessible_route",
  message: "목적지 '○○복지관'의 위치를 찾지 못했습니다",
};
NAV.requestRoute({ poi_id: "", name: "○○복지관", lat: 37.3905, lng: 126.9505, kind: "coord" });
await sleep(60);
check("안내 중이 아닐 때 경로 실패 -> 이전 경로 요약이 화면에서 사라진다", () => {
  assert.ok(!/테스트 무장애 공원 까지/.test($("naviSheetBody").textContent),
    "이전 경로 요약이 화면에 그대로 남아 있다 (말과 화면 불일치)");
  assert.match($("naviSheetBody").textContent, /경로를 안내하지 못했습니다/);
  assert.match($("naviStatus").textContent, /위치를 찾지 못했습니다/);
});
check("실패 안내는 '지역 밖'이라고 말하지 않는다", () => {
  assert.ok(!/안양시 밖/.test($("naviStatus").textContent),
    "이름을 못 찾은 것을 '지역 밖'으로 안내함");
  assert.match($("naviStatus").textContent, /지도에서 가려는 지점/);
});
check("실패 화면에서도 이름 검색·지도 지정으로 이어갈 수 있다", () => {
  assert.ok($("placeQ") && $("naviDestPick"));
});
check("실패한 목적지를 기억하지 않는다 (출발지를 바꿔도 같은 실패를 되풀이하지 않음)", () => {
  lastPlanQuery = null;
  mapClickHandlers[0]({ latLng: { getLat: () => 37.3902, getLng: () => 126.9503 } });
  assert.equal(lastPlanQuery, null, "실패한 도착지로 자동 재요청함");
});

await sleep(80);   // 앞선 출발지 지정이 부른 목록 재조회가 시트를 덮어쓰지 않도록

// ── 안내 중 실패: 진행 중 안내는 유지하되 그 사실을 말한다 ──
NAV.resetTrip();
planResponse = ROUTE;
NAV.requestRoute({ poi_id: "TBF-1", name: "테스트 무장애 공원" });
await sleep(60);
NAV.startGuidance();
await sleep(30);
planResponse = {
  status: "out_of_service_area", tool_name: "plan_accessible_route",
  message: "목적지 '서울시청'는 경로 안내가 가능한 지역(안양시) 밖입니다",
};
NAV.requestRoute({ poi_id: "", name: "서울시청", lat: 37.5665, lng: 126.9780, kind: "building" });
await sleep(60);
check("안내 중 새 목적지 실패 -> 진행 중 안내는 유지되고, 계속된다고 말한다", () => {
  assert.match($("naviStatus").textContent, /안내는 그대로 계속됩니다/);
  assert.match($("naviSheetBody").textContent, /안내 1 \/ 3/, "진행 중 스텝 카드가 사라짐");
});

// ── 대화(ui_action)로 온 실패도 화면에 반영된다 ──
window.NAVI.onUiAction({ action: "route_unavailable",
  payload: { reason: "place_not_found", kind: "목적지", place: "○○복지관" } });
await sleep(20);
check("안내 중에는 대화 실패 신호가 와도 안내를 끊지 않는다", () => {
  assert.match($("naviStatus").textContent, /안내는 그대로 계속됩니다/);
});
NAV.clearRouteDisplay();   // 안내 종료 상태로 되돌린다
NAV.resetTrip();
planResponse = ROUTE;
NAV.requestRoute({ poi_id: "TBF-1", name: "테스트 무장애 공원" });
await sleep(60);
window.NAVI.onUiAction({ action: "route_unavailable",
  payload: { reason: "out_of_service_area", kind: "목적지", place: "서울시청" } });
await sleep(20);
check("안내 전이면 대화 실패 신호로 이전 경로 표시를 정리한다", () => {
  assert.ok(!/테스트 무장애 공원 까지/.test($("naviSheetBody").textContent),
    "대화로 실패했는데 이전 경로가 화면에 남아 있다");
  assert.match($("naviStatus").textContent, /안양시 밖이라 경로를 안내할 수 없습니다/);
});


// ── 실시간 저상버스·역 설비 (v1.39.0, 02 v1.19.0) ──
const LIVE_ROUTE = JSON.parse(JSON.stringify(MMROUTE.ui_action.route));
LIVE_ROUTE.route_id = "r_live";
LIVE_ROUTE.routes[0].legs[1].realtime = {
  status: "success", items: [{ route_id: "241253001" }],
  next_low_floor: { route_id: "241253001", route_name: "2", predict_min: 4, stops_away: 2 },
};
LIVE_ROUTE.routes[0].legs[1].board.poi_id = "208000156";
LIVE_ROUTE.routes[0].legs.push({ kind: "subway", line: "1호선", station_cnt: 1, warnings: [], geometry: [],
  board: { name: "안양", poi_id: "3900039",
           facilities: { elevators: [{ exit_no: "2", detail_loc: "(2F) 1번출구 맞이방 서쪽" }, { exit_no: "내부", detail_loc: "x" }],
                         lifts: [], dis_toilet: "yes", safety_plate: "yes" } },
  alight: { name: "명학", poi_id: "KRNA_1_MHK", facilities: { elevators: [], lifts: [{ exit_no: "1", detail_loc: "계단 옆" }], dis_toilet: "unknown" } } });
LIVE_ROUTE.routes[0].steps[1].leg_ref = { kind: "bus", route_id: "241253001", route_name: "2",
  board_station_id: "208000156", board_name: "소방서", alight_station_id: "208000328" };
LIVE_ROUTE.routes[0].steps[2].leg_ref = LIVE_ROUTE.routes[0].steps[1].leg_ref;
const LIVE_PLAN = { status: "success", route_id: "r_live", mode_used: "walk_bus", mode_label: "도보+버스",
  ui_action: { action: "show_route", route: LIVE_ROUTE } };
let arrivalsResp = { status: "success", station_id: "208000156", items: [{ route_name: "2" }],
  next_low_floor: { route_name: "2", predict_min: 3, stops_away: 1 } };
const arrivalCalls = [];
const prevFetch2 = window.fetch;
window.fetch = async (url, opt) => {
  const u = String(url);
  if (u.includes("bus_arrivals")) { arrivalCalls.push(u); return { ok: true, json: async () => arrivalsResp }; }
  if (u.includes("plan_accessible_route")) { lastPlanQuery = u; return { ok: true, json: async () => LIVE_PLAN }; }
  return prevFetch2(url, opt);
};
NAV.clearRouteDisplay(); NAV.resetTrip();
NAV.requestRoute({ poi_id: "TBF-1", name: "테스트 무장애 공원" });
await sleep(60);
check("이동 방식 라벨은 요청 방식이 아니라 실제 구간(legs) 기준 (v1.41.0)", () => {
  // LIVE_ROUTE 는 mode 를 안 실었으므로 여기서 요청 방식만 지하철 포함으로 표시했다고 가정
  const NAVi = window.NAVI._internals();
  const noSub = JSON.parse(JSON.stringify(LIVE_ROUTE)); noSub.mode = "walk_bus_subway";
  noSub.routes[0].legs = noSub.routes[0].legs.filter((l) => l.kind !== "subway");
  NAVi.showRoute(noSub, "테스트 무장애 공원");
  const autoBtn = [...window.document.querySelectorAll(".mode-cards button")].find((b) => b.getAttribute("data-mode") === "auto");
  assert.match(autoBtn.textContent, /도보\+버스/);
  assert.doesNotMatch(autoBtn.textContent, /지하철/);
  NAV.requestRoute({ poi_id: "TBF-1", name: "테스트 무장애 공원" });
});
await sleep(60);
check("버스 카드: 실시간 저상 확인 결과가 고정 경고를 대체한다", () => {
  const cards = [...window.document.querySelectorAll(".leg-card")];
  const bus = cards.find((c) => /마을버스 2번/.test(c.textContent));
  assert.ok(bus, "버스 카드 없음");
  assert.match(bus.textContent, /저상 2번 약 4분 후 도착 \(2 정거장 전\)/);
  assert.doesNotMatch(bus.textContent, /보장되지 않습니다/);
});
check("지하철 카드: 승차역 승강기 출입구·장애인화장실, 하차역 리프트 경고", () => {
  const sub = [...window.document.querySelectorAll(".leg-card")].find((c) => /1호선/.test(c.textContent));
  assert.ok(sub, "지하철 카드 없음");
  assert.match(sub.textContent, /승차 🛗 안양역 승강기: 2번 출입구 — \(2F\) 1번출구 맞이방 서쪽 외 1곳 · ♿ 장애인화장실 있음/);
  assert.match(sub.textContent, /하차 ⚠ 명학역은 휠체어리프트만 있어/);
});
const startBtn2 = [...$("naviSheetBody").querySelectorAll("button")].find((b) => b.textContent === "안내 시작");
window.NAVI.attachWs(fakeWs);
startBtn2.dispatchEvent(new window.Event("click"));
await sleep(30);
check("안내 시작 시 nav_state 에 다음 승차 정류장·노선이 실린다", () => {
  const nav = wsSent.filter((m) => m.type === "nav_state" && m.route_id === "r_live");
  assert.ok(nav.length, "nav_state 없음");
  const last = nav.at(-1);
  assert.equal(last.board_station_id, "208000156");
  assert.equal(last.board_route_id, "241253001");
  assert.equal(last.board_stop_name, "소방서");
});
const nextBtn2 = [...$("naviSheetBody").querySelectorAll("button")].find((b) => b.textContent === "다음 ▶");
nextBtn2.dispatchEvent(new window.Event("click"));
await sleep(60);
check("승차 스텝: 실시간 도착정보를 조회해 카드에 표시하고 1회 음성 안내", () => {
  assert.ok(arrivalCalls.length >= 1, "bus_arrivals 미호출");
  assert.match(arrivalCalls[0], /station_id=208000156/);
  assert.match(arrivalCalls[0], /route_id=241253001/);
  const live = $("stepLive");
  assert.ok(live, "스텝 카드 실시간 표시 없음");
  assert.match(live.textContent, /저상 2번 약 3분 후 도착/);
  assert.ok(spoken.some((t) => /저상버스 2번이 약 3분 뒤 도착 예정/.test(t)), "저상 도착 음성 없음");
});
const spokenBeforeLive = spoken.length;
arrivalsResp = { status: "success", station_id: "208000156", items: [{ route_name: "2" }], next_low_floor: null };
await NAV.refreshArrivals({ board_station_id: "208000156", route_id: "241253001", route_name: "2" });
check("갱신 결과에 저상이 없으면 '저상이 아니다'로 표시하고 추가 음성은 없다", () => {
  assert.match($("stepLive").textContent, /지금 오는 2번 차량은 저상이 아닙니다/);
  assert.ok($("stepLive").classList.contains("off"));
  assert.equal(spoken.length, spokenBeforeLive);
});
arrivalsResp = { status: "unavailable", reason: "HTTP 403", items: [], next_low_floor: null };
await NAV.refreshArrivals({ board_station_id: "208000156", route_id: "241253001", route_name: "2" });
check("실시간 실패는 안내판 확인 문구로 — 안내는 계속", () => {
  assert.match($("stepLive").textContent, /실시간 도착정보를 받지 못했습니다/);
  assert.match($("naviSheetBody").textContent, /안내 2 \/ 4/);
});
nextBtn2.dispatchEvent(new window.Event("click"));
await sleep(30);
check("하차 스텝으로 넘어가면 실시간 표시가 사라지고 폴링이 멈춘다", () => {
  assert.equal($("stepLive"), null);
  assert.equal(NAV.arrivalPollingActive(), false);
});
window.fetch = prevFetch2;


// ── 상담 답변 숫자 표기 정규화·에코 판정 (v1.40.0) ──
const TN = window.__TEXT_NORM;
check("숫자 표기 정규화: 전화번호·주소·단위 수를 아라비아 숫자로", () => {
  assert.ok(TN, "__TEXT_NORM 노출 없음");
  assert.equal(TN.normalizeKoreanNumbers("대표전화는 일오칠칠에 천번입니다."), "대표전화는 1577-1000번입니다.");
  assert.equal(TN.normalizeKoreanNumbers("연락처는 공삼일 삼팔구 일이삼사입니다."), "연락처는 031-389-1234입니다.");
  assert.equal(TN.normalizeKoreanNumbers("안양지사는 관평로 백팔십이에 있습니다."), "안양지사는 관평로 182에 있습니다.");
  assert.equal(TN.normalizeKoreanNumbers("십오층입니다. 만원입니다."), "15층입니다. 10000원입니다.");
});
check("숫자 표기 정규화: 낱말·한 글자 수·이미 숫자인 문장은 그대로", () => {
  for (const t of ["구사일생으로 살아났다는 이야기입니다.", "이 층에 있어요. 삼층으로 가세요.", "그렇게 하십시오. 원래 그렇습니다.",
                   "관평로 182에 있고 대표전화는 1577-1000번입니다.", ""]) {
    assert.equal(TN.normalizeKoreanNumbers(t), t);
  }
});
check("에코 판정: 직전 상담원 발화 끝말과 같은 짧은 전사만 에코", () => {
  assert.equal(TN.looksLikeEcho("요", "지원 정책을 안내해 드릴게요"), true);
  assert.equal(TN.looksLikeEcho("게요.", "안내해 드릴게요."), true);
  assert.equal(TN.looksLikeEcho("네", "안내해 드릴게요"), false);
  assert.equal(TN.looksLikeEcho("아니요", "안내해 드릴게요"), false);
  assert.equal(TN.looksLikeEcho("", "안내해 드릴게요"), false);
});
check("상담 마이크 게이트: 상담원 음성 중·직후엔 지속 발화만 통과 (소스 레벨 가드)", () => {
  assert.match(HTML, /if \(aiAudioActive\(\)\) \{[\s\S]*?LOCAL_TTS_BARGE_RMS/, "상담 화면에도 barge-in 게이트가 적용돼야 한다");
  assert.doesNotMatch(HTML, /aiAudioActive\(\) && document\.getElementById\("view-navi"\)/, "navi 화면 한정 게이트가 남아 있다");
  assert.match(HTML, /AI_ECHO_TAIL_MS = 800/);
  assert.match(HTML, /echoTailActive\(\) && looksLikeEcho\(msg\.content, lastAiText\)/, "user_transcript 에코 억제 없음");
  assert.match(HTML, /echoTailActive\(\) && looksLikeEcho\(tr, lastAiText\)/, "STT 에코 억제 없음");
});

// ── v1.43.0 안내 발화 큐 · 횡단보도 병합 발화 · 진행거리 기준 전환 ──
{
  const NV = window.NAVI._internals();
  NV.resetTrip();
  // 스텝 좌표를 geometry 위에 둔다: 출발 → (105m) 횡단보도 노드 → 같은 좌표에서 횡단보도 링크(14m) → (14m) 좌회전 → (60m) 도착
  const P0 = [37.3900, 126.9500], P1 = [37.39094, 126.9500], P2 = [37.39107, 126.9500], P3 = [37.39161, 126.9500];
  NV.showRoute({ status: "success", route_id: "r_cw", destination: { poi_id: "TBF-CW" }, routes: [{
    summary: { total_distance_m: 193, duration_sec: 240, max_slope_deg: 1, stairs_cnt: 0, crossing_cnt: 1, warnings: [] },
    geometry: [P0, P1, P2, P3],
    steps: [
      { idx: 0, maneuver: "depart", instruction: "105m 앞으로 이동합니다.", distance_m: 105, coord: P0, warnings: [] },
      { idx: 1, maneuver: "crossing_point", instruction: "횡단보도가 있습니다. 횡단보도를 건너세요. (턱낮춤 미상)", distance_m: 0, coord: P1, warnings: ["턱낮춤 미상"] },
      { idx: 2, maneuver: "crossing", link_type: "crossing", instruction: "횡단보도를 건너 14m 이동합니다.", distance_m: 14, coord: P1, warnings: [] },
      { idx: 3, maneuver: "left", instruction: "좌회전 후 60m 이동합니다.", distance_m: 60, coord: P2, warnings: [] },
      { idx: 4, maneuver: "arrive", instruction: "목적지에 도착했습니다.", distance_m: 0, coord: P3, warnings: [] },
    ] }] }, "횡단보도 테스트");
  check("횡단보도 노드 스텝은 다음 링크 스텝과 한 문장으로 발화 (v1.43.0)", () => {
    assert.equal(NV.stepUtterance(1), "횡단보도가 있습니다. 횡단보도를 건너 14m 이동합니다. (턱낮춤 미상)");
    assert.equal(NV.stepUtterance(0), "105m 앞으로 이동합니다.");
    assert.equal(NV.stepUtterance(3), "좌회전 후 60m 이동합니다.");
  });
  // 가상 시계로 실보행 재현 — 1초마다 1m 씩 북쪽으로
  const realNow = window.Date.now; let vnow = realNow(); window.Date.now = () => vnow;   // 페이지 쪽 시계를 가상으로
  spoken.length = 0;
  NV.startGuidance();
  await sleep(5);
  const before = spoken.length;
  for (let t = 1; t <= 200; t++) {
    vnow += 1000;
    NV.setHere({ lat: P0[0] + 0.000009 * t, lng: P0[1] });
    NV.advanceStep();
    NV.drainSpeak();
    await sleep(0);   // 서버 합성 fetch 스텁(비동기) → 폴백 발화가 기록되도록 마이크로태스크를 비운다
  }
  vnow += 20000; NV.drainSpeak(); await sleep(5);
  const busyAfter = NV.speakBusy();
  window.Date.now = realNow;
  check("실보행: 노드 스텝→링크 스텝이 한 번만, 끊기지 않고 발화되고 다음 안내로 이어진다 (v1.43.0)", () => {
    const stepSpeaks = spoken.slice(before).filter((t) => !/다음 안내까지/.test(t));
    assert.deepEqual(stepSpeaks, [
      "횡단보도가 있습니다. 횡단보도를 건너 14m 이동합니다. (턱낮춤 미상)",
      "좌회전 후 60m 이동합니다.",
      "목적지에 도착했습니다.",
    ], JSON.stringify(spoken.slice(before)));
    assert.equal(busyAfter, false);
  });
  check("스텝 안내가 대기 중이면 중간 거리 안내는 버린다 / 즉시 발화는 대기열을 비운다 (v1.43.0)", () => {
    NV.speak("첫 스텝 안내입니다 첫 스텝 안내입니다", { queue: true, kind: "step" });   // 재생 중(최소 보장 시간 안)
    NV.speak("두 번째 스텝 안내입니다", { queue: true, kind: "step" });
    NV.speak("다음 안내까지 약 300미터입니다", { queue: true, kind: "interim" });
    assert.equal(NV.speakBusy(), true);
    NV.speak("경로를 벗어나셨어요.");   // 즉시 발화 — 대기열 폐기
    assert.equal(NV.speakBusy(), true, "즉시 발화도 재생 중으로 잡혀야 한다");
  });
  await sleep(5);
  check("즉시 발화가 대기열을 비우고 마지막에 재생된다 (v1.43.0)", () => {
    assert.equal(spoken.at(-1), "경로를 벗어나셨어요.");
    assert.equal(spoken.includes("두 번째 스텝 안내입니다"), false, "폐기됐어야 할 대기 안내가 재생됐다");
    assert.equal(spoken.includes("다음 안내까지 약 300미터입니다"), false, "폐기됐어야 할 중간 안내가 재생됐다");
  });
  check("모의 주행은 안내 음성이 재생 중이면 가상 이동을 멈춘다 (소스 레벨 가드)", () => {
    assert.match(HTML, /_drainSpeak\(\);\s*if\(speakBusy\(\)\) return;\s*\/\/ 안내 음성이 끝날 때까지 가상 이동 일시정지/);
    assert.match(HTML, /if\(_reachedStep\(simPos, stepIdx\+1\)\)/);
    assert.match(HTML, /if\(_reachedStep\(here, stepIdx\+1\)\)/);
  });
  NV.resetTrip();
}

// ── v1.43.1 합성 대기 안전 — 서버 TTS 가 5~9초 걸려도 잘리거나 폐기되지 않는다 ──
{
  const NV = window.NAVI._internals();
  const realNow2 = window.Date.now; let vnow2 = realNow2() + 60000; window.Date.now = () => vnow2;   // 앞 블록의 발화가 끝난 뒤
  const prevFetch3 = window.fetch;
  let pendingTts = [];                       // 지연 합성: resolve 를 손으로 호출
  window.fetch = async (url, opt) => {
    const u = String(url);
    if (u.includes("/api/v1/tts")) return new Promise((res) => pendingTts.push({ url: u, res }));
    return prevFetch3(url, opt);
  };
  const played = [];
  window.URL.createObjectURL = () => "blob:x";
  window.Audio = function(){ const self = this; this.duration = 3.0; this.play = () => { played.push(self); return Promise.resolve(); }; this.pause = () => { self.paused = true; }; };
  const resolveOne = (i = 0) => { const p = pendingTts.splice(i, 1)[0]; if (p) p.res({ ok: true, blob: async () => ({}) }); };

  NV.speak("첫 안내 문장입니다", { queue: true, kind: "step" });
  await sleep(2);
  check("합성 대기 중에는 추정 길이가 지나도 busy 를 유지한다 (v1.43.1)", () => {
    assert.equal(pendingTts.length, 1, "합성 요청이 하나 나가야 한다");
    vnow2 += 6000;                          // 종전 창(추정×1.3+1s ≈ 4.9s)을 넘겨도
    assert.equal(NV.speakBusy(), true);
  });
  NV.speak("두 번째 안내 문장입니다", { queue: true, kind: "step" });
  await sleep(2);
  check("합성 대기 중 도착한 다음 스텝은 앞 안내를 끊지 않고 대기열에 남는다 (v1.43.1)", () => {
    assert.equal(pendingTts.length, 1, "대기 중인 문장을 끊고 새로 합성하면 안 된다");
    assert.equal(played.length, 0);
  });
  resolveOne(); await sleep(5);
  check("합성이 끝나면 실제 재생 시작 시각 + duration 으로 종료를 판정한다 (v1.43.1)", () => {
    assert.equal(played.length, 1, "첫 문장이 재생돼야 한다");
    vnow2 += 1500; NV.drainSpeak();
    assert.equal(played[0].paused, undefined, "재생 1.5초 만에 끊겼다");
    assert.equal(pendingTts.length, 0, "재생 중엔 다음 합성을 시작하지 않는다");
  });
  vnow2 += 3000 * 1.3 + 1100; NV.drainSpeak(); await sleep(5);   // onended 가 없어도 상한이 지나면 다음으로
  check("상한이 지나면 대기열의 다음 문장을 합성한다 (v1.43.1)", () => {
    assert.equal(pendingTts.length, 1);
    assert.match(pendingTts[0].url, /%EB%91%90%20%EB%B2%88%EC%A7%B8|두 번째/);
  });
  // 합성이 12초를 넘기면 내장 TTS 로 대신 말하고, 늦게 온 서버 음성은 재생하지 않는다
  const spokenBefore3 = spoken.length;
  vnow2 += 12100; await sleep(20);
  check("합성이 12초를 넘기면 내장 TTS 로 대신 말한다 (v1.43.1)", () => {
    // 실제 타이머는 실시간이라 안 돌았다 → drainSpeak 의 지각 폴백 경로
    NV.drainSpeak();
    assert.equal(spoken.slice(spokenBefore3).filter((t) => t === "두 번째 안내 문장입니다").length, 1);
  });
  resolveOne(); await sleep(5);
  check("늦게 도착한 서버 음성은 재생하지 않는다 — 중복 재생 방지 (v1.43.1)", () => {
    assert.equal(played.length, 1, "폴백 후 서버 음성이 또 재생됐다");
  });
  check("같은 문장의 합성 요청은 한 번만 나간다 / 모바일은 브라우저 STT 를 쓰지 않는다 (소스 레벨 가드)", () => {
    assert.match(HTML, /if\(ttsPending\[key\]\) return ttsPending\[key\];/);
    assert.match(HTML, /if \(isMobileUA\(\)\) return false;/);
    assert.match(HTML, /try\{ prefetchTts\(\); \}catch\(e\)\{\}/, "경로 표시 시점 프리페치 없음");
  });
  NV.resetTrip(); NV.speak("정리");   // 대기열·타이머 정리
  window.fetch = prevFetch3; window.Date.now = realNow2;
}

// ── 결과 ──
let failed = 0;
for (const [st, name] of results) {
  console.log(`${st === "PASS" ? "  ok" : "FAIL"}  ${name}`);
  if (st === "FAIL") failed++;
}
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
