// dashboard/static/sw.js
// Cache-first app shell, so opening the PWA with the daemon unreachable —
// Mac asleep, or a passing-period wake window that hasn't landed yet —
// renders the last-known page instantly instead of a blank tab. Before this,
// boot() awaited /api/config with no timeout: a hung fetch to a sleeping Mac
// left nothing rendered and nothing cached to fall back to.
//
// Scope is "/" — server.py serves this file at /sw.js, not /static/sw.js, so
// the default scope covers the whole origin. A script registered from
// /static/ would only ever see requests under /static/*, which is not enough
// to intercept navigation to "/" or the /api/schedule and /api/after-school
// reads this shell depends on.
//
// CACHED, cache-first with background revalidation:
//   - the app shell: "/", app.js, style.css, the manifest, the icons
//   - /api/schedule and /api/after-school — what the period card and the
//     after-school card need to render without a round trip
//
// NEVER CACHED, always network — deliberately untouched by this file:
//   - every POST (chat, quick-add, config saves, pending-approval taps) —
//     a write must never appear to succeed from a cache
//   - /api/status, /api/today, /api/pending-approvals, /api/config, and
//     everything else not named above — live state, not a shell
//   - /api/stream — an SSE connection; caching it would break it outright

// Bumped whenever the shell's own files change shape together. `activate`
// deletes every cache that is not this name, so a browser holding the old
// index.html cannot serve it alongside the new style.css — which is exactly
// how a half-styled page happens: the markup and the stylesheet are cached
// independently and revalidated independently, so without a version bump a
// tab can hold one of each from either side of the change for as long as it
// takes both background fetches to land.
const CACHE_VERSION = 'friday-shell-v10';
const NETWORK_TIMEOUT_MS = 2500;

const SHELL_URLS = [
  '/static/app.js',
  '/static/style.css',
  '/static/manifest.webmanifest',
  // The hub mark. Part of the shell rather than an ordinary image: an
  // offline homepage that renders everything except the one lit object in
  // the middle of it looks broken, not degraded.
  '/static/jarvis.png',
  '/static/favicon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon-maskable-192.png',
  '/static/icon-maskable-512.png',
];

const DATA_URLS = ['/api/schedule', '/api/after-school'];

// Only reachable if this is the very first offline visit ever — the shell
// itself failed to precache because the tab was never open while the Mac was
// reachable. Every later offline visit has a real cached "/" to serve
// instead. Still better than the browser's own dinosaur.
const OFFLINE_SHELL_FALLBACK = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>F.R.I.D.A.Y.</title></head>
<body style="background:#060E1C;color:#E9F4FF;font-family:sans-serif;padding:2rem;">
<h2>F.R.I.D.A.Y.</h2>
<p>Offline, and nothing is cached yet. Open this page once while the Mac is
reachable — after that it works offline too.</p>
</body></html>`;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_VERSION);
    // One at a time, best-effort: a single missing icon must not fail the
    // whole precache and leave the shell itself uncached.
    await Promise.all(['/', ...SHELL_URLS].map(async (url) => {
      try {
        const resp = await fetch(url, { credentials: 'same-origin' });
        if (resp.ok) await cache.put(url, await stampCachedAt(resp));
      } catch { /* offline install, or a fresh boot — filled in later */ }
    }));
  })());
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n !== CACHE_VERSION).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

// Tags a cached response with when it was cached, so the page can show
// "updated 12:04 PM" instead of presenting stale data as if it were live.
async function stampCachedAt(resp) {
  const body = await resp.arrayBuffer();
  const headers = new Headers(resp.headers);
  headers.set('x-friday-cached-at', new Date().toISOString());
  return new Response(body, { status: resp.status, statusText: resp.statusText, headers });
}

function fetchWithTimeout(request, ms) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  return fetch(request, { signal: ctrl.signal }).finally(() => clearTimeout(t));
}

// Cache-first, background revalidate. The cached response (if any) answers
// immediately — this is the whole point, over network-first-with-fallback,
// which still pays the timeout on every request. A network fetch races a
// short timeout in the background and refreshes the cache for next time; it
// is never awaited by the response that goes back to the page.
async function cacheFirst(event, request, cacheKey) {
  const key = cacheKey || request;
  const cache = await caches.open(CACHE_VERSION);
  const cached = await cache.match(key);

  const revalidate = fetchWithTimeout(request, NETWORK_TIMEOUT_MS)
    .then(async (fresh) => {
      if (fresh && fresh.ok) await cache.put(key, await stampCachedAt(fresh.clone()));
      return fresh;
    })
    .catch(() => null);

  if (cached) {
    event.waitUntil(revalidate);
    return cached;
  }

  const fresh = await revalidate;
  if (fresh && fresh.ok) return fresh;

  if (request.mode === 'navigate') {
    return new Response(OFFLINE_SHELL_FALLBACK, { status: 200, headers: { 'Content-Type': 'text/html' } });
  }
  return new Response(JSON.stringify({ offline: true }), {
    status: 503, headers: { 'Content-Type': 'application/json' },
  });
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;              // never intercept writes
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;    // fonts, etc. — pass through

  if (request.mode === 'navigate' && (url.pathname === '/' || url.pathname === '/index.html')) {
    event.respondWith(cacheFirst(event, request, '/'));
    return;
  }
  if (SHELL_URLS.includes(url.pathname) || DATA_URLS.includes(url.pathname)) {
    event.respondWith(cacheFirst(event, request));
    return;
  }
  // Everything else falls through untouched — no respondWith() at all — so
  // /api/status, /api/today, /api/chat, /api/stream and every write behave
  // exactly as they would with no service worker installed.
});
