"use strict";
// App shell: cache-first. Read-only API calls: network-first, falling back to the
// last good copy (marked with x-offline-cache) so the app still opens offline.
// Writes (POST) are never cached or queued — an entry is only "posted" when Tally confirms it.
const SHELL = "books-shell-v1";
const DATA = "books-data-v1";
const ASSETS = ["./", "index.html", "styles.css", "app.js", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => ![SHELL, DATA].includes(k)).map((k) => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    e.respondWith((async () => {
      try {
        const res = await fetch(req);
        if (res.ok) {
          const copy = new Response(await res.clone().blob(), {
            status: 200, headers: { "content-type": "application/json", "x-offline-cache": new Date().toISOString() },
          });
          (await caches.open(DATA)).put(req, copy);
        }
        return res;
      } catch (err) {
        const hit = await caches.match(req, { cacheName: DATA });
        if (hit) return hit;
        return new Response(JSON.stringify({ error: "You're offline and this view hasn't been saved yet." }),
          { status: 503, headers: { "content-type": "application/json" } });
      }
    })());
    return;
  }

  e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
});
