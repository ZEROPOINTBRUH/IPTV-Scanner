// IPTV Scanner — LiveTVGuide
// Roku-style guide: left/bottom nav, horizontal rails, sticky custom player.
// Playback goes through /proxy/stream with HLS.js; client signals feed the
// server-side health queue (report-alive / report-playback / watching).

(() => {
    'use strict';

    // ------------------------------------------------------------------ config
    const LS_FAVORITES = 'iptv_scanner_favorites_v2';
    const LS_FAVORITES_V1 = 'iptv_scanner_favorites_v1';
    const LS_RECENT = 'iptv_scanner_recent_v1';
    const LS_SESSION = 'iptv_session_id';
    const SS_INSECURE_OK = 'iptv_insecure_ok';

    const RAIL_PAGE = 24;
    const GRID_PAGE = 48;
    const HOME_CATEGORY_RAILS = 6;
    const MAX_RECENT = 20;

    const SEARCH_DEBOUNCE_MS = 320;
    const NO_PLAYBACK_MS = 12000;
    const SLOW_LOAD_MS = 8000;
    const HEARTBEAT_MS = 20000;
    const CHROME_HIDE_MS = 3200;
    const MAX_CONCURRENT_FETCHES = 1;
    const SOFT_REFRESH_MIN_MS = 45000;
    const SSE_BACKOFF_START_MS = 1000;
    const SSE_BACKOFF_MAX_MS = 60000;
    const POLL_FALLBACK_MS = 30000;

    // ----------------------------------------------------------------- helpers
    const $ = (id) => document.getElementById(id);

    function escapeHtml(text) {
        if (text == null) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function h(html) {
        const t = document.createElement('template');
        t.innerHTML = html.trim();
        return t.content.firstElementChild;
    }

    function isYouTubeUrl(url) {
        const u = String(url || '').toLowerCase();
        return u.includes('youtube.com/') || u.includes('youtu.be/');
    }

    function isTwitchUrl(url) {
        return String(url || '').toLowerCase().includes('twitch.tv/');
    }

    function youtubeEmbedSrc(url) {
        const raw = String(url || '').trim();
        let id = null;
        const watch = raw.match(/[?&]v=([a-zA-Z0-9_-]{11})/);
        const short = raw.match(/youtu\.be\/([a-zA-Z0-9_-]{11})/);
        const embed = raw.match(/youtube\.com\/embed\/([a-zA-Z0-9_-]{11})/);
        if (watch) id = watch[1];
        else if (short) id = short[1];
        else if (embed) id = embed[1];
        if (id) {
            return `https://www.youtube.com/embed/${id}?autoplay=1&mute=1&playsinline=1&rel=0`;
        }
        // Channel /live pages — best-effort live_stream embed
        const handle = raw.match(/youtube\.com\/@([^/?#]+)/);
        const channel = raw.match(/youtube\.com\/channel\/([^/?#]+)/);
        const user = raw.match(/youtube\.com\/(?:c|user)\/([^/?#]+)/);
        const ch = (handle && handle[1]) || (channel && channel[1]) || (user && user[1]);
        if (ch) {
            return `https://www.youtube.com/embed/live_stream?channel=${encodeURIComponent(ch)}&autoplay=1&mute=1`;
        }
        return null;
    }

    function twitchEmbedSrc(url) {
        const raw = String(url || '').trim();
        const m = raw.match(/twitch\.tv\/([a-zA-Z0-9_]+)/i);
        if (!m) return null;
        const name = m[1];
        if (!name || /^(videos|directory|downloads|p|settings)$/i.test(name)) return null;
        const parents = new Set([
            location.hostname || 'localhost',
            'localhost',
            '127.0.0.1',
        ]);
        if (location.hostname && location.hostname.startsWith('www.')) {
            parents.add(location.hostname.slice(4));
        }
        const q = [...parents].map((p) => `parent=${encodeURIComponent(p)}`).join('&');
        // VODs: twitch.tv/videos/123
        if (/\/videos\/\d+/i.test(raw)) {
            const vid = raw.match(/\/videos\/(\d+)/i);
            if (vid) return `https://player.twitch.tv/?video=${vid[1]}&${q}&autoplay=true`;
        }
        return `https://player.twitch.tv/?channel=${encodeURIComponent(name)}&${q}&autoplay=true`;
    }

    function platformEmbedSrc(url) {
        if (isYouTubeUrl(url)) return youtubeEmbedSrc(url);
        if (isTwitchUrl(url)) return twitchEmbedSrc(url);
        return null;
    }

    function debounce(fn, ms) {
        let t;
        return (...args) => {
            clearTimeout(t);
            t = setTimeout(() => fn.apply(null, args), ms);
        };
    }

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    function countryFlagEmoji(code) {
        if (!code) return '🌐';
        let c = String(code).trim().toUpperCase();
        if (c === 'UK') c = 'GB';
        if (c === 'EL') c = 'GR';
        if (!/^[A-Z]{2}$/.test(c) || c === 'XX' || c === 'ZZ') return '🌐';
        const A = 0x1f1e6;
        return String.fromCodePoint(A + (c.charCodeAt(0) - 65), A + (c.charCodeAt(1) - 65));
    }

    function formatKbps(bandwidth) {
        const bw = Number(bandwidth || 0);
        if (!bw) return '';
        if (bw >= 1_000_000) return `${(bw / 1_000_000).toFixed(1)} Mbps`;
        return `${Math.round(bw / 1000)} kbps`;
    }

    function hostOf(url) {
        try {
            return new URL(url).hostname;
        } catch {
            return '';
        }
    }

    const isInsecure = (url) => /^http:\/\//i.test(String(url || ''));
    const isOnline = (ch) => String((ch && ch.status) || '').toLowerCase() === 'online';

    function safeJsonParse(raw, fallback) {
        try {
            return raw ? JSON.parse(raw) : fallback;
        } catch {
            return fallback;
        }
    }

    function getSessionId() {
        let id = '';
        try {
            id = localStorage.getItem(LS_SESSION) || '';
        } catch {
            /* ignore */
        }
        if (!id) {
            const rnd =
                window.crypto && crypto.randomUUID
                    ? crypto.randomUUID()
                    : `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
            id = rnd;
            try {
                localStorage.setItem(LS_SESSION, id);
            } catch {
                /* ignore */
            }
        }
        return id;
    }

    // --------------------------------------------- category placeholder styles
    const GROUP_STYLES = [
        { id: 'news', re: /news|noticia|actualit|nachricht|info/i, glyph: '📰', a: '#1e3a8a', b: '#3b82f6' },
        { id: 'sports', re: /sport|futbol|football|soccer|nba|nfl|racing|golf|tennis/i, glyph: '⚽', a: '#14532d', b: '#16a34a' },
        { id: 'movies', re: /movie|film|cine|cinema/i, glyph: '🎬', a: '#7f1d1d', b: '#dc2626' },
        { id: 'entertainment', re: /entertain|series|comedy|drama|show|lifestyle/i, glyph: '🎭', a: '#581c87', b: '#c026d3' },
        { id: 'music', re: /music|musica|radio|hits|mtv/i, glyph: '🎵', a: '#831843', b: '#ec4899' },
        { id: 'documentary', re: /documentar|docu|nature|science|history|discovery/i, glyph: '🌍', a: '#78350f', b: '#d97706' },
        { id: 'kids', re: /kid|child|cartoon|anim|junior|family/i, glyph: '🧸', a: '#0e7490', b: '#22d3ee' },
        { id: 'religious', re: /relig|church|christian|islam|quran|gospel|faith|catholic/i, glyph: '🙏', a: '#3730a3', b: '#818cf8' },
        { id: 'weather', re: /weather|meteo|clima/i, glyph: '⛅', a: '#075985', b: '#38bdf8' },
        { id: 'shopping', re: /shop|qvc|retail|store|deal/i, glyph: '🛍️', a: '#9a3412', b: '#fb923c' },
        { id: 'general', re: /general|variety|misc|public|local/i, glyph: '📡', a: '#334155', b: '#64748b' },
    ];

    function groupStyle(title) {
        const t = String(title || '');
        if (!t) return null;
        for (const s of GROUP_STYLES) if (s.re.test(t)) return s;
        return null;
    }

    function hueFor(str) {
        let hash = 0;
        const s = String(str || 'tv');
        for (let i = 0; i < s.length; i++) hash = (hash * 31 + s.charCodeAt(i)) >>> 0;
        return hash % 360;
    }

    /** Colored tile + glyph by group_title; default TV badge + initial. */
    function placeholderHtml(channel) {
        const s = groupStyle(channel && channel.group_title);
        if (s) {
            return `<div class="ph" data-cat="${s.id}" style="--ph-a:${s.a};--ph-b:${s.b}"><span class="ph-glyph" aria-hidden="true">${s.glyph}</span></div>`;
        }
        const name = (channel && channel.name) || '';
        const initial = (name.trim().charAt(0) || '?').toUpperCase();
        const hue = hueFor((channel && channel.group_title) || name);
        return `<div class="ph ph-default" style="--ph-a:hsl(${hue} 55% 22%);--ph-b:hsl(${(hue + 40) % 360} 65% 42%)"><span class="ph-tv" aria-hidden="true">TV</span><span class="ph-initial" aria-hidden="true">${escapeHtml(initial)}</span></div>`;
    }

    function indexStyle(label) {
        const s = groupStyle(label);
        if (s) return s;
        const hue = hueFor(label);
        return { glyph: '📺', a: `hsl(${hue} 55% 22%)`, b: `hsl(${(hue + 40) % 360} 65% 42%)` };
    }

    // ---------------------------------------------------------------------- app
    class LiveTVGuide {
        constructor() {
            // view state
            this.view = 'home'; // home | search | categories | servers | favorites | videos
            this.drill = null; // {kind:'group'|'server', value, label}
            this.q = '';
            this.sort = 'trending';
            this.includeTest = false;
            this.showPending = false;
            this.group = '';
            this.country = ''; // "server" in the UI
            this.groups = [];
            this.countries = [];

            // data
            this.favorites = this._loadFavorites(); // Map url -> snapshot
            this.recent = this._loadRecent(); // array of snapshots (newest first)
            this.rails = [];
            this.grid = null;
            this._viewToken = 0;
            this._railObserver = null;
            this._gridObserver = null;

            // fetch queue
            this._queue = [];
            this._active = 0;

            // player
            this.currentChannel = null;
            this.ctx = null; // {channels, loadMore}
            this.hls = null;
            this.video = null;
            this._playToken = 0;
            this._playStartedAt = 0;
            this._hasStarted = false;
            this._noPlaybackTimer = null;
            this._hbTimer = null;
            this._chromeTimer = null;
            this._lastPointerType = 'mouse';
            this.muted = false;
            this.volume = 1;
            this._reported = new Set(); // `${event}|${url}` debounce
            this._aliveReported = new Set();
            this._watchReported = new Set();
            this._insecureResolver = null;
            this._sessionId = getSessionId();

            // live status
            this.lastRevision = -1;
            this._es = null;
            this._sseDelay = SSE_BACKOFF_START_MS;
            this._sseTimer = null;
            this._pollId = null;
            this._softRefreshTimer = null;
            this._lastSoftRefresh = 0;
        }

        // -------------------------------------------------------------- init
        init() {
            this._setupNav();
            this._setupToolbar();
            this._setupMore();
            this._setupPlayer();
            this._setupInsecureModal();
            this._setupKeyboard();
            this._setupHistory();
            this._connectEventStream();
            this._fetchStatus();
            this._syncNav();
            this._syncToolbar();
            this._renderView();
        }

        // ----------------------------------------------------------- storage
        _snap(ch) {
            return {
                url: ch.url,
                name: ch.name || '',
                group_title: ch.group_title || '',
                country: ch.country || '',
                icon_url: ch.icon_url || '',
                generic_icon_url: ch.generic_icon_url || '/static/channel-generic.svg',
                status: ch.status || '',
                media_type: ch.media_type || '',
            };
        }

        _loadFavorites() {
            const map = new Map();
            const v2 = safeJsonParse(localStorage.getItem(LS_FAVORITES), null);
            if (Array.isArray(v2)) {
                for (const s of v2) if (s && s.url) map.set(s.url, s);
                return map;
            }
            // migrate v1 (urls only)
            const v1 = safeJsonParse(localStorage.getItem(LS_FAVORITES_V1), []);
            if (Array.isArray(v1)) {
                for (const u of v1) {
                    if (typeof u === 'string' && u) {
                        map.set(u, { url: u, name: hostOf(u) || 'Saved channel', group_title: '', country: '', icon_url: '', generic_icon_url: '/static/channel-generic.svg', status: '' });
                    }
                }
            }
            return map;
        }

        _persistFavorites() {
            try {
                localStorage.setItem(LS_FAVORITES, JSON.stringify([...this.favorites.values()]));
            } catch {
                /* ignore */
            }
        }

        _loadRecent() {
            const arr = safeJsonParse(localStorage.getItem(LS_RECENT), []);
            return Array.isArray(arr) ? arr.filter((s) => s && s.url).slice(0, MAX_RECENT) : [];
        }

        _pushRecent(ch) {
            this.recent = [this._snap(ch), ...this.recent.filter((s) => s.url !== ch.url)].slice(0, MAX_RECENT);
            try {
                localStorage.setItem(LS_RECENT, JSON.stringify(this.recent));
            } catch {
                /* ignore */
            }
        }

        _refreshSnapshots(list) {
            if (!Array.isArray(list) || !this.favorites.size) return;
            let dirty = false;
            for (const ch of list) {
                const cur = ch && this.favorites.get(ch.url);
                if (!cur) continue;
                const next = this._snap(ch);
                if (cur.name !== next.name || cur.icon_url !== next.icon_url || cur.generic_icon_url !== next.generic_icon_url || cur.group_title !== next.group_title || cur.status !== next.status) {
                    this.favorites.set(ch.url, next);
                    dirty = true;
                }
            }
            if (dirty) this._persistFavorites();
        }

        toggleFavorite(ch) {
            if (!ch || !ch.url) return;
            if (this.favorites.has(ch.url)) {
                this.favorites.delete(ch.url);
                this.notify('Removed from My list');
            } else {
                this.favorites.set(ch.url, this._snap(ch));
                this.notify('Added to My list', 'success');
            }
            this._persistFavorites();
            this._syncFavUi(ch.url);
            if (this.view === 'favorites') this._renderView();
        }

        _syncFavUi(url) {
            const fav = this.favorites.has(url);
            document.querySelectorAll('.tile').forEach((tile) => {
                if (tile.getAttribute('data-url') !== url) return;
                const b = tile.querySelector('.tile-fav');
                if (!b) return;
                b.classList.toggle('is-fav', fav);
                b.setAttribute('aria-pressed', String(fav));
                b.setAttribute('aria-label', fav ? 'Remove from My list' : 'Add to My list');
            });
            this._syncDockFav();
        }

        _syncDockFav() {
            const b = $('dockFav');
            if (!b) return;
            const fav = !!(this.currentChannel && this.favorites.has(this.currentChannel.url));
            b.classList.toggle('is-fav', fav);
            b.setAttribute('aria-pressed', String(fav));
            b.setAttribute('aria-label', fav ? 'Remove from My list' : 'Add to My list');
        }

        // --------------------------------------------------------------- nav
        _setupNav() {
            document.querySelectorAll('[data-nav]').forEach((btn) => {
                btn.addEventListener('click', () => {
                    const v = btn.getAttribute('data-nav');
                    if (v === 'more') {
                        this._openModal('moreSheet');
                        return;
                    }
                    this._go({ view: v, drill: null });
                    if (v === 'search') {
                        const inp = $('searchInput');
                        if (inp) {
                            inp.focus();
                            inp.select();
                        }
                    } else {
                        window.scrollTo({ top: 0, behavior: 'smooth' });
                    }
                });
            });
            $('viewBack')?.addEventListener('click', () => {
                this._go({ view: this.view, drill: null });
            });
        }

        _setupHistory() {
            history.replaceState({ view: this.view, drill: this.drill }, '');
            window.addEventListener('popstate', (e) => {
                const st = e.state;
                if (!st || !st.view) return;
                this._applyState(st.view, st.drill || null);
                this._renderView();
            });
        }

        _applyState(view, drill) {
            this.view = view;
            this.drill = drill;
            this.group = drill && drill.kind === 'group' ? drill.value : '';
            this.country = drill && drill.kind === 'server' ? drill.value : '';
            if (view !== 'search' && this.q) {
                this.q = '';
                const inp = $('searchInput');
                if (inp) inp.value = '';
            }
            this._syncDropdownValues();
        }

        _go(st, opts = {}) {
            const drill = st.drill || null;
            const same =
                st.view === this.view && JSON.stringify(drill) === JSON.stringify(this.drill);
            if (same && !opts.force) {
                return;
            }
            this._applyState(st.view, drill);
            try {
                history[opts.replace ? 'replaceState' : 'pushState']({ view: this.view, drill: this.drill }, '');
            } catch {
                /* ignore */
            }
            this._renderView();
        }

        _syncNav() {
            document.querySelectorAll('[data-nav]').forEach((btn) => {
                const v = btn.getAttribute('data-nav');
                const active = v === this.view;
                if (active) btn.setAttribute('aria-current', 'page');
                else btn.removeAttribute('aria-current');
            });
        }

        _syncDropdownValues() {
            const g = $('groupFilter');
            const c = $('countryFilter');
            if (g) g.value = this.group || '';
            if (c) c.value = this.country || '';
        }

        // ----------------------------------------------------------- toolbar
        _setupToolbar() {
            const inp = $('searchInput');
            if (inp) {
                const run = debounce(() => {
                    const next = inp.value.trim();
                    if (next === this.q) return;
                    this.q = next;
                    if (this.view !== 'search') {
                        if (next) this._go({ view: 'search', drill: null });
                    } else {
                        this._renderView();
                    }
                }, SEARCH_DEBOUNCE_MS);
                inp.addEventListener('input', run);
                inp.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape') {
                        inp.value = '';
                        inp.blur();
                        if (this.q) {
                            this.q = '';
                            if (this.view === 'search') this._renderView();
                        }
                    } else if (e.key === 'Enter') {
                        this.q = inp.value.trim();
                        if (this.view !== 'search') this._go({ view: 'search', drill: null });
                        else this._renderView();
                        inp.blur();
                    }
                });
            }

            const sort = $('sortFilter');
            sort?.addEventListener('change', () => {
                this.sort = sort.value || 'trending';
                this._renderView();
            });
        }

        _syncToolbar() {
            const wrap = $('sortWrap');
            const showSort =
                this.view === 'search' ||
                this.view === 'videos' ||
                (this.drill && (this.view === 'categories' || this.view === 'servers'));
            if (wrap) wrap.classList.toggle('hidden', !showSort);
            const sort = $('sortFilter');
            if (sort) sort.value = this.sort;
        }

        // -------------------------------------------------------------- more
        _setupMore() {
            const t = $('testToggle');
            const p = $('pendingToggle');
            t?.addEventListener('change', () => {
                this.includeTest = t.checked;
                this._renderView();
            });
            p?.addEventListener('change', () => {
                this.showPending = p.checked;
                this.notify(p.checked ? 'Showing pending / offline streams' : 'Showing live streams only');
                this._renderView();
            });

            const origin = `${window.location.protocol}//${window.location.host}`;
            const m3u = `${origin}/api/v1/playlist.m3u`;
            const jm = $('jellyfinM3u');
            if (jm) jm.textContent = m3u;
            $('copyM3uBtn')?.addEventListener('click', () => this._copy(m3u, 'Playlist link copied'));
            $('purgeRescanBtn')?.addEventListener('click', () => this._purgeAndRescan());

            document.querySelectorAll('[data-modal-dismiss]').forEach((el) => {
                el.addEventListener('click', () => {
                    const modal = el.closest('.modal');
                    if (modal) this._closeModal(modal.id);
                });
            });
        }

        _openModal(id) {
            const m = $(id);
            if (!m) return;
            this._modalReturnFocus = document.activeElement;
            m.classList.remove('hidden');
            m.setAttribute('aria-hidden', 'false');
            document.body.classList.add('overflow-hidden');
            const focusable = m.querySelector('input, button:not([data-modal-dismiss]):not(.modal-backdrop), a[href]');
            setTimeout(() => (id === 'insecureModal' ? $('insecureContinue') : focusable)?.focus(), 30);
        }

        _closeModal(id) {
            const m = $(id);
            if (!m || m.classList.contains('hidden')) return;
            m.classList.add('hidden');
            m.setAttribute('aria-hidden', 'true');
            if (!document.querySelector('.modal:not(.hidden)')) {
                document.body.classList.remove('overflow-hidden');
            }
            try {
                this._modalReturnFocus?.focus?.();
            } catch {
                /* ignore */
            }
        }

        _copy(text, okMsg) {
            if (navigator.clipboard?.writeText) {
                navigator.clipboard
                    .writeText(text)
                    .then(() => this.notify(okMsg || 'Copied'))
                    .catch(() => this.notify('Could not copy — long-press to copy manually', 'error'));
            } else {
                this.notify('Copy not supported here', 'error');
            }
        }

        async _purgeAndRescan() {
            const passwordInput = $('purgePassword');
            const password = (passwordInput?.value || '').trim();
            if (!password) {
                this.notify('Enter the admin password first', 'error');
                passwordInput?.focus();
                return;
            }
            const ok = window.confirm(
                'Delete all stored channels and fetch playlists from scratch?\n\n' +
                    'This cannot be undone (except from a DB backup).'
            );
            if (!ok) return;
            const btn = $('purgeRescanBtn');
            if (btn) btn.disabled = true;
            try {
                const headers = {
                    Accept: 'application/json',
                    'Content-Type': 'application/json',
                    'X-Admin-Password': password,
                };
                const res = await fetch('/admin/purge', {
                    method: 'POST',
                    headers,
                    body: JSON.stringify({ confirm: 'purge', password }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    const msg =
                        data.message ||
                        data.error ||
                        (res.status === 429
                            ? 'Too many purge attempts — wait and try again'
                            : `HTTP ${res.status}`);
                    throw new Error(msg);
                }
                if (passwordInput) passwordInput.value = '';
                this.notify(
                    `Purged ${Number(data.purged_channels) || 0} channels — fetching fresh sources…`,
                    'success'
                );
                this._closeModal('moreSheet');
                this.lastRevision = -1;
                setTimeout(() => this._renderView(), 1500);
            } catch (err) {
                this.notify(String(err.message || err), 'error');
            } finally {
                if (btn) btn.disabled = false;
            }
        }

        // --------------------------------------------------------- keyboard
        _setupKeyboard() {
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    if (!$('insecureModal').classList.contains('hidden')) {
                        this._resolveInsecure(false);
                        return;
                    }
                    if (!$('moreSheet').classList.contains('hidden')) {
                        this._closeModal('moreSheet');
                        return;
                    }
                }
                if (e.ctrlKey || e.metaKey || e.altKey) return;
                const t = e.target;
                const typing =
                    t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
                if (typing) return;
                if (document.querySelector('.modal:not(.hidden)')) return;
                const k = e.key;
                if (k === '/') {
                    e.preventDefault();
                    $('searchInput')?.focus();
                } else if ((k === ' ' || k === 'k') && this.currentChannel) {
                    if (k === ' ' && t && t.closest && t.closest('button, a')) return;
                    e.preventDefault();
                    this.togglePlay();
                } else if (k === 'm') {
                    this.toggleMute();
                } else if (k === 'f') {
                    this.toggleFullscreen();
                } else if (k === 'n') {
                    this.playNext();
                } else if (k === 'p') {
                    this.playPrev();
                }
            });
        }

        // ------------------------------------------------------- fetch queue
        _schedule(task, priority = false) {
            return new Promise((resolve, reject) => {
                const job = { task, resolve, reject };
                if (priority) this._queue.unshift(job);
                else this._queue.push(job);
                this._pump();
            });
        }

        _pump() {
            while (this._active < MAX_CONCURRENT_FETCHES && this._queue.length) {
                const job = this._queue.shift();
                this._active++;
                Promise.resolve()
                    .then(job.task)
                    .then(job.resolve, job.reject)
                    .finally(() => {
                        this._active--;
                        this._pump();
                    });
            }
        }

        async _getJson(url, tries = 3) {
            for (let i = 0; i < tries; i++) {
                const res = await fetch(url, { headers: { Accept: 'application/json' } });
                if (res.status === 429 && i < tries - 1) {
                    const ra = Number(res.headers.get('Retry-After')) || 1;
                    await sleep(Math.min(ra, 6) * 1000 + 150);
                    continue;
                }
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (data && data.error && !Array.isArray(data.channels)) throw new Error(String(data.error));
                if (data && data.error && Array.isArray(data.channels) && !data.channels.length && !data.total_channels) {
                    throw new Error(String(data.error));
                }
                return data;
            }
            throw new Error('Too many requests');
        }

        _buildQuery(params, page, limit) {
            const p = new URLSearchParams();
            p.set('page', String(page));
            p.set('limit', String(limit));
            if (params.q) p.set('q', params.q);
            if (params.group) p.set('group', params.group);
            if (params.country) p.set('country', params.country);
            p.set('sort', params.sort || 'trending');
            if (params.sort_dir) p.set('sort_dir', params.sort_dir);
            if (params.status) p.set('status', params.status);
            if (params.videos) p.set('videos', '1');
            else if (!this.showPending && !params.status) p.set('media_type', 'live');
            else if (!this.showPending && params.status === 'online') p.set('media_type', 'live');
            if (this.includeTest) p.set('include_test', '1');
            // Explicit status rails (e.g. recently scanned) ignore the pending dig toggle.
            if (this.showPending && !params.status) p.set('pending', '1');
            return p.toString();
        }

        async _fetchChannels(params, page, limit, opts = {}) {
            const qs = this._buildQuery(params, page, limit);
            const data = await this._schedule(
                () => (opts.isStale && opts.isStale() ? null : this._getJson(`/channels?${qs}`)),
                !!opts.priority
            );
            if (!data) return null;
            if (typeof data.revision === 'number') this.lastRevision = data.revision;
            this._ingestMeta(data);
            this._refreshSnapshots(data.channels);
            return data;
        }

        _ingestMeta(data) {
            const groups = Array.isArray(data.groups) ? data.groups : null;
            const countries = Array.isArray(data.countries) ? data.countries : null;
            if (groups) {
                this.groups = groups
                    .map((g) => {
                        if (g == null) return null;
                        if (typeof g === 'string') return { name: g, count: 0 };
                        const name = String(g.name || g.label || '').trim();
                        return name ? { name, count: Number(g.count) || 0 } : null;
                    })
                    .filter(Boolean);
            }
            if (countries) {
                this.countries = countries
                    .map((c) => {
                        if (c == null) return null;
                        if (typeof c === 'string') return { code: c, name: c, count: 0 };
                        const code = String(c.code || c.value || '').trim();
                        const name = String(c.name || c.label || code).trim();
                        return code || name ? { code: code || name, name: name || code, count: Number(c.count) || 0 } : null;
                    })
                    .filter(Boolean);
            }
            if (groups || countries) this._fillHiddenSelects();
        }

        _fillHiddenSelects() {
            const g = $('groupFilter');
            const c = $('countryFilter');
            if (g) {
                g.innerHTML =
                    '<option value="">All categories</option>' +
                    this.groups.map((x) => `<option value="${escapeHtml(x.name)}">${escapeHtml(x.name)}</option>`).join('');
            }
            if (c) {
                c.innerHTML =
                    '<option value="">All servers</option>' +
                    this.countries.map((x) => `<option value="${escapeHtml(x.code)}">${escapeHtml(x.name)}</option>`).join('');
            }
            this._syncDropdownValues();
        }

        async _ensureMeta() {
            if (this.groups.length || this.countries.length) return;
            try {
                await this._fetchChannels({ sort: 'trending' }, 1, 1, { priority: true });
            } catch {
                /* surfaced by caller */
            }
        }

        countryName(code) {
            if (!code) return '';
            const up = String(code).toUpperCase();
            const hit = this.countries.find((c) => String(c.code).toUpperCase() === up);
            return hit ? hit.name : code;
        }

        // ------------------------------------------------------------- views
        _teardownView() {
            if (this._railObserver) this._railObserver.disconnect();
            if (this._gridObserver) this._gridObserver.disconnect();
            this._railObserver = null;
            this._gridObserver = null;
            this.rails = [];
            this.grid = null;
        }

        _setHeader(title, sub, showBack) {
            const t = $('viewTitle');
            const s = $('viewSub');
            const b = $('viewBack');
            if (t) t.textContent = title || '';
            if (s) s.textContent = sub || '';
            if (b) b.classList.toggle('hidden', !showBack);
        }

        _renderView() {
            this._viewToken++;
            this._teardownView();
            this._syncNav();
            this._syncToolbar();
            const body = $('viewBody');
            if (!body) return;
            body.innerHTML = '';
            body.setAttribute('aria-busy', 'true');

            const token = this._viewToken;
            switch (this.view) {
                case 'search':
                    this._buildGrid({
                        title: this.q ? `Results for “${this.q}”` : 'Search',
                        sub: this.q ? '' : 'Type above, or browse everything',
                        params: { q: this.q, sort: this.sort },
                        emptyMsg: this.q ? `No channels match “${this.q}”.` : 'No channels yet.',
                    });
                    break;
                case 'videos':
                    this._buildGrid({
                        title: 'Videos',
                        sub: 'On-demand streams',
                        params: { videos: true, sort: this.sort },
                        emptyMsg: 'No on-demand videos found.',
                    });
                    break;
                case 'categories':
                    if (this.drill && this.drill.kind === 'group') {
                        this._buildGrid({
                            title: this.drill.label || this.drill.value,
                            sub: 'Category',
                            back: true,
                            params: { group: this.drill.value, sort: this.sort },
                            emptyMsg: 'Nothing live in this category right now.',
                        });
                    } else {
                        this._buildCategoryIndex(token);
                    }
                    break;
                case 'servers':
                    if (this.drill && this.drill.kind === 'server') {
                        this._buildGrid({
                            title: `${countryFlagEmoji(this.drill.value)} ${this.drill.label || this.drill.value}`,
                            sub: 'Server location',
                            back: true,
                            params: { country: this.drill.value, sort: this.sort },
                            emptyMsg: 'Nothing live from this server right now.',
                        });
                    } else {
                        this._buildServerIndex(token);
                    }
                    break;
                case 'favorites':
                    this._buildFavorites();
                    break;
                default:
                    this._buildHome(token);
            }
        }

        _stateCard(title, msg, ctaLabel, onCta) {
            const el = h(
                `<div class="state-card"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(msg || '')}</span></div>`
            );
            if (ctaLabel && onCta) {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'cta';
                b.textContent = ctaLabel;
                b.addEventListener('click', onCta);
                el.appendChild(document.createElement('br'));
                el.appendChild(b);
            }
            return el;
        }

        // ---------------------------------------------------------------- home
        _buildHome(token) {
            const body = $('viewBody');
            this._setHeader(
                this.showPending ? 'Unchecked streams' : 'Live TV',
                this.showPending ? 'These may not load — if one plays, it helps everyone.' : 'Tap a tile to watch.',
                false
            );
            const wrap = document.createElement('div');
            body.appendChild(wrap);

            if (this.favorites.size) {
                this._addRail(wrap, {
                    key: 'mylist',
                    title: 'My list',
                    local: [...this.favorites.values()].reverse(),
                    moreView: 'favorites',
                });
            }
            if (this.recent.length) {
                this._addRail(wrap, { key: 'recent', title: 'Recently watched', local: this.recent });
            }

            const trending = this._addRail(wrap, {
                key: 'trending',
                title: this.showPending ? 'Worth a try' : 'Trending now',
                params: { sort: 'trending' },
                eager: true,
                moreView: 'search',
            });
            trending.onFirstLoad = () => {
                if (token !== this._viewToken) return;
                body.setAttribute('aria-busy', 'false');
                this._addRecentlyScannedRails(wrap, token);
                this._addCategoryRails(wrap, token);
            };
            trending.onError = () => {
                if (token === this._viewToken) body.setAttribute('aria-busy', 'false');
            };
        }

        _addRecentlyScannedRails(wrap, token) {
            if (token !== this._viewToken) return;
            this._addRail(wrap, {
                key: 'scanned-online',
                title: 'Recently scanned · Online',
                params: {
                    status: 'online',
                    sort: 'last_checked_at',
                    sort_dir: 'desc',
                },
                lazy: true,
                hideIfEmpty: true,
            });
            this._addRail(wrap, {
                key: 'scanned-offline',
                title: 'Recently scanned · Offline',
                params: {
                    status: 'offline',
                    sort: 'last_checked_at',
                    sort_dir: 'desc',
                },
                lazy: true,
                hideIfEmpty: true,
            });
        }

        _addCategoryRails(wrap, token) {
            if (token !== this._viewToken) return;
            const skip = /^(test|undefined|uncategori[sz]ed|unknown|other|none)$/i;
            const cats = this.groups
                .filter((g) => g.name && !skip.test(g.name.trim()))
                .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
                .slice(0, HOME_CATEGORY_RAILS);
            for (const g of cats) {
                this._addRail(wrap, {
                    key: `g:${g.name}`,
                    title: g.name,
                    params: { group: g.name, sort: 'trending' },
                    moreDrill: { kind: 'group', value: g.name, label: g.name },
                    lazy: true,
                    hideIfEmpty: true,
                });
            }
        }

        // ---------------------------------------------------------------- rails
        _addRail(parent, cfg) {
            const rail = {
                key: cfg.key,
                title: cfg.title,
                params: cfg.params || {},
                items: [],
                seen: new Set(),
                page: 0,
                hasMore: !cfg.local,
                loading: false,
                local: !!cfg.local,
                hideIfEmpty: !!cfg.hideIfEmpty,
                eager: !!cfg.eager,
                onFirstLoad: null,
                onError: null,
            };

            const sec = document.createElement('section');
            sec.className = 'rail-section';
            sec.setAttribute('aria-label', cfg.title);
            const seeAll = cfg.moreDrill || cfg.moreView ? '<button type="button" class="rail-more">See all</button>' : '';
            sec.innerHTML = `
                <div class="rail-head"><h2 class="rail-title">${escapeHtml(cfg.title)}</h2>${seeAll}</div>
                <div class="rail-wrap">
                    <button type="button" class="rail-arrow rail-arrow-left is-off" aria-label="Scroll left" tabindex="-1">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m15 5-7 7 7 7"/></svg>
                    </button>
                    <div class="rail-track" role="list"></div>
                    <button type="button" class="rail-arrow rail-arrow-right is-off" aria-label="Scroll right" tabindex="-1">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m9 5 7 7-7 7"/></svg>
                    </button>
                </div>`;
            parent.appendChild(sec);
            rail.sec = sec;
            rail.track = sec.querySelector('.rail-track');
            rail.arrowL = sec.querySelector('.rail-arrow-left');
            rail.arrowR = sec.querySelector('.rail-arrow-right');

            sec.querySelector('.rail-more')?.addEventListener('click', () => {
                if (cfg.moreDrill) this._go({ view: 'categories', drill: cfg.moreDrill });
                else if (cfg.moreView) this._go({ view: cfg.moreView, drill: null });
                window.scrollTo({ top: 0, behavior: 'smooth' });
            });

            const scrollBy = (dir) =>
                rail.track.scrollBy({ left: dir * Math.max(200, rail.track.clientWidth * 0.85), behavior: 'smooth' });
            rail.arrowL.addEventListener('click', () => scrollBy(-1));
            rail.arrowR.addEventListener('click', () => scrollBy(1));

            let raf = 0;
            rail.track.addEventListener(
                'scroll',
                () => {
                    if (raf) return;
                    raf = requestAnimationFrame(() => {
                        raf = 0;
                        this._railScrolled(rail);
                    });
                },
                { passive: true }
            );

            this.rails.push(rail);

            if (rail.local) {
                this._renderRailItems(rail, cfg.local);
                this._updateRailArrows(rail);
            } else {
                this._renderSkeletons(rail, 6);
                if (rail.eager) {
                    this._loadRail(rail);
                } else {
                    this._observeRail(rail);
                }
            }
            return rail;
        }

        _observeRail(rail) {
            if (!('IntersectionObserver' in window)) {
                this._loadRail(rail);
                return;
            }
            if (!this._railObserver) {
                this._railObserver = new IntersectionObserver(
                    (entries) => {
                        for (const en of entries) {
                            if (!en.isIntersecting) continue;
                            const r = this.rails.find((x) => x.sec === en.target);
                            this._railObserver.unobserve(en.target);
                            if (r) this._loadRail(r);
                        }
                    },
                    { rootMargin: '120px 0px' }
                );
            }
            this._railObserver.observe(rail.sec);
        }

        _renderSkeletons(rail, n) {
            const frag = document.createDocumentFragment();
            for (let i = 0; i < n; i++) {
                frag.appendChild(
                    h(
                        '<div class="tile tile-skeleton" role="presentation" aria-hidden="true"><span class="tile-art"></span><span class="sk-line"></span><span class="sk-line" style="width:45%"></span></div>'
                    )
                );
            }
            rail.track.appendChild(frag);
        }

        _clearSkeletons(rail) {
            rail.track.querySelectorAll('.tile-skeleton, .rail-empty, .rail-retry').forEach((n) => n.remove());
        }

        _railCtx(rail) {
            return {
                channels: rail.items,
                loadMore: rail.local || !rail.hasMore ? null : () => this._loadRail(rail),
            };
        }

        _renderRailItems(rail, list) {
            const frag = document.createDocumentFragment();
            let added = 0;
            for (const ch of list) {
                if (!ch || !ch.url || rail.seen.has(ch.url)) continue;
                rail.seen.add(ch.url);
                rail.items.push(ch);
                frag.appendChild(this._createTile(ch, () => this._railCtx(rail)));
                added++;
            }
            rail.track.appendChild(frag);
            return added;
        }

        async _loadRail(rail) {
            if (rail.loading || !rail.hasMore || rail.local) return 0;
            rail.loading = true;
            const token = this._viewToken;
            const page = rail.page + 1;
            if (page > 1) this._renderSkeletons(rail, 3);
            let added = 0;
            try {
                const data = await this._fetchChannels(rail.params, page, RAIL_PAGE, {
                    priority: rail.eager,
                    isStale: () => token !== this._viewToken,
                });
                if (!data || token !== this._viewToken) return 0;
                this._clearSkeletons(rail);
                const list = Array.isArray(data.channels) ? data.channels : [];
                added = this._renderRailItems(rail, list);
                rail.page = page;
                rail.hasMore = !!data.has_more && list.length > 0;

                if (page === 1) {
                    if (!rail.items.length) {
                        if (rail.hideIfEmpty) {
                            rail.sec.remove();
                            this.rails = this.rails.filter((r) => r !== rail);
                        } else {
                            rail.track.appendChild(
                                h(
                                    `<p class="rail-empty">${
                                        this.showPending
                                            ? 'Nothing to show here yet.'
                                            : 'No live channels yet — the scanner is still checking streams. Try “Show pending” in More.'
                                    }</p>`
                                )
                            );
                        }
                    }
                    if (rail.onFirstLoad) rail.onFirstLoad();
                }
                this._updateRailArrows(rail);
                this._fillRailIfShort(rail);
            } catch (err) {
                if (token !== this._viewToken) return 0;
                this._clearSkeletons(rail);
                const retry = h(
                    '<div class="rail-retry state-card" style="flex:1;padding:1rem">Couldn\'t load this row. <button type="button" class="cta" style="margin:0 0 0 .5rem">Retry</button></div>'
                );
                retry.querySelector('button').addEventListener('click', () => {
                    this._clearSkeletons(rail);
                    this._renderSkeletons(rail, 6);
                    this._loadRail(rail);
                });
                rail.track.appendChild(retry);
                if (rail.onError) rail.onError(err);
            } finally {
                rail.loading = false;
            }
            return added;
        }

        _fillRailIfShort(rail) {
            // If the first page doesn't overflow the viewport, fetch more so the row can scroll.
            if (!rail.hasMore || rail.local) return;
            requestAnimationFrame(() => {
                if (!rail.track.isConnected) return;
                if (rail.track.scrollWidth <= rail.track.clientWidth + 40 && rail.page < 4) {
                    this._loadRail(rail);
                }
            });
        }

        _railScrolled(rail) {
            this._updateRailArrows(rail);
            const t = rail.track;
            if (rail.hasMore && !rail.loading && t.scrollLeft + t.clientWidth > t.scrollWidth - 480) {
                this._loadRail(rail);
            }
        }

        _updateRailArrows(rail) {
            const t = rail.track;
            if (!t || !rail.arrowL) return;
            rail.arrowL.classList.toggle('is-off', t.scrollLeft < 8);
            const atEnd = t.scrollLeft + t.clientWidth >= t.scrollWidth - 8;
            rail.arrowR.classList.toggle('is-off', atEnd && !rail.hasMore);
        }

        // ---------------------------------------------------------------- tiles
        _createTile(ch, getCtx) {
            const name = ch.name || 'Untitled channel';
            const status = String(ch.status || '').toLowerCase();
            const pill =
                status && status !== 'online'
                    ? `<span class="tile-pill">${status === 'offline' || status === 'error' ? 'May be offline' : 'Unchecked'}</span>`
                    : '';
            const fav = this.favorites.has(ch.url);
            const active = this.currentChannel && this.currentChannel.url === ch.url;
            const flag = ch.country && String(ch.country).toUpperCase() !== 'GLOBAL' ? `${countryFlagEmoji(ch.country)} ` : '';
            const meta = `${flag}${ch.group_title || 'Live'}`.trim();

            const artUrl = ch.icon_url || ch.generic_icon_url || '/static/channel-generic.svg';
            const art = `<img class="tile-img" src="${escapeHtml(artUrl)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">`;

            const el = h(`
                <div class="tile${active ? ' is-active' : ''}" role="listitem" data-url="${escapeHtml(ch.url)}">
                    <button type="button" class="tile-main" aria-label="Play ${escapeHtml(name)}">
                        <span class="tile-art">${art}${pill}
                            <span class="tile-play" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></span>
                            <span class="tile-now">Now</span>
                        </span>
                        <span class="tile-name">${escapeHtml(name)}</span>
                        <span class="tile-meta">${escapeHtml(meta)}</span>
                    </button>
                    <button type="button" class="tile-fav${fav ? ' is-fav' : ''}" aria-pressed="${fav}" aria-label="${fav ? 'Remove from My list' : 'Add to My list'}">
                        <svg viewBox="0 0 24 24"><path d="M12 17.3 6.2 21l1.5-6.6L2.5 9.9l6.7-.6L12 3l2.8 6.3 6.7.6-5.2 4.5L17.8 21z"/></svg>
                    </button>
                </div>`);

            const img = el.querySelector('img.tile-img');
            if (img) {
                img.addEventListener('error', () => {
                    const holder = img.parentElement;
                    if (!holder) return;
                    img.remove();
                    holder.insertAdjacentHTML('afterbegin', placeholderHtml(ch));
                });
            }
            el.querySelector('.tile-main').addEventListener('click', () => this.selectAndPlay(ch, getCtx ? getCtx() : null));
            el.querySelector('.tile-fav').addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleFavorite(ch);
            });
            return el;
        }

        _markActiveTiles() {
            const cur = this.currentChannel && this.currentChannel.url;
            document.querySelectorAll('.tile[data-url]').forEach((tile) => {
                tile.classList.toggle('is-active', !!cur && tile.getAttribute('data-url') === cur);
            });
        }

        // ----------------------------------------------------------------- grid
        _buildGrid(cfg) {
            const body = $('viewBody');
            this._setHeader(cfg.title, cfg.sub || '', !!cfg.back);

            const gridEl = document.createElement('div');
            gridEl.className = 'tile-grid';
            gridEl.setAttribute('role', 'list');
            const foot = document.createElement('div');
            foot.className = 'grid-foot';
            const sentinel = document.createElement('div');
            sentinel.style.height = '1px';
            body.appendChild(gridEl);
            body.appendChild(foot);
            body.appendChild(sentinel);

            const grid = {
                params: cfg.params,
                items: [],
                seen: new Set(),
                page: 0,
                hasMore: true,
                loading: false,
                total: 0,
                gridEl,
                foot,
                emptyMsg: cfg.emptyMsg,
                cfg,
            };
            this.grid = grid;

            if ('IntersectionObserver' in window) {
                this._gridObserver = new IntersectionObserver(
                    (entries) => {
                        if (entries.some((e) => e.isIntersecting)) this._loadGrid();
                    },
                    { rootMargin: '700px 0px' }
                );
                this._gridObserver.observe(sentinel);
            }
            this._loadGrid();
        }

        _gridCtx(grid) {
            return {
                channels: grid.items,
                loadMore: grid.hasMore ? () => this._loadGrid() : null,
            };
        }

        async _loadGrid() {
            const grid = this.grid;
            if (!grid || grid.loading || !grid.hasMore) return 0;
            grid.loading = true;
            const token = this._viewToken;
            const page = grid.page + 1;
            this._gridFoot(grid, 'loading');
            let added = 0;
            try {
                const data = await this._fetchChannels(grid.params, page, GRID_PAGE, {
                    priority: true,
                    isStale: () => token !== this._viewToken,
                });
                if (!data || token !== this._viewToken) return 0;
                const list = Array.isArray(data.channels) ? data.channels : [];
                const frag = document.createDocumentFragment();
                for (const ch of list) {
                    if (!ch || !ch.url || grid.seen.has(ch.url)) continue;
                    grid.seen.add(ch.url);
                    grid.items.push(ch);
                    frag.appendChild(this._createTile(ch, () => this._gridCtx(grid)));
                    added++;
                }
                grid.gridEl.appendChild(frag);
                grid.page = page;
                grid.hasMore = !!data.has_more && list.length > 0;
                grid.total = Number(data.total_channels) || grid.items.length;
                $('viewBody')?.setAttribute('aria-busy', 'false');
                const sub = grid.cfg.sub ? `${grid.cfg.sub} · ` : '';
                const s = $('viewSub');
                if (s && grid.items.length) s.textContent = `${sub}${grid.total.toLocaleString()} channel${grid.total === 1 ? '' : 's'}`;
                this._gridFoot(grid, grid.items.length ? (grid.hasMore ? 'more' : 'end') : 'empty');
            } catch (err) {
                if (token !== this._viewToken) return 0;
                $('viewBody')?.setAttribute('aria-busy', 'false');
                this._gridFoot(grid, 'error');
            } finally {
                grid.loading = false;
            }
            return added;
        }

        _gridFoot(grid, state) {
            const foot = grid.foot;
            foot.innerHTML = '';
            if (state === 'loading') {
                if (!grid.items.length) {
                    const sk = document.createElement('div');
                    sk.className = 'tile-grid';
                    for (let i = 0; i < 12; i++) {
                        sk.appendChild(
                            h(
                                '<div class="tile tile-skeleton" aria-hidden="true"><span class="tile-art"></span><span class="sk-line"></span><span class="sk-line" style="width:45%"></span></div>'
                            )
                        );
                    }
                    foot.appendChild(sk);
                } else {
                    foot.appendChild(h('<p class="mt-4 text-center text-xs text-slate-500">Loading more…</p>'));
                }
            } else if (state === 'more') {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'load-more';
                b.textContent = 'Load more';
                b.addEventListener('click', () => this._loadGrid());
                foot.appendChild(b);
            } else if (state === 'empty') {
                foot.appendChild(
                    this._stateCard('Nothing here', grid.emptyMsg || 'No channels found.', this._hasFilters() ? 'Clear filters' : null, () => this._clearFilters())
                );
            } else if (state === 'error') {
                foot.appendChild(
                    this._stateCard('Couldn\'t load channels', 'Check your connection and try again.', 'Retry', () => {
                        grid.hasMore = true;
                        this._loadGrid();
                    })
                );
            }
        }

        _hasFilters() {
            return !!(this.q || this.showPending || this.includeTest);
        }

        _clearFilters() {
            this.q = '';
            this.showPending = false;
            this.includeTest = false;
            const inp = $('searchInput');
            if (inp) inp.value = '';
            const p = $('pendingToggle');
            const t = $('testToggle');
            if (p) p.checked = false;
            if (t) t.checked = false;
            this._go({ view: 'home', drill: null }, { force: true, replace: true });
        }

        // ------------------------------------------------------ category index
        async _buildCategoryIndex(token) {
            const body = $('viewBody');
            this._setHeader('Categories', 'Pick a category to browse.', false);
            await this._ensureMeta();
            if (token !== this._viewToken) return;
            body.setAttribute('aria-busy', 'false');
            const skip = /^(test|undefined|uncategori[sz]ed|unknown|none)$/i;
            const list = this.groups
                .filter((g) => g.name && (this.includeTest || !skip.test(g.name.trim())))
                .sort((a, b) => a.name.localeCompare(b.name));
            if (!list.length) {
                body.appendChild(
                    this._stateCard('No categories yet', 'Channels are still being scanned.', 'Reload', () => {
                        this.groups = [];
                        this._renderView();
                    })
                );
                return;
            }
            const grid = document.createElement('div');
            grid.className = 'index-grid';
            for (const g of list) {
                const s = indexStyle(g.name);
                const b = h(
                    `<button type="button" class="index-tile" style="--ph-a:${s.a};--ph-b:${s.b}">
                        <span class="index-tile-glyph" aria-hidden="true">${s.glyph}</span>
                        <span class="index-tile-name">${escapeHtml(g.name)}</span>
                    </button>`
                );
                b.addEventListener('click', () => {
                    this._go({ view: 'categories', drill: { kind: 'group', value: g.name, label: g.name } });
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                });
                grid.appendChild(b);
            }
            body.appendChild(grid);
        }

        // -------------------------------------------------------- server index
        async _buildServerIndex(token) {
            const body = $('viewBody');
            this._setHeader('Servers', 'Where the stream is hosted.', false);
            await this._ensureMeta();
            if (token !== this._viewToken) return;
            body.setAttribute('aria-busy', 'false');
            const list = [...this.countries].sort((a, b) => a.name.localeCompare(b.name));
            if (!list.length) {
                body.appendChild(
                    this._stateCard('No servers yet', 'Server locations are still being resolved.', 'Reload', () => {
                        this.countries = [];
                        this._renderView();
                    })
                );
                return;
            }
            const grid = document.createElement('div');
            grid.className = 'index-grid';
            for (const c of list) {
                const s = indexStyle(c.name);
                const b = h(
                    `<button type="button" class="index-tile" style="--ph-a:${s.a};--ph-b:${s.b}">
                        <span class="index-tile-glyph" aria-hidden="true">${countryFlagEmoji(c.code)}</span>
                        <span class="index-tile-name">${escapeHtml(c.name)}</span>
                        <span class="index-tile-sub">${escapeHtml(String(c.code).toUpperCase())}</span>
                    </button>`
                );
                b.addEventListener('click', () => {
                    this._go({ view: 'servers', drill: { kind: 'server', value: c.code, label: c.name } });
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                });
                grid.appendChild(b);
            }
            body.appendChild(grid);
        }

        // ----------------------------------------------------------- favorites
        _buildFavorites() {
            const body = $('viewBody');
            const items = [...this.favorites.values()].reverse();
            this._setHeader('My list', items.length ? `${items.length} saved` : '', false);
            body.setAttribute('aria-busy', 'false');
            if (!items.length) {
                body.appendChild(
                    this._stateCard('Nothing saved yet', 'Tap the ★ on any channel to keep it here.', 'Browse channels', () =>
                        this._go({ view: 'home', drill: null })
                    )
                );
                return;
            }
            const gridEl = document.createElement('div');
            gridEl.className = 'tile-grid';
            gridEl.setAttribute('role', 'list');
            const ctx = { channels: items, loadMore: null };
            for (const ch of items) gridEl.appendChild(this._createTile(ch, () => ctx));
            body.appendChild(gridEl);
        }

        // ============================================================ PLAYER
        _setupPlayer() {
            const wrap = $('videoPlayer');
            if (!wrap) return;

            document.querySelectorAll('[data-player-action]').forEach((btn) => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const a = btn.getAttribute('data-player-action');
                    this._playerAction(a);
                    this._showChrome();
                });
            });

            const tap = $('playerTap');
            wrap.addEventListener('pointerdown', (e) => {
                this._lastPointerType = e.pointerType || 'mouse';
            });
            tap?.addEventListener('click', () => {
                if (!this.currentChannel) return;
                const touch = this._lastPointerType === 'touch' || this._lastPointerType === 'pen';
                if (touch && !wrap.classList.contains('chrome-on')) {
                    this._showChrome();
                    return;
                }
                this.togglePlay();
                this._showChrome();
            });
            tap?.addEventListener('dblclick', () => this.toggleFullscreen());

            wrap.addEventListener('pointermove', () => this._showChrome());
            wrap.addEventListener('focusin', () => this._showChrome());
            wrap.addEventListener('pointerleave', () => this._scheduleChromeHide());

            $('qualitySelect')?.addEventListener('change', (e) => this._onQualityChange(e.target.value));

            const onFs = () => {
                const fs = document.fullscreenElement || document.webkitFullscreenElement;
                wrap.classList.toggle('is-fs', fs === wrap);
                this._showChrome();
            };
            document.addEventListener('fullscreenchange', onFs);
            document.addEventListener('webkitfullscreenchange', onFs);

            if ('mediaSession' in navigator) {
                try {
                    navigator.mediaSession.setActionHandler('previoustrack', () => this.playPrev());
                    navigator.mediaSession.setActionHandler('nexttrack', () => this.playNext());
                    navigator.mediaSession.setActionHandler('play', () => this.video && this.video.play().catch(() => {}));
                    navigator.mediaSession.setActionHandler('pause', () => this.video && this.video.pause());
                } catch {
                    /* ignore */
                }
            }
        }

        _playerAction(a) {
            switch (a) {
                case 'toggle-play':
                    this.togglePlay();
                    break;
                case 'toggle-mute':
                    this.toggleMute();
                    break;
                case 'fullscreen':
                    this.toggleFullscreen();
                    break;
                case 'prev':
                    this.playPrev();
                    break;
                case 'next':
                    this.playNext();
                    break;
                case 'fav':
                    if (this.currentChannel) this.toggleFavorite(this.currentChannel);
                    else this.notify('Pick a channel first');
                    break;
                case 'copy':
                    if (this.currentChannel) this._copy(this.currentChannel.url, 'Stream link copied');
                    else this.notify('Pick a channel first');
                    break;
                case 'vlc':
                    if (this.currentChannel) {
                        try {
                            window.open(`vlc://${this.currentChannel.url}`, '_blank');
                            this.notify(`Opening ${this.currentChannel.name || 'stream'} in VLC…`);
                        } catch {
                            this.notify('Could not launch VLC', 'error');
                        }
                    } else this.notify('Pick a channel first');
                    break;
                case 'mini': {
                    const dock = $('playerDock');
                    const on = !dock.classList.contains('is-mini');
                    dock.classList.toggle('is-mini', on);
                    $('miniToggle')?.setAttribute('aria-pressed', String(on));
                    break;
                }
                case 'retry':
                    if (this.currentChannel) this.selectAndPlay(this.currentChannel, this.ctx);
                    break;
                default:
            }
        }

        togglePlay() {
            const v = this.video;
            if (!v) return;
            if (v.paused) v.play().catch(() => {});
            else v.pause();
        }

        toggleMute() {
            this.muted = !this.muted;
            if (this.video) this.video.muted = this.muted;
            this._syncPlayerUi();
        }

        toggleFullscreen() {
            const wrap = $('videoPlayer');
            if (!wrap || !this.currentChannel) return;
            const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
            if (fsEl) {
                (document.exitFullscreen || document.webkitExitFullscreen).call(document);
                return;
            }
            const req = wrap.requestFullscreen || wrap.webkitRequestFullscreen;
            if (req) {
                const p = req.call(wrap);
                if (p && p.catch) p.catch(() => {});
            } else if (this.video && this.video.webkitEnterFullscreen) {
                this.video.webkitEnterFullscreen();
            }
        }

        _syncPlayerUi() {
            const wrap = $('videoPlayer');
            if (!wrap) return;
            const v = this.video;
            wrap.classList.toggle('is-playing', !!(v && !v.paused && !v.ended));
            wrap.classList.toggle('is-paused', !(v && !v.paused && !v.ended));
            wrap.classList.toggle('is-muted', !!(v ? v.muted : this.muted));
            const cp = $('chromeCenterPlay');
            if (cp) cp.setAttribute('aria-label', v && !v.paused ? 'Pause' : 'Play');
        }

        _showChrome() {
            const wrap = $('videoPlayer');
            if (!wrap) return;
            wrap.classList.add('chrome-on');
            this._scheduleChromeHide();
        }

        _scheduleChromeHide() {
            clearTimeout(this._chromeTimer);
            this._chromeTimer = setTimeout(() => {
                const wrap = $('videoPlayer');
                if (!wrap) return;
                const v = this.video;
                const busy = document.activeElement === $('qualitySelect');
                if (v && !v.paused && !busy) wrap.classList.remove('chrome-on');
            }, CHROME_HIDE_MS);
        }

        // ------------------------------------------------- insecure confirm
        _setupInsecureModal() {
            $('insecureContinue')?.addEventListener('click', () => this._resolveInsecure(true));
            document.querySelectorAll('[data-insecure-cancel]').forEach((el) => {
                el.addEventListener('click', () => this._resolveInsecure(false));
            });
        }

        _insecureRemembered() {
            try {
                return sessionStorage.getItem(SS_INSECURE_OK) === '1';
            } catch {
                return !!this._insecureMem;
            }
        }

        _confirmInsecure(url) {
            if (this._insecureResolver) this._resolveInsecure(false);
            const host = $('insecureHost');
            if (host) host.textContent = hostOf(url) || 'This server';
            this._openModal('insecureModal');
            return new Promise((resolve) => {
                this._insecureResolver = resolve;
            });
        }

        _resolveInsecure(ok) {
            const r = this._insecureResolver;
            this._insecureResolver = null;
            if (ok) {
                this._insecureMem = true;
                try {
                    sessionStorage.setItem(SS_INSECURE_OK, '1');
                } catch {
                    /* ignore */
                }
            }
            this._closeModal('insecureModal');
            if (r) r(!!ok);
        }

        // ---------------------------------------------------- select & play
        async selectAndPlay(channel, ctx) {
            if (!channel || !channel.url) return;
            if (isInsecure(channel.url) && !this._insecureRemembered()) {
                const ok = await this._confirmInsecure(channel.url);
                if (!ok) return;
            }
            this.currentChannel = channel;
            if (ctx) this.ctx = ctx;
            else if (!this.ctx || !this.ctx.channels.some((c) => c.url === channel.url)) {
                this.ctx = { channels: [channel], loadMore: null };
            }

            this._updateNowPlaying(channel);
            this._pushRecent(channel);
            this._markActiveTiles();
            this._syncDockFav();
            this._startPlayback(channel);
            this._recordWatch(channel);
            if (channel.quality_count && channel.quality_count > 0) this._populateVariantOptions(channel);
        }

        _updateNowPlaying(channel) {
            const flag = countryFlagEmoji(channel.country);
            const title = `${flag} ${channel.name || 'Untitled channel'}`;
            const parts = [];
            if (channel.group_title) parts.push(channel.group_title);
            if (channel.country && String(channel.country).toUpperCase() !== 'GLOBAL') {
                parts.push(this.countryName(channel.country));
            }
            const host = hostOf(channel.url);
            const info = channel.playing_now
                ? String(channel.playing_now)
                : parts.join(' · ') || (host ? `Streaming from ${host}` : 'Live stream');
            const n = $('currentChannelName');
            const i = $('currentChannelInfo');
            const c = $('chromeTitle');
            if (n) n.textContent = title;
            if (i) i.textContent = info;
            if (c) c.textContent = title;
            const dot = $('liveDot');
            if (dot) {
                dot.className = `h-1.5 w-1.5 rounded-full ${isOnline(channel) ? 'bg-emerald-400' : 'bg-amber-400'}`;
            }
            $('insecureBadge')?.classList.toggle('hidden', !isInsecure(channel.url));
            this._setViewers(null);
            document.title = `${channel.name || 'Live'} — Live TV Guide`;

            if ('mediaSession' in navigator && window.MediaMetadata) {
                try {
                    navigator.mediaSession.metadata = new MediaMetadata({
                        title: channel.name || 'Live channel',
                        artist: channel.group_title || 'Live TV Guide',
                    });
                } catch {
                    /* ignore */
                }
            }
        }

        _recordWatch(channel) {
            const url = channel.url;
            if (this._watchReported.has(url)) return;
            this._watchReported.add(url);
            fetch('/api/watch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            }).catch(() => this._watchReported.delete(url));
        }

        // ------------------------------------------------- prev / next walk
        async playNext() {
            await this._step(1);
        }

        async playPrev() {
            await this._step(-1);
        }

        async _step(dir) {
            const cur = this.currentChannel;
            const ctx = this.ctx;
            if (!ctx || !ctx.channels.length) {
                this.notify('Pick a channel from a row first');
                return;
            }
            let idx = cur ? ctx.channels.findIndex((c) => c.url === cur.url) : -1;
            let nextIdx;
            if (idx < 0) nextIdx = dir > 0 ? 0 : ctx.channels.length - 1;
            else nextIdx = idx + dir;

            if (nextIdx >= ctx.channels.length) {
                if (ctx.loadMore) {
                    const before = ctx.channels.length;
                    try {
                        await ctx.loadMore();
                    } catch {
                        /* ignore */
                    }
                    if (ctx.channels.length > before) nextIdx = before;
                    else nextIdx = 0;
                } else {
                    nextIdx = 0;
                }
            } else if (nextIdx < 0) {
                nextIdx = ctx.channels.length - 1;
            }
            const next = ctx.channels[nextIdx];
            if (next) {
                this.selectAndPlay(next, ctx);
                this._scrollTileIntoView(next.url);
            }
        }

        _scrollTileIntoView(url) {
            const tile = document.querySelector(`.tile[data-url="${CSS.escape(url)}"]`);
            if (tile && tile.scrollIntoView) {
                try {
                    tile.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
                } catch {
                    /* ignore */
                }
            }
        }

        // ----------------------------------------------------------- playback
        _clearPlaybackTimers() {
            clearTimeout(this._noPlaybackTimer);
            this._noPlaybackTimer = null;
        }

        _showSpinner(on) {
            $('playerSpinner')?.classList.toggle('hidden', !on);
        }

        _showPlayerError(msg) {
            const wrap = $('videoPlayer');
            const box = $('playerError');
            const txt = $('playerErrorText');
            if (txt && msg) txt.textContent = msg;
            box?.classList.remove('hidden');
            wrap?.classList.add('has-error');
            this._showSpinner(false);
            this._showChrome();
        }

        _hidePlayerError() {
            $('playerError')?.classList.add('hidden');
            $('videoPlayer')?.classList.remove('has-error');
        }

        _startPlayback(channel, opts = {}) {
            const wrap = $('videoPlayer');
            const mount = $('videoMount');
            if (!wrap || !mount) return;
            const token = ++this._playToken;
            this._clearPlaybackTimers();
            this._stopHeartbeat();
            this._teardownHls();
            this._hasStarted = false;
            this._slowChecked = false;
            this._playStartedAt = performance.now();

            $('playerPlaceholder')?.classList.add('hidden');
            this._hidePlayerError();
            $('audioOnlyBadge')?.classList.add('hidden');
            wrap.classList.remove('is-empty');
            this._showSpinner(true);

            const url = channel.url;
            const proxied = `/proxy/stream?url=${encodeURIComponent(url)}`;
            const embedSrc = platformEmbedSrc(url);

            if (!opts.keepQuality) this._resetQualitySelect();

            // YouTube / Twitch: official embed players (never through /proxy/stream).
            if (embedSrc) {
                this.video = null;
                wrap.classList.add('is-embed');
                mount.innerHTML =
                    `<iframe src="${escapeHtml(embedSrc)}" title="Embedded player" ` +
                    `allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; fullscreen" ` +
                    `allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>`;
                this._showSpinner(false);
                this._hasStarted = true;
                this._syncPlayerUi();
                this._startHeartbeat(token);
                this._maybeReportAlive();
                return;
            }
            wrap.classList.remove('is-embed');

            const video = document.createElement('video');
            video.className = 'video-el';
            video.setAttribute('playsinline', '');
            video.setAttribute('webkit-playsinline', '');
            video.controls = false;
            video.autoplay = true;
            video.muted = true; // start muted so autoplay is always allowed
            video.volume = this.volume;
            mount.innerHTML = '';
            mount.appendChild(video);
            this.video = video;

            video.addEventListener('loadeddata', () => {
                if (token !== this._playToken) return;
                this._onStarted('loadeddata');
            });
            video.addEventListener('playing', () => {
                if (token !== this._playToken) return;
                this._showSpinner(false);
                this._hidePlayerError();
                this._onStarted('playing');
                if (!this._slowChecked) {
                    this._slowChecked = true;
                    this._maybeReportSlow();
                }
                this._syncPlayerUi();
                if (!this._hbTimer) this._startHeartbeat(token);
                this._maybeReportAlive();
            });
            video.addEventListener('pause', () => {
                if (token !== this._playToken) return;
                this._syncPlayerUi();
                this._stopHeartbeat();
                this._showChrome();
            });
            video.addEventListener('ended', () => {
                if (token !== this._playToken) return;
                this._syncPlayerUi();
                this._stopHeartbeat();
            });
            video.addEventListener('waiting', () => {
                if (token !== this._playToken) return;
                this._showSpinner(true);
            });
            video.addEventListener('volumechange', () => {
                if (token !== this._playToken) return;
                this._syncPlayerUi();
            });
            video.addEventListener('error', () => {
                if (token !== this._playToken || this.hls) return;
                this._onFatalPlayback('media-error');
            });
            video.addEventListener(
                'canplay',
                () => {
                    if (token !== this._playToken) return;
                    video.muted = this.muted;
                    video.play().catch(() => {
                        video.muted = true;
                        this.muted = true;
                        video.play().catch(() => {});
                        this._syncPlayerUi();
                        this.notify('Tap the speaker to unmute');
                    });
                },
                { once: true }
            );

            this._attachPlayer(video, proxied, url, token);
            this._syncPlayerUi();
            this._showChrome();

            // no_playback watchdog (only meaningful for streams we list as online)
            this._noPlaybackTimer = setTimeout(() => {
                if (token !== this._playToken || this._hasStarted) return;
                this._onFatalPlayback('timeout');
            }, NO_PLAYBACK_MS);
        }

        _onStarted() {
            if (this._hasStarted) return;
            this._hasStarted = true;
            this._clearPlaybackTimers();
        }

        // slow_load fires only when real playback began after SLOW_LOAD_MS
        _maybeReportSlow() {
            const ch = this.currentChannel;
            if (!ch) return;
            const loadMs = Math.round(performance.now() - this._playStartedAt);
            if (loadMs >= SLOW_LOAD_MS) this._reportPlayback(ch.url, 'slow_load', { load_ms: loadMs });
        }

        _onFatalPlayback(reason) {
            const ch = this.currentChannel;
            this._clearPlaybackTimers();
            this._showSpinner(false);
            const online = ch && isOnline(ch);
            this._showPlayerError(
                online
                    ? "This stream isn't responding right now. We've flagged it for a re-check."
                    : "This stream didn't load — it hasn't been verified yet. Try another channel."
            );
            if (ch && online) this._reportPlayback(ch.url, 'no_playback', { reason });
        }

        /** POST /api/report-playback — debounced per event+URL for the session. */
        _reportPlayback(url, event, extra = {}) {
            if (!url) return;
            const key = `${event}|${url}`;
            if (this._reported.has(key)) return;
            this._reported.add(key);
            const body = { url, event };
            if (event === 'slow_load' && extra.load_ms != null) body.load_ms = extra.load_ms;
            fetch('/api/report-playback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            }).catch(() => {});
        }

        _maybeReportAlive() {
            const ch = this.currentChannel;
            if (!ch || !ch.url) return;
            if (isOnline(ch)) return;
            if (this._aliveReported.has(ch.url)) return;
            this._aliveReported.add(ch.url);
            fetch('/api/report-alive', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: ch.url }),
            })
                .then((r) => r.json())
                .then((data) => {
                    if (!data || !data.ok) return;
                    ch.status = 'online';
                    if (this.favorites.has(ch.url)) this.favorites.set(ch.url, this._snap(ch));
                    const dot = $('liveDot');
                    if (dot && this.currentChannel === ch) dot.className = 'h-1.5 w-1.5 rounded-full bg-emerald-400';
                    document.querySelectorAll('.tile').forEach((tile) => {
                        if (tile.getAttribute('data-url') === ch.url) tile.querySelector('.tile-pill')?.remove();
                    });
                    if (data.promoted) this.notify('Stream verified — now listed as live for everyone', 'success');
                    if (typeof data.revision === 'number') this.lastRevision = data.revision;
                })
                .catch(() => this._aliveReported.delete(ch.url));
        }

        // ----------------------------------------------------------- heartbeat
        _startHeartbeat(token) {
            this._stopHeartbeat();
            const beat = () => {
                const ch = this.currentChannel;
                if (!ch || token !== this._playToken) return;
                fetch('/api/watching', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: ch.url, session_id: this._sessionId }),
                })
                    .then((r) => (r.ok ? r.json() : null))
                    .then((d) => {
                        if (d && d.ok && token === this._playToken) this._setViewers(d.viewers);
                    })
                    .catch(() => {});
            };
            beat();
            this._hbTimer = setInterval(beat, HEARTBEAT_MS);
        }

        _stopHeartbeat() {
            if (this._hbTimer) clearInterval(this._hbTimer);
            this._hbTimer = null;
        }

        _setViewers(n) {
            const badge = $('viewersBadge');
            const num = $('viewersCount');
            if (!badge) return;
            if (n == null || Number.isNaN(Number(n))) {
                badge.classList.add('hidden');
                return;
            }
            if (num) num.textContent = Number(n).toLocaleString();
            badge.classList.remove('hidden');
        }

        // ------------------------------------------------------------- quality
        _onQualityChange(value) {
            const v = String(value);
            if (this.hls) {
                if (v === '-1') {
                    this.hls.currentLevel = -1;
                    this.notify('Auto quality');
                    return;
                }
                if (v.startsWith('lvl:')) {
                    const idx = parseInt(v.slice(4), 10);
                    if (!Number.isNaN(idx)) this.hls.currentLevel = idx;
                    return;
                }
            }
            if (v.startsWith('var:')) {
                const varUrl = v.slice(4);
                if (varUrl && this.currentChannel) {
                    this._startPlayback({ ...this.currentChannel, url: varUrl }, { keepQuality: true });
                }
            }
        }

        _resetQualitySelect() {
            const q = $('qualitySelect');
            if (!q) return;
            q.innerHTML = '<option value="-1">Auto</option>';
            q.disabled = true;
            q.value = '-1';
        }

        _populateHlsQualityLevels(levels) {
            const q = $('qualitySelect');
            if (!q || !Array.isArray(levels) || !levels.length) return;
            const opts = ['<option value="-1">Auto</option>'];
            levels.forEach((lvl, i) => {
                const label = lvl.height ? `${lvl.height}p` : lvl.bitrate ? `${Math.round(lvl.bitrate / 1000)}k` : `Level ${i + 1}`;
                opts.push(`<option value="lvl:${i}">${escapeHtml(label)}</option>`);
            });
            q.innerHTML = opts.join('');
            q.disabled = levels.length < 2;
            q.value = '-1';
        }

        async _populateVariantOptions(channel) {
            const q = $('qualitySelect');
            if (!q || !channel.url || !channel.quality_count) return;
            try {
                const res = await fetch(`/api/variants?url=${encodeURIComponent(channel.url)}`);
                const data = await res.json();
                const list = Array.isArray(data.variants) ? data.variants : [];
                if (!list.length) return;
                if (q.options.length > 1 && !q.disabled) return; // hls.js already filled it
                if (!this.currentChannel || this.currentChannel.url !== channel.url) return;
                const opts = ['<option value="-1">Auto</option>'];
                list.forEach((v) => {
                    const label = v.resolution || (v.bandwidth ? formatKbps(v.bandwidth) : 'Variant');
                    opts.push(`<option value="var:${escapeHtml(v.url)}">${escapeHtml(label)}</option>`);
                });
                q.innerHTML = opts.join('');
                q.disabled = false;
                q.value = '-1';
            } catch {
                /* ignore */
            }
        }

        _teardownHls() {
            if (this.hls) {
                try {
                    this.hls.destroy();
                } catch {
                    /* ignore */
                }
                this.hls = null;
            }
        }

        _setAudioOnlyUi(on) {
            $('audioOnlyBadge')?.classList.toggle('hidden', !on);
        }

        _attachPlayer(video, playUrl, originalUrl, token) {
            const path = (originalUrl || '').split(/[?#]/)[0].toLowerCase();
            const progressive = /\.(mp4|webm|ogv|mp3|aac|m4a|ogg|wav)$/i.test(path);
            const audioFile = /\.(mp3|aac|m4a|ogg|wav)$/i.test(path);

            // Decide audio-only from real video dimensions after playback starts.
            const watchDimensions = () => {
                const reveal = () => {
                    if (!video.isConnected || token !== this._playToken) return;
                    if (video.videoWidth > 0 && video.videoHeight > 0) {
                        this._setAudioOnlyUi(false);
                        return;
                    }
                    if (!video.paused && video.currentTime > 0.25 && video.videoWidth === 0) {
                        this._setAudioOnlyUi(true);
                    }
                };
                video.addEventListener('loadeddata', reveal);
                video.addEventListener('playing', () => setTimeout(reveal, 400));
                video.addEventListener('resize', reveal);
            };

            if (progressive) {
                video.src = playUrl;
                if (audioFile) this._setAudioOnlyUi(true);
                else watchDimensions();
                return;
            }

            if (typeof Hls !== 'undefined' && Hls.isSupported()) {
                const hls = new Hls({
                    enableWorker: true,
                    lowLatencyMode: false,
                    maxBufferLength: 30,
                    backBufferLength: 30,
                    xhrSetup: (xhr) => {
                        try {
                            xhr.withCredentials = false;
                        } catch {
                            /* ignore */
                        }
                    },
                });
                this.hls = hls;
                let netRetries = 0;
                let mediaRetries = 0;
                hls.loadSource(playUrl);
                hls.attachMedia(video);
                hls.on(Hls.Events.MANIFEST_PARSED, (_, data) => {
                    if (token !== this._playToken) return;
                    if (data && data.levels && data.levels.length) this._populateHlsQualityLevels(data.levels);
                });
                hls.on(Hls.Events.ERROR, (_, data) => {
                    if (token !== this._playToken || !data || !data.fatal) return;
                    if (data.type === Hls.ErrorTypes.NETWORK_ERROR && netRetries < 3) {
                        netRetries++;
                        setTimeout(() => {
                            if (this.hls === hls && token === this._playToken) hls.startLoad();
                        }, 700 * netRetries);
                        return;
                    }
                    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && mediaRetries < 2) {
                        mediaRetries++;
                        hls.recoverMediaError();
                        return;
                    }
                    this._onFatalPlayback('hls-fatal');
                });
                watchDimensions();
                return;
            }

            // Native HLS (Safari / iOS) or plain src fallback
            video.src = playUrl;
            watchDimensions();
        }

        // ============================================== status + live updates
        _connectEventStream() {
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden && !this._es) {
                    clearTimeout(this._sseTimer);
                    this._sseDelay = SSE_BACKOFF_START_MS;
                    this._openEventStream();
                }
            });
            this._openEventStream();
        }

        _openEventStream() {
            if (this._es) return;
            if (!('EventSource' in window)) {
                this._startPolling();
                return;
            }
            let es;
            try {
                es = new EventSource('/api/events');
            } catch {
                this._scheduleSseRetry();
                return;
            }
            this._es = es;
            es.onopen = () => {
                this._sseDelay = SSE_BACKOFF_START_MS;
                this._stopPolling();
            };
            es.onmessage = (ev) => {
                this._sseDelay = SSE_BACKOFF_START_MS;
                this._stopPolling();
                try {
                    const d = JSON.parse(ev.data);
                    if (d.error) return;
                    this._applyStatus(d);
                    if (typeof d.revision === 'number' && d.revision !== this.lastRevision) {
                        this.lastRevision = d.revision;
                        this._scheduleSoftRefresh();
                    }
                } catch {
                    /* ignore */
                }
            };
            es.onerror = () => {
                // CONNECTING = browser is retrying on its own; CLOSED = we must reconnect.
                this._startPolling();
                if (es.readyState === 2 /* CLOSED */) {
                    try {
                        es.close();
                    } catch {
                        /* ignore */
                    }
                    if (this._es === es) this._es = null;
                    this._scheduleSseRetry();
                }
            };
        }

        _scheduleSseRetry() {
            clearTimeout(this._sseTimer);
            const jitter = Math.random() * 400;
            const delay = Math.min(this._sseDelay, SSE_BACKOFF_MAX_MS) + jitter;
            this._sseDelay = Math.min(this._sseDelay * 2, SSE_BACKOFF_MAX_MS);
            this._sseTimer = setTimeout(() => this._openEventStream(), delay);
        }

        _startPolling() {
            if (this._pollId) return;
            this._pollId = setInterval(() => this._fetchStatus(), POLL_FALLBACK_MS);
        }

        _stopPolling() {
            if (!this._pollId) return;
            clearInterval(this._pollId);
            this._pollId = null;
        }

        async _fetchStatus() {
            try {
                const r = await fetch('/status');
                const d = await r.json();
                if (d.error) return;
                this._applyStatus(d);
                if (typeof d.revision === 'number' && d.revision !== this.lastRevision) {
                    this.lastRevision = d.revision;
                    this._scheduleSoftRefresh();
                }
            } catch {
                /* ignore */
            }
        }

        _applyStatus(d) {
            const counts = d.counts && typeof d.counts === 'object' ? d.counts : null;
            const online = counts && typeof counts.online === 'number' ? counts.online : d.online_channels;
            const pending = counts
                ? (Number(counts.pending) || 0) + (Number(counts.unknown) || 0)
                : null;
            const set = (key, val) => {
                if (val == null || Number.isNaN(Number(val))) return;
                document.querySelectorAll(`[data-stat="${key}"]`).forEach((el) => {
                    el.textContent = Number(val).toLocaleString();
                });
            };
            set('online', online);
            set('pending', pending);
            const label = d.scanning ? 'Scanning…' : 'Ready';
            document.querySelectorAll('[data-stat="scan"]').forEach((el) => {
                el.textContent = label;
            });
        }

        _scheduleSoftRefresh() {
            clearTimeout(this._softRefreshTimer);
            const wait = Math.max(2500, SOFT_REFRESH_MIN_MS - (Date.now() - this._lastSoftRefresh));
            this._softRefreshTimer = setTimeout(() => this._softRefresh(), wait);
        }

        /** Quiet refresh: only touches rails sitting at their first page, never the player. */
        async _softRefresh() {
            if (document.hidden) {
                this._scheduleSoftRefresh();
                return;
            }
            this._lastSoftRefresh = Date.now();
            const token = this._viewToken;
            for (const rail of [...this.rails]) {
                if (rail.local || rail.page !== 1 || rail.loading || !rail.track.isConnected) continue;
                if (rail.track.scrollLeft > 20) continue;
                try {
                    const data = await this._fetchChannels(rail.params, 1, RAIL_PAGE, {
                        isStale: () => token !== this._viewToken,
                    });
                    if (!data || token !== this._viewToken) return;
                    const list = Array.isArray(data.channels) ? data.channels : [];
                    const sig = (arr) => arr.map((c) => `${c.url}|${c.status || ''}|${c.name || ''}`).join('\n');
                    if (sig(list) === sig(rail.items.slice(0, list.length)) && rail.items.length === list.length) continue;
                    if (rail.track.scrollLeft > 20 || rail.page !== 1) continue;
                    rail.track.innerHTML = '';
                    rail.items = [];
                    rail.seen = new Set();
                    this._renderRailItems(rail, list);
                    rail.hasMore = !!data.has_more;
                    this._updateRailArrows(rail);
                } catch {
                    /* keep stale rail */
                }
            }
        }

        // ------------------------------------------------------------- notify
        notify(message, type = 'info') {
            const host = $('notifyHost');
            if (!host) return;
            const el = document.createElement('div');
            el.className = `toast${type === 'error' ? ' toast-error' : type === 'success' ? ' toast-success' : ''}`;
            el.textContent = message;
            host.appendChild(el);
            while (host.children.length > 3) host.firstElementChild.remove();
            requestAnimationFrame(() => el.classList.add('is-in'));
            setTimeout(() => {
                el.classList.remove('is-in');
                setTimeout(() => el.remove(), 220);
            }, 2600);
        }
    }

    // ----------------------------------------------------------------- boot
    document.addEventListener('DOMContentLoaded', () => {
        const app = new LiveTVGuide();
        window.iptvScanner = app;
        app.init();
    });
})();
