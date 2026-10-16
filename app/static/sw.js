/* Restaurant Growth OS — service worker (basic PWA shell).
   Strategy:
   - Never cache API or auth responses (always fresh, per-tenant, sensitive).
   - Cache static assets (design system, icons) cache-first.
   - For navigations, try network first, fall back to a minimal offline notice. */

const CACHE = 'rgos-v33';
const STATIC_ASSETS = [
  '/static/design-system.css',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // Never cache API / auth / sync — these are dynamic and tenant-sensitive.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/webhooks')) return;

  // Static assets: cache-first.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      }))
    );
    return;
  }

  // Navigations: network-first, offline fallback.
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request).catch(() => new Response(
        '<!DOCTYPE html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<div style="font-family:-apple-system,system-ui,sans-serif;max-width:380px;margin:18vh auto;padding:0 24px;text-align:center;color:#5B6370">' +
        '<div style="width:40px;height:40px;border-radius:10px;background:#4F46E5;margin:0 auto 18px"></div>' +
        '<h1 style="font-size:20px;color:#0E1116;margin-bottom:8px">Brak połączenia</h1>' +
        '<p style="line-height:1.5">Growth OS potrzebuje internetu, żeby pokazać aktualne dane. Sprawdź połączenie i odśwież.</p>' +
        '</div>',
        { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      ))
    );
  }
});
