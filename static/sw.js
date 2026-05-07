const CACHE = 'solipool-v1';
const OFFLINE_URLS = ['/', '/login', '/static/manifest.json'];

self.addEventListener('install', evt => {
  evt.waitUntil(
    caches.open(CACHE).then(c => c.addAll(OFFLINE_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', evt => {
  evt.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', evt => {
  if (evt.request.method !== 'GET') return;
  const url = new URL(evt.request.url);
  if (url.pathname.startsWith('/ussd')) return;
  evt.respondWith(
    fetch(evt.request)
      .then(resp => {
        if (resp && resp.status === 200 && resp.type === 'basic') {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(evt.request, clone));
        }
        return resp;
      })
      .catch(() => caches.match(evt.request).then(r => r || caches.match('/')))
  );
});
