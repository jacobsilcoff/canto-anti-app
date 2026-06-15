const CACHE = 'cantonese-{{VERSION}}';
const SHELL = [
  '/static/style.css',
  '/static/manifest.json',
];

// Install: cache the app shell.
// Use individual catch() so a single fetch failure doesn't abort install
// and block skipWaiting() — which would leave the SW stuck in "installing"
// and cause navigator.serviceWorker.ready to never resolve.
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => Promise.allSettled(SHELL.map(url => c.add(url))))
      .then(() => self.skipWaiting())
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

// Push notifications
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
    // Stale-while-revalidate: serve cache instantly, refresh in the background
    // so an updated asset is picked up on the next load even within a version.
    e.respondWith(
      caches.match(request).then(cached => {
        const network = fetch(request).then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(request, clone));
          return res;
        });
        return cached || network;
      })
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
