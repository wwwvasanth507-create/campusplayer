// Campus Player - High-Performance PWA & Background Upload Service Worker
const CACHE_NAME = 'campusplayer-cache-v2';
const STATIC_ASSETS = [
  '/',
  '/static/css/style.css',
  '/static/js/main.js',
  '/static/manifest.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.warn('[PWA] Static asset cache warning:', err);
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Only cache GET requests, bypass for upload endpoints, API, video streams (.m3u8, .ts), socket.io, and auth routes
  const url = new URL(event.request.url);
  if (
    event.request.method !== 'GET' ||
    url.pathname.includes('/teacher/upload_chunk') ||
    url.pathname.includes('/socket.io/') ||
    url.pathname.includes('/hls/') ||
    url.pathname.includes('/api/') ||
    url.pathname.includes('/auth/') ||
    url.pathname.includes('/login') ||
    url.pathname.includes('/logout')
  ) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache);
          });
        }
        return networkResponse;
      })
      .catch(() => {
        return caches.match(event.request).then((cachedResponse) => {
          if (cachedResponse) {
            return cachedResponse;
          }
          if (event.request.headers.get('accept') && event.request.headers.get('accept').includes('text/html')) {
            return caches.match('/');
          }
        });
      })
  );
});

// ═══════════════════════════════════════════════════════════════
//  BACKGROUND FETCH API & DESKTOP NOTIFICATIONS FOR 20GB+ UPLOADS
// ═══════════════════════════════════════════════════════════════

self.addEventListener('backgroundfetchsuccess', (event) => {
  const bgFetch = event.registration;
  console.log(`[SW] Background Fetch Succeeded: ${bgFetch.id}`);

  event.waitUntil((async () => {
    // Notify all open window clients
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clients) {
      client.postMessage({
        type: 'BACKGROUND_FETCH_SUCCESS',
        id: bgFetch.id,
        title: bgFetch.title
      });
    }

    // Show OS system tray desktop notification if permission granted
    if (self.Notification && self.Notification.permission === 'granted') {
      self.registration.showNotification('Campus Player — Upload Complete', {
        body: `Video "${bgFetch.title || 'Video Upload'}" uploaded successfully in background!`,
        icon: '/static/img/icon-192.png',
        badge: '/static/img/icon-192.png',
        tag: `upload-${bgFetch.id}`
      });
    }
  })());
});

self.addEventListener('backgroundfetchfail', (event) => {
  const bgFetch = event.registration;
  console.warn(`[SW] Background Fetch Failed: ${bgFetch.id}`);

  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clients) {
      client.postMessage({
        type: 'BACKGROUND_FETCH_FAIL',
        id: bgFetch.id
      });
    }
  })());
});

self.addEventListener('backgroundfetchclick', (event) => {
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window' });
    if (clients.length > 0) {
      clients[0].focus();
    } else {
      self.clients.openWindow('/teacher/videos');
    }
  })());
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'PING_BACKGROUND_SW') {
    event.ports[0].postMessage({ status: 'ACTIVE' });
  }
});
