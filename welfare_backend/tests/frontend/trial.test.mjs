/**
 * 실증 참여자 계정 전용 기록 (v1.58.0, #318) — jsdom 런타임 검증
 *
 * 서버가 /api/v1/trial/status 에 enabled=true 로 답하는 경우만 본다(아닌 경우는 navi.test.mjs 끝에서 확인).
 *   1) 기록 식별자(rid)가 생기고 웹소켓 주소에 붙는다
 *   2) 길안내 문장 재생·끊김·버려진 문장이 사건으로 올라간다
 *   3) 마이크 연속 녹음 조각이 올라가고, 업로드가 실패하면 다시 보낸다
 *   4) 기록 전송이 실패해도 예외가 밖으로 새지 않는다
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

const HTML = readFileSync(new URL("../../static/accessistant.html", import.meta.url), "utf8");
const results = [];
function check(name, fn) {
  try { fn(); results.push(["PASS", name]); }
  catch (e) { results.push(["FAIL", name + " — " + e.message]); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const posted = [];        // {url, body}
let audioFailOnce = true;
let eventsFail = false;
const recorders = [];

const dom = new JSDOM(HTML, {
  runScripts: "dangerously", pretendToBeVisual: true, url: "https://example.test/static/accessistant.html",
  beforeParse(window) {
    window.fetch = async (url, opt) => {
      const u = String(url);
      if (u.includes("/api/v1/trial/status")) return { ok: true, json: async () => ({ enabled: true }) };
      if (u.includes("/api/v1/trial/events")) {
        if (eventsFail) return { ok: false, status: 502, json: async () => ({}) };
        posted.push({ url: u, body: JSON.parse(opt.body) });
        return { ok: true, json: async () => ({ ok: true }) };
      }
      if (u.includes("/api/v1/trial/audio")) {
        if (audioFailOnce) { audioFailOnce = false; throw new Error("network"); }
        posted.push({ url: u, body: opt.body });
        return { ok: true, json: async () => ({ ok: true }) };
      }
      if (u.includes("/api/v1/tts")) return { ok: true, blob: async () => ({}) };
      return { ok: true, json: async () => ({}) };
    };
    window.MediaRecorder = class {
      constructor(stream, opts) { this.stream = stream; this.opts = opts; this.state = "inactive"; this.mimeType = opts.mimeType || ""; recorders.push(this); }
      static isTypeSupported(m) { return m === "audio/webm;codecs=opus"; }
      start(ts) { this.timeslice = ts; this.state = "recording"; }
      stop() { this.state = "inactive"; if (this.ondataavailable) this.ondataavailable({ data: new window.Blob(["tail"], { type: "audio/webm" }) }); }
    };
    window.SpeechSynthesisUtterance = function (t) { this.text = t; };
    window.speechSynthesis = { cancel: () => {}, speak: (u) => { if (u.onend) u.onend(); } };
    window.navigator.geolocation = { watchPosition: () => 1, clearWatch: () => {} };
  },
});
const { window } = dom;
await sleep(50);
const T = window.__TRIAL;

// 1) 켜짐 · 기록 식별자
const q = await T.wsQuery();
check("실증 계정: 기록 켜짐 · 기록 식별자 형식 · 웹소켓 주소에 rid", () => {
  assert.equal(T.enabled, true);
  assert.match(T.rid, /^t\d{14}_[a-z0-9]{1,6}$/);
  assert.equal(q, "&rid=" + encodeURIComponent(T.rid));
});

// 2) 길안내 사건
window.URL.createObjectURL = () => "blob:x";
const played = [];
window.Audio = function () { const self = this; this.duration = 2.0; this.play = () => { played.push(self); return Promise.resolve(); }; this.pause = () => { self.paused = true; }; };
const NV = window.NAVI._internals();
NV.speak("앞으로 50m 이동합니다");
await sleep(20);
NV.stopSpeak();                                   // 재생 중 끊음 → navi_cut
NV.speak("첫째", { queue: true, kind: "step" });  // 대기열 상한(3) 넘기면 앞 문장부터 버림 → navi_drop
NV.speak("둘째", { queue: true, kind: "step" });
NV.speak("셋째", { queue: true, kind: "step" });
NV.speak("넷째", { queue: true, kind: "step" });
NV.speak("다섯째", { queue: true, kind: "step" });
await sleep(20);
NV.stopSpeak();
for (let i = 0; i < 40; i++) T.ev("test_fill", { i });   // 40건이 모이면 바로 올린다
await sleep(30);
const evs = posted.filter((p) => p.url.includes("/trial/events")).flatMap((p) => p.body.events);
check("길안내: 실제 재생한 문장(navi_play: 문장·서버 음성·보이스·길이)", () => {
  const e = evs.find((x) => x.type === "navi_play" && x.text === "앞으로 50m 이동합니다");
  assert.ok(e, "navi_play 없음: " + JSON.stringify(evs.map((x) => x.type)));
  assert.equal(e.via, "server");
  assert.equal(e.voice, "female");
  assert.equal(e.dur_ms, 2000);
  assert.equal(typeof e.tc, "number");
});
check("길안내: 재생 중 끊김(navi_cut)", () => {
  assert.ok(evs.some((x) => x.type === "navi_cut" && x.text === "앞으로 50m 이동합니다"));
});
check("길안내: 대기열 상한으로 버린 문장(navi_drop)", () => {
  assert.ok(evs.some((x) => x.type === "navi_drop" && x.text === "둘째"), JSON.stringify(evs.filter((x) => x.type === "navi_drop")));
});
check("사건 묶음에 rid 가 실린다", () => {
  const b = posted.find((p) => p.url.includes("/trial/events")).body;
  assert.equal(b.rid, T.rid);
  assert.ok(Array.isArray(b.events) && b.events.length >= 40);
});

// 3) 마이크 연속 녹음
const fakeStream = { id: "s1" };
T.attachMic(fakeStream);
const mr = recorders[recorders.length - 1];
check("마이크 녹음: 같은 스트림 · opus · 32kbps · 10초 조각", () => {
  assert.equal(mr.stream, fakeStream);
  assert.equal(mr.opts.mimeType, "audio/webm;codecs=opus");
  assert.equal(mr.opts.audioBitsPerSecond, 32000);
  assert.equal(mr.timeslice, 10000);
  assert.equal(mr.state, "recording");
});
mr.ondataavailable({ data: new window.Blob(["head"], { type: "audio/webm" }) });
await sleep(20);
check("첫 조각 업로드 실패 → 아직 안 올라감(단말 보관)", () => {
  assert.equal(posted.filter((p) => p.url.includes("/trial/audio")).length, 0);
});
await sleep(2200);                                  // 첫 재시도 2초
T.detachMic();                                      // stop → 마지막 조각
await sleep(50);
const aud = posted.filter((p) => p.url.includes("/trial/audio"));
check("재시도 후 조각이 순서대로 올라간다(rid·rec·seq·t0·tc)", () => {
  assert.equal(aud.length, 2, JSON.stringify(aud.map((a) => a.url)));
  const u0 = new window.URL(aud[0].url, "https://example.test");
  const u1 = new window.URL(aud[1].url, "https://example.test");
  assert.equal(u0.searchParams.get("rid"), T.rid);
  assert.equal(u0.searchParams.get("seq"), "0");
  assert.equal(u1.searchParams.get("seq"), "1");
  assert.equal(u0.searchParams.get("rec"), u1.searchParams.get("rec"));
  assert.ok(Number(u0.searchParams.get("t0")) > 0);
});
check("녹음 정지 후 상태 inactive", () => assert.equal(mr.state, "inactive"));

// 4) 전송 실패는 밖으로 새지 않고 다음에 다시 보낸다
eventsFail = true;
const before = posted.length;
let threw = null;
try { for (let i = 0; i < 45; i++) T.ev("fail_fill", { i }); } catch (e) { threw = e; }
await sleep(30);
check("사건 전송 실패: 예외 없음 · 아무것도 안 올라감", () => {
  assert.equal(threw, null);
  assert.equal(posted.length, before);
});
eventsFail = false;
for (let i = 0; i < 40; i++) T.ev("retry_fill", { i });
await sleep(60);
check("다음 전송 때 실패분까지 함께 올라간다", () => {
  const all = posted.slice(before).flatMap((p) => p.body.events || []);
  assert.ok(all.some((x) => x.type === "fail_fill"), "실패분 유실");
});
check("상담원 음성 재생 위치·중단·기기 음성 기록 지점(소스 레벨)", () => {
  assert.match(HTML, /trialEv\("ai_play", \{ off: trialOff \}\)/);
  assert.match(HTML, /trialEv\("ai_stop"/);
  assert.match(HTML, /trialEv\("consult_device_tts"/);
  assert.match(HTML, /window\.__TRIAL\.attachMic\(micStream\)/);
  assert.match(HTML, /window\.__TRIAL\.detachMic\(\)/);
});

let failed = 0;
for (const [st, name] of results) {
  console.log(`${st === "PASS" ? "  ok" : "FAIL"}  ${name}`);
  if (st === "FAIL") failed++;
}
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
