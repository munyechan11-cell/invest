// Sift Quant AI — Service Worker (PWA install + 오프라인 페이지 캐시)
const CACHE = 'sift-v4';
const ASSETS = ['/', '/static/manifest.json', '/static/icon-192.svg', '/static/icon-512.svg', '/static/icon-maskable.svg', '/static/icon-glyph.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS).catch(()=>{})));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // 동적 API/WS는 항상 네트워크. 정적 파일만 캐시 fallback.
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;
  if (e.request.method !== 'GET') return;

  e.respondWith(
    fetch(e.request).then(res => {
      // 성공한 정적 자원은 캐시 갱신
      if (res.ok && ASSETS.some(a => url.pathname === a || url.pathname === '/')) {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone)).catch(()=>{});
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});
