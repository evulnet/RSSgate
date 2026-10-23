/* RSSgate service worker - rendered by /sw.js with the CURRENT asset hash.
 * A byte change to this file is what makes iOS re-register, so the hash
 * lives in the cache name AND the precache URLs. Layers:
 *   /api/*  GET : network-first; the cache is the OFFLINE FALLBACK only
 *               (stale-while-revalidate here would break read-your-writes)
 *   /img/*      : cache-first - filenames are sha256 content hashes, so a
 *               hit is never stale, and offline reading keeps its photos
 *   everything else (shell, statics): stale-while-revalidate
 * Writes (POST) and cross-origin requests are NEVER intercepted - the
 * page's IndexedDB queue owns offline writes. */
const CACHE = "rssgate-{{ app_version }}";
const SHELL = [
  "/",
  "/static/style.css?v={{ app_version }}",
  "/static/viewer.js?v={{ app_version }}",
  "/static/manifest.webmanifest",
  "/static/favicon.svg",
  "/static/favicon-32.png",
  "/static/apple-touch-icon.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (ev) => {
  ev.waitUntil(
    caches.open(CACHE)
      // allSettled: one missing icon must not abort the whole install
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((k) => k.startsWith("rssgate-") && k !== CACHE)
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function cachePut(req, ev) {
  return (res) => {
    if (res && res.ok) {
      const p = res.clone().blob().then((b) =>
        caches.open(CACHE).then((c) =>
          c.put(req, new Response(b, { headers: res.headers }))));
      // extend the event so the write survives the SW going idle; an
      // unhandled rejection here is a silent cache miss later
      try { ev.waitUntil(p); } catch (e) { /* not extendable anymore */ }
    }
    return res;
  };
}

self.addEventListener("fetch", (ev) => {
  const req = ev.request;
  if (req.method !== "GET") return;               // writes belong to the page
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // never touch the world

  if (url.pathname.startsWith("/api/")) {
    ev.respondWith(
      fetch(req).then(cachePut(req, ev)).catch(() =>
        caches.match(req).then((hit) => hit ||
          new Response('{"offline":true}', {
            status: 503, headers: { "Content-Type": "application/json" } })))
    );
    return;
  }

  if (url.pathname.startsWith("/img/")) {
    ev.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then(cachePut(req, ev)))
    );
    return;
  }

  // shell + statics: stale-while-revalidate
  ev.respondWith(
    caches.match(req).then((hit) => {
      const net = fetch(req).then(cachePut(req, ev)).catch(() => hit);
      return hit || net;
    })
  );
});
