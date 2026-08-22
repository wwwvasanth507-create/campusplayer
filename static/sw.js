// Campus Player - High-Performance PWA & Background Upload Service Worker
const CACHE_NAME = 'campusplayer-cache-v3';
const STATIC_ASSETS = [
  '/',
  '/static/css/style.css',
  '/static/js/main.js',
  '/static/manifest.json'
];

// In-memory map of active upload sessions registered by the page
// uuid -> { uuid, title, totalChunks }
const activeUploads = new Map();

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
//  MESSAGE HANDLER — Register active upload sessions from the page
// ═══════════════════════════════════════════════════════════════

self.addEventListener('message', (event) => {
  if (!event.data) return;

  if (event.data.type === 'PING_BACKGROUND_SW') {
    if (event.ports && event.ports[0]) {
      event.ports[0].postMessage({ status: 'ACTIVE' });
    }
    return;
  }

  if (event.data.type === 'REGISTER_ACTIVE_UPLOAD') {
    // Track the active upload so we can notify clients on background fetch completion
    const { uuid, title, totalChunks } = event.data;
    if (uuid) {
      activeUploads.set(uuid, { uuid, title: title || 'Video', totalChunks });
      console.log(`[SW] Registered active upload: ${uuid} — ${title}`);
    }
    return;
  }

  if (event.data.type === 'UPLOAD_COMPLETE') {
    // Page signals that an upload completed normally — remove from tracking
    const { uuid } = event.data;
    if (uuid) activeUploads.delete(uuid);
    return;
  }
});

// ═══════════════════════════════════════════════════════════════
//  BACKGROUND FETCH API — Handles uploads registered with
//  registration.backgroundFetch.fetch(...)
//  Supported in Chromium-based browsers only (progressive enhancement).
//  Firefox and Safari fall back to normal XHR-based chunked upload.
// ═══════════════════════════════════════════════════════════════

self.addEventListener('backgroundfetchsuccess', (event) => {
  const bgFetch = event.registration;
  console.log(`[SW] Background Fetch Succeeded: ${bgFetch.id}`);

  event.waitUntil((async () => {
    // Notify all open window clients so they can refresh the video list
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clients) {
      client.postMessage({
        type: 'BACKGROUND_FETCH_SUCCESS',
        id: bgFetch.id,
        title: bgFetch.title
      });
    }

    // Remove from activeUploads tracking
    const uuid = bgFetch.id.replace(/^upload-/, '');
    activeUploads.delete(uuid);

    // Show OS system tray desktop notification if permission granted
    if (self.Notification && self.Notification.permission === 'granted') {
      await self.registration.showNotification('Campus Player — Upload Complete', {
        body: `"${bgFetch.title || 'Video Upload'}" uploaded successfully in background!`,
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

    if (self.Notification && self.Notification.permission === 'granted') {
      await self.registration.showNotification('Campus Player — Upload Failed', {
        body: `Upload "${bgFetch.title || 'Video'}" failed. Please retry.`,
        icon: '/static/img/icon-192.png',
        tag: `upload-fail-${bgFetch.id}`
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
