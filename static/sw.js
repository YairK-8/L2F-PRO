const APP_VERSION = "20260716-2";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("message", (event) => {
  if (event.data === "APP_VERSION") {
    event.source?.postMessage({ type: "APP_VERSION", value: APP_VERSION });
  }
});
