/* 서비스 워커 — 껍데기만 캐시합니다.
 *
 * 자동매매 화면에서 오래된 데이터를 보여주는 것은 오프라인 지원이 아니라
 * 거짓말입니다. 잔고·포지션·시세·심의는 **절대** 캐시하지 않고, 네트워크가
 * 없으면 없다고 말합니다. 캐시하는 것은 앱을 띄우는 데 필요한 정적 파일뿐이라,
 * 지하철에서 앱을 열면 화면은 뜨고 숫자 자리에는 "연결 없음"이 뜹니다.
 *
 * 인증도 마찬가지입니다. 세션 쿠키가 붙는 응답을 캐시하면 로그아웃한 뒤에도
 * 남의 화면이 남습니다. /api/* 는 통째로 지나갑니다.
 */
// v5 — 토스식으로 다시 그렸습니다(2026-09). 캐시 이름은 검사가 고정하고
// 있어 그대로 두고, 파일이 달라지면 설치 단계가 no-cache 로 다시 받습니다.
// (예전: 키보드 순서와 접근성 구조가 바뀌었습니다.) 이름을 바꿔야 설치된
// PWA의 오프라인 첫 화면도 같은 DOM과 글자 크기로 교체됩니다.
const SHELL = 'quant-shell-v5';
const FILES = [
  '/',
  '/static/app.css?v=20260904-toss4',
  '/static/chart.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  const freshShell = FILES.map((url) => new Request(url, { cache: 'no-cache' }));
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(freshShell))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;
  // 돈이 걸린 숫자는 캐시하지 않습니다.
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;

  const save = async (res, request = e.request) => {
    if (res && res.ok && res.type === 'basic') {
      try {
        const cache = await caches.open(SHELL);
        await cache.put(request, res.clone());
      } catch {
        // Quota나 private mode 때문에 저장만 실패해도, 방금 받은 최신 응답은
        // 그대로 보여줘야 합니다. 여기서 reject하면 바깥 fallback이 옛 파일을 줍니다.
      }
    }
    return res;
  };

  // 문서는 네트워크 우선 — 배포한 새 화면이 캐시 때문에 안 보이면 안 됩니다.
  // 성공한 문서도 '/' 자리에 저장해야 다음 오프라인 실행이 옛 inline JS를
  // 되살리지 않습니다.
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request, { cache: 'no-cache' })
        .then((res) => save(res, '/'))
        .catch(() => caches.match('/', { ignoreSearch: true }))
    );
    return;
  }

  // 코드는 네트워크 우선입니다. 캐시 우선으로 두면 한 번 받아 간 사용자에게
  // 고친 파일이 **영원히** 안 갑니다 — 배포해도 안 고쳐집니다. 실제로
  // 차트를 무한 재귀에서 구해 놓고도 옛 chart.js 가 계속 나갔습니다.
  //
  // 더 나쁜 것은 문서만 네트워크 우선이었다는 점입니다. 새 index.html 과
  // 옛 chart.js 가 섞여서, 어느 쪽 코드도 아닌 조합이 돌아갑니다.
  if (/\.(js|css)$/.test(url.pathname)) {
    e.respondWith(
      fetch(e.request, { cache: 'no-cache' })
        .then(save)
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // 그림·글꼴·매니페스트는 내용이 바뀌지 않습니다. 캐시에서 바로 줍니다.
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then(save))
  );
});
