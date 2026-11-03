/**
 * 운영 콘솔 [접근성 제보] 탭 — 사진 보기 (v2.24.0) jsdom 런타임 검증
 *
 *   1) 사진이 있는 제보는 '보기' 버튼, 없는 제보는 '-' — 새 창 링크(target=_blank)는 없다
 *   2) 보기 → 사진 창이 열리고 응답 바이트를 허용 형식으로 고정한 Blob 을 <img> 에 넣는다
 *   3) 응답 형식이 허용 이미지가 아니면(내려받기 응답 등) 표시하지 않고 안내 문구만
 *   4) 404 · 연결 실패 문구, 닫으면 Blob 주소 해제
 *   5) 연달아 누르면 마지막 요청만 반영
 *   6) 탭 전체 전환 중 오류 없음, 제보 탭 버튼 핸들러가 함수
 */
import { readFileSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";
import assert from "node:assert/strict";

const HTML = readFileSync(new URL("../../static/admin.html", import.meta.url), "utf8");
const results = [];
async function check(name, fn) {
  try { await fn(); results.push(["PASS", name]); }
  catch (e) { results.push(["FAIL", name + " — " + e.message]); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const REPORTS = { count: 2, items: [
  { report_id: 11, lat: 37.39, lon: 126.95, reason: "curb", detail: "<b>턱</b>", has_photo: true, status: "new", created_at: "2026-11-02T09:00:00" },
  { report_id: 12, lat: 37.40, lon: 126.96, reason: "steep", detail: null, has_photo: false, status: "rejected", created_at: "2026-11-02T09:10:00" },
] };
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);
let photoReply = () => ({ status: 200, type: "image/png", body: PNG });
let photoDelay = 0;
const photoCalls = [];
const blobs = [];
const revoked = [];
const errors = [];

const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push(String(e && e.message || e)));
vc.on("error", (e) => errors.push(String(e)));

const dom = new JSDOM(HTML, {
  runScripts: "dangerously", pretendToBeVisual: true, url: "https://example.test/admin", virtualConsole: vc,
  beforeParse(window) {
    window.alert = () => {}; window.confirm = () => true; window.prompt = () => null;
    let n = 0;
    window.URL.createObjectURL = (b) => { blobs.push(b); return "blob:https://example.test/" + (++n); };
    window.URL.revokeObjectURL = (u) => { revoked.push(u); };
    window.fetch = async (url) => {
      const u = String(url);
      const m = u.match(/^\/admin\/api\/nav-reports\/(\d+)\/photo$/);
      if (m) {
        photoCalls.push(+m[1]);
        const rep = photoReply(+m[1]);
        if (photoDelay) await sleep(photoDelay);
        if (rep.throw) throw new Error("network");
        return { ok: rep.status === 200, status: rep.status,
          headers: { get: (k) => (k.toLowerCase() === "content-type" ? rep.type : null) },
          arrayBuffer: async () => rep.body.buffer.slice(0),
          json: async () => ({ detail: "사진이 없습니다" }) };
      }
      if (u.startsWith("/admin/api/nav-reports")) return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(REPORTS)) };
      return { ok: true, status: 200, json: async () => ([]) };
    };
  },
});
const { window } = dom;
const doc = window.document;
const $ = (id) => doc.getElementById(id);
await sleep(30);

await check("탭 전체 전환 중 오류 없음", async () => {
  for (const t of ["overview", "ops", "policy", "review", "unresolved", "discovery", "mobility", "navrep"]) {
    window.showTab(t); await sleep(15);
  }
  for (const f of ["nrShowPhoto", "nrClosePhoto", "nrAct", "nrApply", "nrDelete", "loadNavReports"]) {
    assert.equal(typeof window[f], "function", f);
  }
  assert.deepEqual(errors, []);
});

await check("사진 있는 제보는 보기 버튼, 없는 제보는 '-' · 새 창 링크 없음", () => {
  const rows = [...doc.querySelectorAll("#nr-table tbody tr")];
  assert.equal(rows.length, 2);
  const cell = rows[0].children[5], cell2 = rows[1].children[5];
  assert.equal(cell.querySelector("button").textContent, "보기");
  assert.equal(cell.querySelector("a"), null);
  assert.equal(cell2.textContent, "-");
  assert.equal(doc.querySelectorAll('#nr-table a[href*="/photo"]').length, 0);
  assert.equal(rows[0].children[3].querySelectorAll("b").length, 0);   // 상세는 이스케이프
});

await check("보기 → 사진 창에 허용 형식 Blob 을 <img> 로 표시", async () => {
  doc.querySelector("#nr-table tbody tr button").click();
  await sleep(20);
  assert.ok($("nrPhotoModal").classList.contains("on"));
  assert.equal(photoCalls.at(-1), 11);
  const img = $("nrPhotoImg");
  assert.equal(img.tagName, "IMG");
  assert.equal(img.hidden, false);
  assert.match(img.getAttribute("src"), /^blob:/);
  assert.equal(blobs.at(-1).type, "image/png");
  assert.equal(blobs.at(-1).size, PNG.length);
  assert.match($("nrPhotoTitle").textContent, /제보 #11 사진/);
  assert.equal($("nrPhotoMsg").textContent, "");
});

await check("닫으면 Blob 주소 해제 · 창 닫힘", async () => {
  const src = $("nrPhotoImg").getAttribute("src");
  window.nrClosePhoto();
  assert.equal($("nrPhotoModal").classList.contains("on"), false);
  assert.equal($("nrPhotoImg").hidden, true);
  assert.equal($("nrPhotoImg").getAttribute("src"), null);
  assert.ok(revoked.includes(src));
});

await check("허용 이미지가 아닌 응답(내려받기 형식·문서 형식)은 표시하지 않는다", async () => {
  for (const type of ["application/octet-stream", "text/html; charset=utf-8", "image/svg+xml", null]) {
    const before = blobs.length;
    photoReply = () => ({ status: 200, type, body: PNG });
    await window.nrShowPhoto(11);
    assert.equal(blobs.length, before, String(type));
    assert.equal($("nrPhotoImg").hidden, true);
    assert.equal($("nrPhotoImg").getAttribute("src"), null);
    assert.match($("nrPhotoMsg").textContent, /이미지 형식\(JPEG·PNG·WebP\)이 아니어서 표시하지 않습니다/);
  }
  photoReply = () => ({ status: 200, type: "IMAGE/JPEG", body: PNG });
  await window.nrShowPhoto(11);
  assert.equal(blobs.at(-1).type, "image/jpeg");                 // 형식 값은 소문자 정규형으로 고정
  window.nrClosePhoto();
});

await check("404 · 연결 실패 문구", async () => {
  photoReply = () => ({ status: 404, type: "application/json", body: PNG });
  await window.nrShowPhoto(11);
  assert.equal($("nrPhotoMsg").textContent, "사진이 없습니다.");
  photoReply = () => ({ status: 502, type: "application/json", body: PNG });
  await window.nrShowPhoto(11);
  assert.equal($("nrPhotoMsg").textContent, "사진을 불러오지 못했습니다.");
  photoReply = () => ({ throw: true });
  await window.nrShowPhoto(11);
  assert.equal($("nrPhotoMsg").textContent, "사진을 불러오지 못했습니다.");
  assert.equal($("nrPhotoImg").hidden, true);
  window.nrClosePhoto();
});

await check("연달아 누르면 마지막 요청만 반영, 닫은 뒤 늦은 응답은 무시", async () => {
  photoReply = (id) => ({ status: 200, type: id === 11 ? "image/png" : "image/webp", body: PNG });
  photoDelay = 20;
  const before = blobs.length;
  const p1 = window.nrShowPhoto(11), p2 = window.nrShowPhoto(13);
  await Promise.all([p1, p2]);
  assert.equal(blobs.length, before + 1);
  assert.equal(blobs.at(-1).type, "image/webp");
  assert.match($("nrPhotoTitle").textContent, /제보 #13 사진/);
  const p3 = window.nrShowPhoto(11); window.nrClosePhoto(); await p3;
  assert.equal(blobs.length, before + 1);
  assert.equal($("nrPhotoModal").classList.contains("on"), false);
  photoDelay = 0;
});

await check("창 바깥(배경)을 누르면 닫힌다", async () => {
  photoReply = () => ({ status: 200, type: "image/png", body: PNG });
  await window.nrShowPhoto(11);
  $("nrPhotoModal").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  assert.equal($("nrPhotoModal").classList.contains("on"), false);
  assert.deepEqual(errors, []);
});

for (const [r, n] of results) console.log(r, n);
const failed = results.filter(([r]) => r === "FAIL").length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
