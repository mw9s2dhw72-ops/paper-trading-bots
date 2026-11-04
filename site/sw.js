// Offline fallback: network-first; if the tunnel/box is unreachable (error, timeout or non-200 such as
// "No tunnel found"), serve the last good copy so the app still opens with the latest known numbers.
const C = 'paperbots-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
function timeout(ms){ return new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms)); }
self.addEventListener('fetch', e => {
  const req = e.request; if (req.method !== 'GET') return;
  const key = new Request(req.url.split('?')[0]);
  e.respondWith((async () => {
    const cache = await caches.open(C);
    try {
      const r = await Promise.race([fetch(req, {cache: 'no-store'}), timeout(9000)]);
      if (r.ok) { cache.put(key, r.clone()); return r; }
      const hit = await cache.match(key); return hit || r;
    } catch (err) {
      const hit = await cache.match(key); if (hit) return hit; throw err;
    }
  })());
});
