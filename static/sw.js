const APP_VERSION = "20260806-1";
const APP_SHELL_CACHE = `l2f-shell-${APP_VERSION}`;
const RUNTIME_CACHE = `l2f-runtime-${APP_VERSION}`;
const CDN_CACHE = `l2f-cdn-${APP_VERSION}`;
const APP_SHELL_URLS = [
  "/",
  "/admin",
  "/manifest.json?v=20260716-2",
  "/APP-icon.png?v=20260716-2",
  "/icon-512.png?v=20260716-2",
  "/icon-192.png?v=20260716-2",
  "/apple-touch-icon.png?v=20260716-2",
];
const CDN_HOSTS = new Set([
  "cdnjs.cloudflare.com",
  "cdn.jsdelivr.net",
  "unpkg.com",
  "fonts.googleapis.com",
  "fonts.gstatic.com",
]);

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(APP_SHELL_CACHE);
    await Promise.allSettled(APP_SHELL_URLS.map(async (url) => {
      try {
        await cache.add(url);
      } catch (_err) {}
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const cacheNames = await caches.keys();
    await Promise.all(
      cacheNames
        .filter((name) => ![APP_SHELL_CACHE, RUNTIME_CACHE, CDN_CACHE].includes(name))
        .map((name) => caches.delete(name))
    );
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data === "APP_VERSION") {
    event.source?.postMessage({ type: "APP_VERSION", value: APP_VERSION });
  }
});

async function putIfCacheable(cache, request, response) {
  if (response && (response.ok || response.type === "opaque")) {
    await cache.put(request, response.clone());
  }
  return response;
}

async function staleWhileRevalidate(request, cacheName, fallbackUrl = "/") {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const networkPromise = fetch(request)
    .then((response) => putIfCacheable(cache, request, response))
    .catch(() => null);

  if (cached) {
    return cached;
  }

  const networkResponse = await networkPromise;
  if (networkResponse) {
    return networkResponse;
  }

  if (request.mode === "navigate") {
    return cache.match(fallbackUrl) || Response.error();
  }

  return Response.error();
}

async function networkFirst(request, cacheName, fallbackUrl = "/") {
  const cache = await caches.open(cacheName);

  try {
    const response = await fetch(request);
    return await putIfCacheable(cache, request, response);
  } catch (_err) {
    const cached = await cache.match(request);
    if (cached) {
      return cached;
    }
    if (request.mode === "navigate") {
      return cache.match(fallbackUrl) || Response.error();
    }
    return Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request, APP_SHELL_CACHE, "/"));
    return;
  }

  if (url.origin === self.location.origin) {
    if (url.pathname === "/socket.io/socket.io.js") {
      event.respondWith(staleWhileRevalidate(request, RUNTIME_CACHE));
      return;
    }
    if (url.pathname.startsWith("/socket.io/") || url.pathname.startsWith("/api/")) {
      return;
    }
    if (url.pathname === "/sw.js") {
      return;
    }
    event.respondWith(staleWhileRevalidate(request, RUNTIME_CACHE));
    return;
  }

  if (CDN_HOSTS.has(url.hostname)) {
    event.respondWith(staleWhileRevalidate(request, CDN_CACHE));
  }
});