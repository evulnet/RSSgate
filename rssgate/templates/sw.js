/* RSSgate service worker - rendered by /sw.js with the CURRENT asset hash.
 * A byte change to this file is what makes iOS re-register, so the hash
 * lives in the cache name AND the precache URLs. Layers:
 *   /api/*  GET : network-first; the cache is the OFFLINE FALLBACK only
 *               (stale-while-revalidate here would break read-your-writes).
 *               Every fallback answer (cached 200 or the 503 stub) carries
 *               X-RSSgate-Offline: 1 - the page's only offline signal used
 *               to be a fetch TypeError, which this layer swallows, so a
 *               cached /api/pulse 200 kept netOk=true while the server was
 *               down and read beacons were lost.
 *   /image/*    : cache-first in its OWN cache (IMG, not versioned: a deploy
 *               must not wipe offline photos) - filenames hash the source
 *               URL, so a hit is never stale. Capped at IMG_MAX entries.
 *   everything else (shell, statics): stale-while-revalidate
 * Writes (POST) and cross-origin requests are NEVER intercepted - the
 * page's IndexedDB queue owns offline writes. */
const CACHE = "rssgate-{{ app_version }}";
const IMG = "rssgate-img";          // survives deploys (activate keeps it)
const IMG_MAX = 1500;
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
        .filter((k) => k.startsWith("rssgate-") && k !== CACHE && k !== IMG)
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function trimImages() {
  return caches.open(IMG).then((c) => c.keys().then((keys) =>
    Promise.all(keys.slice(0, Math.max(0, keys.length - IMG_MAX))
      .map((k) => c.delete(k)))));
}

function offline(res) {             // mark a fallback so the page can tell
  const h = new Headers(res.headers);
  h.set("X-RSSgate-Offline", "1");
  return new Response(res.body, { status: res.status, headers: h });
}

function cachePut(req, ev, name) {
  return (res) => {
    if (res && res.ok) {
      const p = res.clone().blob().then((b) =>
        caches.open(name || CACHE).then((c) =>
          c.put(req, new Response(b, { headers: res.headers }))))
        .then(() => (name === IMG ? trimImages() : null));
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
        caches.match(req, { cacheName: CACHE }).then((hit) => offline(hit ||
          new Response('{"offline":true}', {
            status: 503, headers: { "Content-Type": "application/json" } }))))
    );
    return;
  }

  if (url.pathname.startsWith("/image/")) {
    ev.respondWith(
      caches.match(req, { cacheName: IMG }).then((hit) =>
        hit || fetch(req).then(cachePut(req, ev, IMG)))
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
