const CACHE_NAME = "cenro-shell-v3";
const OFFLINE_URL = "/offline/";
const PRECACHE_URLS = [
  "/",
  "/dashboard/",
  "/offline/",
  "/static/css/style.css",
  "/static/css/consumer-responsive.css",
  "/static/css/admin-layout.css",
  "/static/img/cenro_logo.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) return caches.delete(key);
          return Promise.resolve();
        })
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  // JSON APIs (notifications, etc.): never cache — stale responses hide new alerts.
  // User uploads (signatures, photos): never cache-first — wrong/stale cache breaks images.
  try {
    const path = new URL(req.url).pathname;
    if (path.includes("/api/") || path.startsWith("/media/")) {
      event.respondWith(fetch(req));
      return;
    }
  } catch (e) {
    /* fall through */
  }

  // HTML navigation: network-first, fallback to cache/offline page.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return resp;
        })
        .catch(async () => {
          const cached = await caches.match(req);
          return cached || caches.match(OFFLINE_URL);
        })
    );
    return;
  }

  // Static assets: cache-first.
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((resp) => {
        if (resp && resp.status === 200 && req.url.startsWith(self.location.origin)) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
        }
        return resp;
      });
    })
  );
});
