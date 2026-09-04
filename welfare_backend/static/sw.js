/* AccessistantAI 서비스워커 — 홈 화면 설치·오프라인 최소 지원.
   API·WebSocket 응답은 캐시하지 않는다(정책·경로 정보는 항상 최신이어야 함). */
const CACHE = "accessistant-v2";
const SHELL = ["/static/accessistant.html", "/static/manifest.webmanifest", "/static/logo.svg",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-192.png", "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon-180.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;
  if (url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
