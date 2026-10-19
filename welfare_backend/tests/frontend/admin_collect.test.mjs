/**
 * 운영 콘솔 [운영] 탭 — 데이터 수집 상태·알림 설정 (v2.2.0) jsdom 런타임 검증
 *
 *   1) 작업별 표: 마지막 정기 실행·결과 배지·처리 건수·연속 실패·알림 결과
 *   2) 실패 목록 펼침, 정기 실행 뒤의 수동 시험 실행 안내
 *   3) 알림 설정 표시 → 저장(PUT 본문) → 저장 실패(422) 문구
 *   4) 입력 중에는 자동 새로고침이 입력값을 덮어쓰지 않는다
 *   5) 표가 없는 DB(ready=false)·iitp_db 미설정(503) 안내
 *   6) 분류 머리행(통계 / 이동편의 / 문화·체육)과 종류별 현재 적재 건수 — 0건 종류 경고색 (v2.3.0)
 *   7) 종류별 메모(고용 파일데이터 표별 판 기준일)를 건수 뒤 괄호로 표시 (v2.5.0)
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

const HTML = readFileSync(new URL("../../static/admin.html", import.meta.url), "utf8");
const results = [];
async function check(name, fn) {
  try { await fn(); results.push(["PASS", name]); }
  catch (e) { results.push(["FAIL", name + " — " + e.message]); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const STATUS = {
  ready: true,
  alert: { enabled: true, emails: ["ops@example.co.kr"], updated_at: "2026-10-13 15:00", updated_by: "admin-console" },
  jobs: [
    { ext_sys: "KOSIS", label: "KOSIS 통계", schedule: "매월 3·18일 03:00", group: "통계", last_sync: "2026-10-03 03:00",
      consecutive_fail: 2, last_success: "2026-09-03 03:05",
      last_regular: { run_id: 10, started_at: "2026-10-03 03:00", status: "PARTIAL", targets: 24, ok_cnt: 20, fail_cnt: 4,
        mode: "db", scope: "ALL", alert_status: "SENT", alert_detail: "이상 알림 1명",
        failures: [{ id: "DT_11761_N007", stage: "collect", reason: "KOSIS data API 오류 응답: err=20(<b>구조</b>)" }] },
      last_run: { run_id: 12, started_at: "2026-10-13 15:10", status: "SUCCESS", mode: "file", scope: "PARTIAL" },
      recent: [] },
    { ext_sys: "ODCLOUD_EMP", label: "장애인 고용 파일데이터(14종)", schedule: "매월 3일 04:40", group: "통계",
      last_sync: "2026-11-03 04:45", consecutive_fail: 0, last_success: "2026-11-03 04:40",
      breakdown: [{ key: "emp_dis_jobseeker_status", label: "구직자", count: 44588, note: "2025-12-31 · 판 2개" },
                  { key: "emp_dis_job_posting", label: "구인", count: 0, note: "2024-12-31" },
                  { key: "emp_dis_center_usage", label: "훈련<센터>", count: 19, note: null }],
      last_regular: { run_id: 15, started_at: "2026-11-03 04:40", status: "SUCCESS", targets: 14, ok_cnt: 14, fail_cnt: 0,
        mode: "db", scope: "ALL", alert_status: "NONE", alert_detail: "정상", failures: [] },
      last_run: { run_id: 15, started_at: "2026-11-03 04:40", status: "SUCCESS", mode: "db", scope: "ALL" }, recent: [] },
    { ext_sys: "GBIS", label: "경기 버스 노선(GBIS)", schedule: "매주 월 04:00", group: "이동편의", last_sync: "2026-10-12 04:00",
      consecutive_fail: 0, last_success: "2026-10-12 04:00",
      last_regular: { run_id: 11, started_at: "2026-10-12 04:00", status: "SUCCESS", targets: 1, ok_cnt: 1, fail_cnt: 0,
        mode: "db", scope: "ALL", alert_status: "NONE", alert_detail: "정상", failures: [] },
      last_run: { run_id: 11, started_at: "2026-10-12 04:00", status: "SUCCESS", mode: "db", scope: "ALL" },
      recent: [] },
    { ext_sys: "GG_TOILET", label: "경기 공중화장실", schedule: "분기 1일 04:20", group: "이동편의", last_sync: "2026-09-30 19:20",
      consecutive_fail: 0, last_success: null, last_regular: null, last_run: null, recent: [] },
    { ext_sys: "TOUR_BF_API", label: "무장애 관광지", schedule: "매주 일 04:00", group: "이동편의", last_sync: null,
      consecutive_fail: 0, last_success: null, last_regular: null,
      last_run: { run_id: 13, started_at: "2026-10-13 16:00", status: "SUCCESS", mode: "file", scope: "ALL" }, recent: [] },
    { ext_sys: "GG_CULTURE", label: "경기 문화·체육 시설(4종)", schedule: "매월 2일 04:20", group: "문화·체육",
      last_sync: "2026-10-14 16:00", consecutive_fail: 1, last_success: null,
      breakdown: [{ key: "braille_lib", label: "점자도서관", count: 0 }, { key: "booknarae_lib", label: "책나래 제공도서관", count: 260 },
                  { key: "sports_fac", label: "장애인<체육>시설", count: 5 }, { key: "sports_org", label: "장애인체육회", count: 32 }],
      last_regular: { run_id: 14, started_at: "2026-10-14 16:00", status: "PARTIAL", targets: 297, ok_cnt: 297, fail_cnt: 0,
        mode: "db", scope: "ALL", alert_status: "SENT", alert_detail: "이상 알림 1명",
        failures: [{ id: "GG_CULTURE:braille_lib", stage: "collect", reason: "점자도서관 수집 실패: ERROR-500" }] },
      last_run: { run_id: 14, started_at: "2026-10-14 16:00", status: "PARTIAL", mode: "db", scope: "ALL" }, recent: [] },
  ],
};

let mode = "normal";
const puts = [];
let putReply = null;

const dom = new JSDOM(HTML, {
  runScripts: "dangerously", pretendToBeVisual: true, url: "https://example.test/admin",
  beforeParse(window) {
    window.alert = () => {}; window.confirm = () => true;
    window.fetch = async (url, opt = {}) => {
      const u = String(url);
      if (u.startsWith("/admin/api/collect/status")) {
        if (mode === "noconf") return { ok: false, status: 503, json: async () => ({ detail: "IITP DB not configured" }) };
        if (mode === "notready") return { ok: true, status: 200, json: async () => ({ ready: false, jobs: [], alert: null }) };
        return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(STATUS)) };
      }
      if (u.startsWith("/admin/api/collect/alert-config") && opt.method === "PUT") {
        puts.push(JSON.parse(opt.body));
        return putReply || { ok: true, status: 200, json: async () => ({ enabled: true, emails: ["a@x.co.kr", "b@x.co.kr"],
          updated_at: "2026-10-13 16:00", updated_by: "admin-console" }) };
      }
      if (u.startsWith("/admin/api/ops/status")) return { ok: true, status: 200, json: async () => ({ schedules: {}, next: {} }) };
      return { ok: true, status: 200, json: async () => ([]) };
    };
  },
});
const { window } = dom;
const $ = (id) => window.document.getElementById(id);
await sleep(30);

window.showTab("ops");
await sleep(30);

await check("작업별 표가 그려진다", () => {
  const rows = [...window.document.querySelectorAll("#ops-collect tbody tr[data-sys]")];
  assert.deepEqual(rows.map((r) => r.dataset.sys), ["KOSIS", "ODCLOUD_EMP", "GBIS", "GG_TOILET", "TOUR_BF_API", "GG_CULTURE"]);
  const k = rows[0].textContent;
  assert.match(k, /KOSIS 통계/); assert.match(k, /2026-10-03 03:00/); assert.match(k, /일부 실패/);
  assert.match(k, /20\/24/); assert.match(k, /2회/); assert.match(k, /마지막 성공 2026-09-03 03:05/); assert.match(k, /발송/);
  assert.match(rows[2].textContent, /정상/);
  assert.match(rows[3].textContent, /기록 없음/); assert.match(rows[3].textContent, /동기화 2026-09-30 19:20/);
});

await check("실패 목록 펼침 + 수동 시험 실행 안내, 사유는 HTML 이스케이프", () => {
  const det = window.document.querySelector("#ops-collect details.co-fail");
  assert.ok(det); assert.match(det.querySelector("summary").textContent, /실패 1건 보기/);
  const li = det.querySelector("li");
  assert.match(li.textContent, /DT_11761_N007 · 수집 — KOSIS data API 오류 응답: err=20\(<b>구조<\/b>\)/);
  assert.equal(li.querySelectorAll("b").length, 1);   // 통계표 ID 만 굵게 — 사유 속 태그는 글자로
  assert.match($("ops-collect").textContent, /최근 수동 실행: 2026-10-13 15:10 \(file·PARTIAL\)/);
});

await check("정기 실행 기록 없이 수동 실행만 있는 작업도 안내한다", () => {
  assert.match($("ops-collect").textContent, /최근 수동 실행: 2026-10-13 16:00 \(file·ALL\)/);
});

await check("다시 그려도 펼쳐 둔 실패 목록은 펼친 채", async () => {
  window.document.querySelector("#ops-collect details.co-fail").open = true;
  await window.loadCollect();
  assert.equal(window.document.querySelector("#ops-collect details.co-fail").open, true);
});

await check("분류 머리행 — 분류마다 한 번, 작업보다 앞에", () => {
  const trs = [...window.document.querySelectorAll("#ops-collect tbody tr")];
  const heads = trs.filter((r) => r.classList.contains("co-grp")).map((r) => r.textContent);
  assert.deepEqual(heads, ["통계", "이동편의", "문화·체육"]);
  const iHead = trs.findIndex((r) => r.textContent === "문화·체육");
  assert.equal(trs[iHead + 1].dataset.sys, "GG_CULTURE");
  assert.equal(trs.find((r) => r.classList.contains("co-grp")).querySelector("td").getAttribute("colspan"), "7");
});

await check("종류별 현재 적재 건수 — 0건 종류는 경고색, 이름은 이스케이프", () => {
  const row = window.document.querySelector('#ops-collect tr[data-sys="GG_CULTURE"]');
  const bd = row.querySelector(".co-bd");
  assert.ok(bd);
  assert.match(bd.textContent, /현재 점자도서관 0 · 책나래 제공도서관 260 · 장애인<체육>시설 5 · 장애인체육회 32/);
  const bad = bd.querySelectorAll(".b.bad");
  assert.equal(bad.length, 1); assert.equal(bad[0].textContent, "점자도서관 0");
  assert.equal(bd.querySelectorAll("체육").length, 0);
  assert.equal(window.document.querySelector('#ops-collect tr[data-sys="KOSIS"] .co-bd'), null);   // 대상 아닌 작업은 없음
  assert.match(row.textContent, /일부 실패/);
  assert.match($("ops-collect").textContent, /GG_CULTURE:braille_lib · 수집 — 점자도서관 수집 실패/);
});

await check("종류별 메모(판 기준일)는 건수 뒤 괄호 — 없으면 생략, 0건은 경고색 (v2.5.0)", () => {
  const row = window.document.querySelector('#ops-collect tr[data-sys="ODCLOUD_EMP"]');
  const bd = row.querySelector(".co-bd");
  assert.match(bd.textContent, /현재 구직자 44588\(2025-12-31 · 판 2개\) · 구인 0\(2024-12-31\) · 훈련<센터> 19$/);
  const bad = bd.querySelectorAll(".b.bad");
  assert.equal(bad.length, 1); assert.equal(bad[0].textContent, "구인 0");
  assert.equal(bd.querySelectorAll("센터").length, 0);           // 이름은 이스케이프
  assert.match(row.textContent, /14\/14/);
});

await check("알림 설정이 채워진다", () => {
  assert.equal($("caEnabled").checked, true);
  assert.equal($("caEmails").value, "ops@example.co.kr");
  assert.match($("caMsg").textContent, /마지막 수정 2026-10-13 15:00 · admin-console/);
});

await check("입력 중에는 새로고침이 덮어쓰지 않는다", async () => {
  $("caEmails").value = "a@x.co.kr, b@x.co.kr";
  $("caEmails").dispatchEvent(new window.Event("input"));
  await window.loadCollect();
  assert.equal($("caEmails").value, "a@x.co.kr, b@x.co.kr");
});

await check("저장 — PUT 본문과 결과 반영", async () => {
  await window.saveCollectAlert();
  assert.deepEqual(puts.at(-1), { enabled: true, emails: "a@x.co.kr, b@x.co.kr" });
  assert.match($("caMsg").textContent, /저장했습니다/);
  assert.equal($("caEmails").value, "a@x.co.kr, b@x.co.kr");
  assert.equal($("caSave").disabled, false);
});

await check("저장 실패(422) 문구", async () => {
  putReply = { ok: false, status: 422, json: async () => ({ detail: { error: "메일 주소 형식이 맞지 않습니다: nope" } }) };
  $("caEmails").value = "nope"; $("caEmails").dispatchEvent(new window.Event("input"));
  await window.saveCollectAlert();
  assert.match($("caMsg").textContent, /저장 실패: 메일 주소 형식이 맞지 않습니다: nope/);
  putReply = null;
});

await check("표가 없는 DB 안내 + 입력 비활성", async () => {
  mode = "notready"; window.caDirty = false;
  await window.loadCollect();
  assert.match($("ops-collect").textContent, /v1\.9\.0/);
  assert.equal($("caEmails").disabled, true);
});

await check("iitp_db 미설정 안내", async () => {
  mode = "noconf";
  await window.loadCollect();
  assert.match($("ops-collect").textContent, /IITP_DB_NAME/);
});

for (const [r, n] of results) console.log(r, n);
const failed = results.filter(([r]) => r === "FAIL").length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
