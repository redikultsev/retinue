// Retinue's service worker: the app shell only. /api/* is never touched — not cached, not even intercepted — so
// every snapshot, event and decision goes to the server as if there were no worker.
//
// - a navigation to an app route (/, /login, the tabs, /s/…, /a/…, /f/…, /b/…, /p/…): the network first (a new deploy is seen at once), and
//   only an HTML answer is kept as the shell; offline, the cached shell. Any other navigation (the manifest, an
//   icon opened directly) goes to the network untouched;
// - /assets/* (content-hashed by Vite): the cache first, filled on first use, trimmed to the newest 80;
// - the rest of the shell (manifest, icons): the cache, refreshed in the background.
const SHELL = "retinue-shell-v1";
const ASSETS = "retinue-assets-v1";
const PRECACHE = ["/", "/manifest.webmanifest", "/icons/icon.svg", "/icons/icon-192.png", "/icons/apple-touch-icon.png"];
const KEEP_ASSETS = 80;
const APP_ROUTE =
  /^\/(?:$|login$|today$|sessions$|new$|pages$|read$|week$|spaces$|spaces\/[^/]+$|spaces\/[^/]+\/r\/.+$|[safbp]\/[^/]+$)/;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== ASSETS).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function trim() {
  const cache = await caches.open(ASSETS);
  const keys = await cache.keys();
  await Promise.all(keys.slice(0, Math.max(0, keys.length - KEEP_ASSETS)).map((k) => cache.delete(k)));
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/api") return;

  if (req.mode === "navigate") {
    if (!APP_ROUTE.test(url.pathname)) return;
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok && (res.headers.get("content-type") || "").startsWith("text/html")) {
            const copy = res.clone();
            caches.open(SHELL).then((c) => c.put("/", copy));
          }
          return res;
        })
        .catch(() => caches.match("/").then((r) => r || Response.error())),
    );
    return;
  }

  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            if (res.ok) {
              const copy = res.clone();
              caches.open(ASSETS).then((c) => c.put(req, copy)).then(trim);
            }
            return res;
          }),
      ),
    );
    return;
  }

  if (PRECACHE.includes(url.pathname)) {
    event.respondWith(
      caches.open(SHELL).then((cache) =>
        cache.match(req).then((hit) => {
          const fresh = fetch(req)
            .then((res) => {
              if (res.ok) cache.put(req, res.clone());
              return res;
            })
            .catch(() => hit);
          return hit || fresh;
        }),
      ),
    );
  }
});
