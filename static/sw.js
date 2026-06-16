const CACHE = 'cantonese-{{VERSION}}';
// Separate long-lived cache for user-uploaded media (UUID-named → immutable).
// Intentionally NOT versioned so photos survive app updates.
const MEDIA_CACHE = 'cantonese-media-v1';

const SHELL = [
  '/static/style.css',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/label-picker.js',
];

// API GET responses that are safe to serve stale (change rarely, useful offline).
const SWR_API = new Set([
  '/api/me',
  '/api/settings',
  '/api/streak',
  '/api/languages',
]);

// Minimal offline fallback page — returned when a navigation request fails and
// no cached copy exists. Embedded here so no extra network request is needed.
const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;display:flex;align-items:center;
  justify-content:center;min-height:100vh;background:#f8f9fa;color:#202124;padding:24px}
.card{background:#fff;border-radius:16px;padding:36px 28px;max-width:380px;width:100%;
  text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.08)}
h1{font-size:1.3rem;font-weight:700;margin-bottom:10px}
p{color:#5f6368;font-size:.92rem;line-height:1.5;margin-bottom:24px}
button{background:#1a73e8;color:#fff;border:none;border-radius:8px;padding:11px 24px;
  font-size:.95rem;font-weight:600;cursor:pointer}
</style></head>
<body><div class="card">
<h1>You're offline</h1>
<p>Check your connection and try again. Your flashcard reviews are saved locally and will sync when you reconnect.</p>
<button onclick="location.reload()">Try again</button>
</div></body></html>`;

// ── Install ───────────────────────────────────────────────────────────────────
// Use Promise.allSettled so a single fetch failure doesn't abort the entire
// install — which would leave the SW stuck in "installing" and cause
// navigator.serviceWorker.ready to never resolve.
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => Promise.allSettled(SHELL.map(url => c.add(url))))
      .then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────────────────────────────────────
// Delete old versioned caches. Keep MEDIA_CACHE — it's not versioned so user
// photos survive app updates.
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE && k !== MEDIA_CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Push notifications ────────────────────────────────────────────────────────
self.addEventListener('push', e => {
  const data = e.data ? e.data.json() : {};
  e.waitUntil(
    self.registration.showNotification(data.title || 'New notification', {
      body: data.body || '',
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/icon-192.png',
      data: { url: data.url || '/messages' },
      tag: data.tag || 'default',
      renotify: true,
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/messages';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin) && 'focus' in c) {
          c.navigate(url);
          return c.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
// Strategy by URL type:
//   /api/media/*     → cache-first (immutable UUID files; separate long-lived cache)
//   SWR_API routes   → stale-while-revalidate (serve cache, refresh in background)
//   /api/*           → network-only (app JS owns offline logic for data mutations)
//   /static/*, fonts → stale-while-revalidate (serve instantly, refresh behind scenes)
//   navigation       → network-first, fall back to cache, then inline offline page
self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  if (request.method !== 'GET') return;
  if (url.origin !== self.location.origin && !url.hostname.includes('fonts.g')) return;

  // ── Media images (user-uploaded photos) — cache-first, immutable ──────────
  if (url.pathname.startsWith('/api/media/')) {
    e.respondWith(
      caches.open(MEDIA_CACHE).then(mc =>
        mc.match(request).then(cached => {
          if (cached) return cached;
          return fetch(request).then(res => {
            if (res.ok) mc.put(request, res.clone());
            return res;
          });
        })
      )
    );
    return;
  }

  // ── Safe API responses — stale-while-revalidate ───────────────────────────
  if (SWR_API.has(url.pathname)) {
    e.respondWith(
      caches.open(CACHE).then(c =>
        c.match(request).then(cached => {
          const network = fetch(request).then(res => {
            if (res.ok) c.put(request, res.clone());
            return res;
          }).catch(() => cached || Response.error());
          // Serve cache immediately if available; kick off network update in parallel
          return cached ? (network.catch(() => {}), cached) : network;
        })
      )
    );
    return;
  }

  // ── Other API calls — network-only (app JS handles its own offline logic) ──
  if (url.pathname.startsWith('/api/')) return;

  // ── Static assets + fonts — stale-while-revalidate ───────────────────────
  if (url.pathname.startsWith('/static/') || url.hostname.includes('fonts.g')) {
    e.respondWith(
      caches.open(CACHE).then(c =>
        c.match(request).then(cached => {
          const network = fetch(request).then(res => {
            if (res.ok) c.put(request, res.clone());
            return res;
          });
          return cached || network;
        })
      )
    );
    return;
  }

  // ── Navigation — network-first, cache fallback, then offline page ─────────
  e.respondWith(
    fetch(request)
      .then(res => {
        // Only cache successful navigations (not login redirects, 404s, etc.)
        if (res.ok) caches.open(CACHE).then(c => c.put(request, res.clone()));
        return res;
      })
      .catch(async () => {
        const cached = await caches.match(request);
        if (cached) return cached;
        // Nothing in cache — return the embedded offline page
        return new Response(OFFLINE_HTML, {
          status: 503,
          headers: { 'Content-Type': 'text/html; charset=utf-8' },
        });
      })
  );
});
