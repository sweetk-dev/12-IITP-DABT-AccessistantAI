/**
 * 웹에서 계정 바꾸기 (jsdom) — v2.35.0
 *   1) 정책상담 첫 화면 위쪽에 지금 계정 이름이 보인다
 *   2) 누르고 확인하면 /api/v1/account/switch 를 부른다 · 취소하면 부르지 않는다
 *   3) 계정을 모르면(인증 없는 배포) 버튼을 숨긴다
 *   4) 이동경로 안내(/navi)와 앱 안에서는 보이지 않는다(앱은 앱의 로그아웃)
 *   5) 테마 색은 흰색 — 휴대폰 아래 내비게이션 바 아이콘이 파란 바탕에 묻히지 않게
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

const HTML = readFileSync(new URL("../../static/accessistant.html", import.meta.url), "utf8");
const results = [];
function check(name, fn) { try { fn(); results.push(["PASS", name]); } catch (e) { results.push(["FAIL", name + " — " + (e && e.message)]); } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function boot(url, opts) {
  opts = opts || {};
  const dom = new JSDOM(HTML, { runScripts: "dangerously", pretendToBeVisual: true, url,
    beforeParse(w) {
      if (opts.app) w.AccessNaviApp = { logout() {} };
      w.WebSocket = class { constructor() { this.readyState = 0; } send() {} close() {} };
      w.__calls = [];
      w.fetch = async (u0, o) => { const u = String(u0); w.__calls.push([u, (o && o.method) || "GET"]);
        if (u.includes("/api/v1/whoami")) return { ok: true, json: async () => ({ user: opts.user === undefined ? "P2" : opts.user }) };
        if (u.includes("/api/v1/config")) return { ok: true, json: async () => ({ features: { route: true, tour: true }, region: { name: "안양시", bbox: [37.33, 126.87, 37.45, 127.0] } }) };
        return { ok: true, json: async () => ({ ok: true }) }; };
      w.navigator.geolocation = { watchPosition: () => 1, clearWatch: () => {}, getCurrentPosition: () => {} };
      w.navigator.mediaDevices = { getUserMedia: async () => { throw new Error("no mic"); } };
      w.speechSynthesis = { cancel() {}, speak() {} };
      w.HTMLMediaElement.prototype.play = async () => {}; w.HTMLMediaElement.prototype.pause = () => {};
      const mk = () => new Proxy(function () {}, { get: (t, k) => (k === "then" ? undefined : mk()), apply: () => mk(), construct: () => mk() });
      w.kakao = { maps: new Proxy({ load: (cb) => cb() }, { get: (t, k) => (k in t ? t[k] : mk()) }) };
    } });
  return dom.window;
}
const ready = async (w) => { await sleep(50); w.document.dispatchEvent(new w.Event("DOMContentLoaded")); await sleep(150); };

{
  const w = boot("https://example.test/static/accessistant.html"); await ready(w);
  const b = w.document.getElementById("acctBtn");
  check("정책상담: 첫 화면 위쪽에 지금 계정 이름", () => { assert.equal(b.hidden, false); assert.equal(b.textContent, "P2"); assert.ok(b.closest("#view-mode .appbar")); });
  w.confirm = () => false; b.click(); await sleep(20);
  check("취소하면 바꾸지 않는다", () => assert.ok(!w.__calls.some(([u]) => u.includes("/account/switch"))));
  w.confirm = (m) => { w.__asked = m; return true; }; b.click(); await sleep(20);
  check("확인하면 계정 바꾸기를 부른다", () => {
    assert.ok(w.__calls.some(([u, m]) => u.includes("/api/v1/account/switch") && m === "POST"));
    assert.match(w.__asked, /지금 계정: P2/);
  });
  w.close();
}
{
  const w = boot("https://example.test/iitp/accessistantai/static/accessistant.html"); await ready(w);
  check("하위 경로에서도 기준 경로 아래로 묻는다", () => assert.ok(w.__calls.some(([u]) => u.includes("/iitp/accessistantai/api/v1/whoami"))));
  w.close();
}
{
  const w = boot("https://example.test/static/accessistant.html", { user: null }); await ready(w);
  check("계정을 모르면 버튼을 숨긴다", () => assert.equal(w.document.getElementById("acctBtn").hidden, true));
  w.close();
}
{
  const w = boot("https://example.test/navi", { app: true }); await ready(w);
  check("앱(이동경로 안내)에서는 보이지 않고 계정도 묻지 않는다", () => {
    assert.equal(w.document.getElementById("acctBtn").hidden, true);
    assert.ok(!w.__calls.some(([u]) => u.includes("/whoami")));
  });
  w.close();
}
check("테마 색은 흰색", () => assert.match(HTML, /<meta name="theme-color" content="#ffffff">/));
const SW = readFileSync(new URL("../../static/sw.js", import.meta.url), "utf8");
check("계정 바꾸기 요청은 서비스워커가 가로채지 않고, 실패 응답은 담지 않는다", () => {
  assert.match(SW, /searchParams\.has\("acct"\)\) return;/);
  assert.match(SW, /if \(res\.ok\)/);
  assert.match(HTML, /location\.replace\(location\.pathname\+"\?acct="/);
});

// ───────── v2.35.2 ─────────
{
  const w = boot("https://example.test/static/accessistant.html"); await ready(w);
  const m = w.document.querySelector('link[rel="manifest"]');
  check("(v2.35.2) 매니페스트는 인증 정보와 함께 가져온다(crossorigin=use-credentials)", () => { assert.ok(m); assert.equal(m.getAttribute("crossorigin"), "use-credentials"); });
  w.close();
}
check("(v2.35.2) 재생기 진단 — 만들 때·상태 바뀔 때 실증 기록에 남긴다", () => {
  assert.match(HTML, /trialEv\("ai_ctx", \{ rate: ctx\.sampleRate, state: ctx\.state/);
  assert.match(HTML, /ctx\.onstatechange = \(\) => trialEv\("ai_ctx_state"/);
});
check("(v2.35.2) 시험(?pb=native)만 기본 표본율로 만들고 닫지 않는다 — 기본 동작은 24kHz·중단 때 닫기 그대로", () => {
  assert.match(HTML, /PB_MODE === "native" \? new C\(\) : new C\(\{ sampleRate: rate \}\)/);
  assert.match(HTML, /if \(playbackCtx && PB_MODE !== "native"\) \{/);
});

let failed = 0;
for (const [st, name] of results) { console.log(`${st === "PASS" ? "  ok" : "FAIL"}  ${name}`); if (st === "FAIL") failed++; }
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
