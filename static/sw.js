const CACHE = 'cantonese-v1';
const SHELL = [
  '/',
  '/cards',
  '/static/style.css',
  '/static/manifest.json',
  'https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500&display=swap',
];

// Install: cache the app shell
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

// Activate: remove old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Fetch strategy:
//   - Navigation (HTML pages): network-first, fall back to cache
//   - Static assets (/static/): cache-first
//   - API calls: network-only (offline logic is handled in app JS)
self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  // Let non-GET and cross-origin (except Google Fonts) pass through
  if (request.method !== 'GET') return;
  if (url.origin !== self.location.origin && !url.hostname.includes('fonts.g')) return;

  if (url.pathname.startsWith('/api/')) {
    // Network-only for API — app JS handles offline fallback
    return;
  }

  if (url.pathname.startsWith('/static/') || url.hostname.includes('fonts.g')) {
    // Cache-first for assets
    e.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(request, clone));
        return res;
      }))
    );
    return;
  }

  // Navigation: network-first, fall back to cache
  e.respondWith(
    fetch(request).then(res => {
      const clone = res.clone();
      caches.open(CACHE).then(c => c.put(request, clone));
      return res;
    }).catch(() => caches.match(request))
  );
});
