// PSPLA Checker — Service Worker (PWA)
// Network-first strategy: always try live data, fall back to cache when offline.

const CACHE_NAME = 'pspla-v1';
const PRECACHE_URLS = [
  '/',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css'
];

// Install: pre-cache shell assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// Activate: clean up old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch: network-first, fall back to cache
self.addEventListener('fetch', event => {
  // Skip non-GET requests (form submissions, API calls)
  if (event.request.method !== 'GET') return;

  // Skip non-http(s) schemes (chrome-extension://, etc.)
  if (!event.request.url.startsWith('http')) return;

  // Skip Supabase API calls — never cache these
  if (event.request.url.includes('supabase.co')) return;

  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Cache successful responses for offline fallback
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
