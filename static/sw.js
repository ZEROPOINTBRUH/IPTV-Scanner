// IPTV Scanner service worker — caches the app shell ONLY.
// Never touched (always network): /proxy/*, /channels, /status, /api/*, /icons/*.
// Shell JS/CSS are network-first so deploys show up; "/" and the manifest are
// stale-while-revalidate for fast, offline-tolerant starts.

const SHELL_CACHE = 'iptv-shell-v9';
const SHELL_URLS = [
    '/',
    '/static/js/scripts.js',
    '/static/css/styles.css',
    '/static/favicon.svg',
    '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches
            .open(SHELL_CACHE)
            .then((cache) =>
                Promise.all(
                    SHELL_URLS.map((u) =>
                        cache.add(new Request(u, { cache: 'reload' })).catch(() => null)
                    )
                )
            )
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches
            .keys()
            .then((keys) =>
                Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)))
            )
            .then(() => self.clients.claim())
    );
});

function isBypass(url) {
    const p = url.pathname;
    if (p.startsWith('/proxy/')) return true;
    if (p === '/channels') return true;
    if (p === '/status') return true;
    if (p.startsWith('/api/')) return true;
    if (p.startsWith('/icons/')) return true;
    if (p === '/sw.js') return true;
    return false;
}

function isVolatileShell(url) {
    return (
        url.pathname === '/static/js/scripts.js' ||
        url.pathname === '/static/css/styles.css'
    );
}

function isShellRequest(url) {
    const p = url.pathname;
    return (
        p === '/' ||
        p === '/index.html' ||
        p === '/manifest.webmanifest' ||
        p === '/static/favicon.svg' ||
        isVolatileShell(url)
    );
}

function putInCache(req, res) {
    if (res && res.ok) {
        const clone = res.clone();
        caches
            .open(SHELL_CACHE)
            .then((c) => c.put(req, clone))
            .catch(() => {});
    }
    return res;
}

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;

    let url;
    try {
        url = new URL(req.url);
    } catch {
        return;
    }

    if (url.origin === self.location.origin) {
        if (isBypass(url)) return;

        // Network-first: scripts + styles (versioned via ?v= but cached by path).
        if (isVolatileShell(url)) {
            event.respondWith(
                fetch(req)
                    .then((res) => putInCache(req, res))
                    .catch(() => caches.match(req, { ignoreSearch: true }))
            );
            return;
        }

        // Stale-while-revalidate: page, manifest, favicon.
        if (isShellRequest(url)) {
            event.respondWith(
                caches.match(req, { ignoreSearch: true }).then((cached) => {
                    const fetcher = fetch(req)
                        .then((res) => putInCache(req, res))
                        .catch(() => cached);
                    return cached || fetcher;
                })
            );
        }
        return;
    }

    // Third-party fonts / Tailwind CDN: cache-first.
    if (
        url.hostname === 'fonts.googleapis.com' ||
        url.hostname === 'fonts.gstatic.com' ||
        url.hostname === 'cdn.tailwindcss.com'
    ) {
        event.respondWith(
            caches.match(req).then(
                (cached) =>
                    cached ||
                    fetch(req)
                        .then((res) => putInCache(req, res))
                        .catch(() => cached)
            )
        );
    }
});
