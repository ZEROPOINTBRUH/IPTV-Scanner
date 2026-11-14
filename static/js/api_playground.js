/* API Playground — Easy chip multi-select + optional Expert mode. */
(function () {
  'use strict';

  var root = document.getElementById('playground');
  if (!root) return;

  var pageSize = Number(root.getAttribute('data-page-size') || 50);
  var waitHint = Number(root.getAttribute('data-wait') || 2);
  var base = (root.getAttribute('data-base') || '').replace(/\/$/, '') || location.origin;

  var state = {
    format: 'json',
    status: 'online',
    sort: 'trending',
    sort_dir: '',
    media_type: 'live',
    q: '',
    groups: [],
    servers: [],
    page: 1,
    limit: pageSize,
    facets: false,
    proxy: false,
    expert: false,
  };

  var catalog = { groups: [], servers: [] };
  var groupFilter = '';
  var serverFilter = '';
  var busyUntil = 0;
  var viewMode = 'cards';
  var syncingManual = false;

  var $ = function (id) { return document.getElementById(id); };
  var urlEl = $('apiUrl');
  var outEl = $('apiOut');
  var previewEl = $('apiPreview');
  var metaEl = $('apiMeta');
  var runBtn = $('apiRun');
  var proxyWrap = $('apiProxyWrap');
  var expertPanel = $('apiExpertPanel');
  var modeBadge = $('apiModeBadge');

  function uniqKeepOrder(values) {
    var out = [];
    var seen = Object.create(null);
    (values || []).forEach(function (v) {
      var s = String(v || '').trim();
      if (!s) return;
      var key = s.toLowerCase();
      if (seen[key]) return;
      seen[key] = true;
      out.push(s);
    });
    return out;
  }

  function parseCsv(text) {
    return uniqKeepOrder(String(text || '').split(','));
  }

  function joinCsv(values) {
    return uniqKeepOrder(values).join(',');
  }

  function mountChips(container, options, key, allowClear) {
    container.innerHTML = '';
    options.forEach(function (opt) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip';
      btn.textContent = opt.label;
      btn.dataset.value = opt.value;
      btn.addEventListener('click', function () {
        if (allowClear && state[key] === opt.value) state[key] = '';
        else state[key] = opt.value;
        syncChips(container, key);
        syncProxyVisibility();
        syncFilterAvailability();
        refreshUrl();
      });
      container.appendChild(btn);
    });
    syncChips(container, key);
  }

  function syncChips(container, key) {
    var cur = state[key];
    Array.prototype.forEach.call(container.querySelectorAll('.chip'), function (btn) {
      btn.classList.toggle('is-on', btn.dataset.value === String(cur));
    });
  }

  function syncProxyVisibility() {
    proxyWrap.classList.toggle('hidden', state.format !== 'playlist');
  }

  function syncFilterAvailability() {
    var locked = state.format === 'playlist';
    ['apiStatus', 'apiSort', 'apiSortDir', 'apiMedia'].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      Array.prototype.forEach.call(el.querySelectorAll('.chip'), function (btn) {
        btn.disabled = locked;
      });
    });
    ['apiQ', 'apiGroup', 'apiServer', 'apiPage', 'apiLimit', 'apiFacets',
      'apiGroupFilter', 'apiServerFilter', 'apiClearGroups', 'apiClearServers',
      'apiLoadFacets'].forEach(function (id) {
      var el = $(id);
      if (el) el.disabled = locked;
    });
    Array.prototype.forEach.call(
      document.querySelectorAll('#apiFacetGroupChips .chip, #apiFacetServerChips .chip'),
      function (btn) { btn.disabled = locked; }
    );
  }

  function setExpert(on) {
    state.expert = !!on;
    $('apiExpert').checked = state.expert;
    expertPanel.classList.toggle('hidden', !state.expert);
    modeBadge.textContent = state.expert ? 'Expert' : 'Easy';
    document.body.classList.toggle('pg-expert-on', state.expert);
    if (state.expert) syncManualFromChips();
  }

  function syncManualFromChips() {
    syncingManual = true;
    $('apiGroup').value = joinCsv(state.groups);
    $('apiServer').value = joinCsv(state.servers);
    $('apiPage').value = String(state.page || 1);
    $('apiLimit').value = String(state.limit || pageSize);
    $('apiFacets').checked = !!state.facets;
    $('apiProxy').checked = !!state.proxy;
    syncingManual = false;
  }

  function applyManualToChips() {
    if (syncingManual) return;
    state.groups = parseCsv($('apiGroup').value);
    state.servers = parseCsv($('apiServer').value).map(function (s) { return s.toUpperCase(); });
    state.page = Math.max(1, parseInt($('apiPage').value, 10) || 1);
    state.limit = Math.min(200, Math.max(1, parseInt($('apiLimit').value, 10) || pageSize));
    state.facets = !!$('apiFacets').checked;
    state.proxy = !!$('apiProxy').checked;
    renderFacetChips();
    renderSelected();
  }

  function buildPath() {
    if (state.format === 'playlist') {
      return '/api/v1/playlist.m3u' + (state.proxy ? '?proxy=1' : '');
    }
    var path = state.format === 'm3u' ? '/api/v1/channels.m3u' : '/api/v1/channels';
    var params = new URLSearchParams();
    if (state.page && Number(state.page) !== 1) params.set('page', String(state.page));
    if (state.limit && Number(state.limit) !== pageSize) params.set('limit', String(state.limit));
    if (state.q) params.set('q', state.q);
    var groups = joinCsv(state.groups);
    var servers = joinCsv(state.servers);
    if (groups) params.set('group', groups);
    if (servers) params.set('server', servers);
    if (state.status && state.status !== 'online') params.set('status', state.status);
    if (state.media_type && state.media_type !== 'live') params.set('media_type', state.media_type);
    if (state.sort && state.sort !== 'trending') params.set('sort', state.sort);
    if (state.sort_dir) params.set('sort_dir', state.sort_dir);
    if (state.facets && state.format === 'json') params.set('include', 'facets');
    if (state.format === 'm3u' && !params.has('page')) params.set('page', '1');
    var qs = params.toString();
    return qs ? path + '?' + qs : path;
  }

  function refreshUrl() {
    urlEl.textContent = base + buildPath();
  }

  function readEasyInputs() {
    state.q = ($('apiQ').value || '').trim();
    if (state.expert) applyManualToChips();
  }

  function applyStateToForm() {
    $('apiQ').value = state.q;
    syncChips($('apiFormat'), 'format');
    syncChips($('apiStatus'), 'status');
    syncChips($('apiSort'), 'sort');
    syncChips($('apiSortDir'), 'sort_dir');
    syncChips($('apiMedia'), 'media_type');
    syncManualFromChips();
    syncProxyVisibility();
    syncFilterAvailability();
    renderFacetChips();
    renderSelected();
    refreshUrl();
  }

  function setMeta(text, ok) {
    metaEl.textContent = text;
    metaEl.className = 'pg-meta' + (ok === true ? ' is-ok' : ok === false ? ' is-err' : '');
  }

  function summarizeJson(data) {
    if (!data || typeof data !== 'object') return '';
    var bits = [];
    if (data.total != null) bits.push(data.total + ' total');
    if (data.page != null) bits.push('page ' + data.page + (data.total_pages != null ? '/' + data.total_pages : ''));
    if (Array.isArray(data.channels)) bits.push(data.channels.length + ' rows');
    if (data.has_more) bits.push('has_more');
    if (data.wait_seconds != null) bits.push('wait ' + data.wait_seconds + 's');
    if (data.warning) bits.push('warning');
    return bits.join(' · ');
  }

  function setView(mode) {
    viewMode = mode;
    $('apiViewRaw').classList.toggle('is-on', mode === 'raw');
    $('apiViewCards').classList.toggle('is-on', mode === 'cards');
    outEl.classList.toggle('is-hidden', mode === 'cards');
    previewEl.classList.toggle('is-on', mode === 'cards');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function escapeAttr(s) { return escapeHtml(s).replace(/'/g, '&#39;'); }

  function renderPreview(channels) {
    var list = Array.isArray(channels) ? channels : [];
    previewEl.innerHTML = '';
    if (!list.length) {
      var empty = document.createElement('div');
      empty.className = 'pg-empty';
      empty.textContent = state.format === 'json'
        ? 'No channels yet — pick categories or Run.'
        : 'Switch to Raw for M3U text, or choose JSON.';
      previewEl.appendChild(empty);
      return;
    }
    list.slice(0, 48).forEach(function (ch, i) {
      var tile = document.createElement('article');
      tile.className = 'pg-tile';
      tile.style.animationDelay = Math.min(i, 12) * 0.03 + 's';
      var art = ch.logo || ch.generic_logo || '/static/channel-generic.svg';
      var meta = [ch.group || '', ch.server || ch.country || ''].filter(Boolean).join(' · ');
      tile.innerHTML =
        '<div class="pg-tile-art"><img src="' + escapeAttr(art) + '" alt="" loading="lazy" referrerpolicy="no-referrer"></div>' +
        '<div class="pg-tile-body">' +
          '<div class="pg-tile-name">' + escapeHtml(ch.name || 'Untitled') + '</div>' +
          '<div class="pg-tile-meta">' + escapeHtml(meta || (ch.status || '')) + '</div>' +
        '</div>';
      var img = tile.querySelector('img');
      img.addEventListener('error', function () {
        if (img.dataset.fallback) return;
        img.dataset.fallback = '1';
        img.src = ch.generic_logo || '/static/channel-generic.svg';
      });
      previewEl.appendChild(tile);
    });
  }

  function selectedSet(list) {
    var set = Object.create(null);
    list.forEach(function (v) { set[String(v).toLowerCase()] = true; });
    return set;
  }

  function toggleMulti(field, value) {
    var cur = state[field].slice();
    var key = String(value).toLowerCase();
    var idx = cur.findIndex(function (v) { return String(v).toLowerCase() === key; });
    if (idx >= 0) cur.splice(idx, 1);
    else cur.push(value);
    state[field] = uniqKeepOrder(cur);
    if (state.expert) syncManualFromChips();
    renderFacetChips();
    renderSelected();
    refreshUrl();
  }

  function renderSelected() {
    function paint(elId, values, field) {
      var el = $(elId);
      if (!values.length) {
        el.innerHTML = '<span class="pg-selected-empty">None selected — showing all</span>';
        return;
      }
      el.innerHTML = values.map(function (v) {
        return '<button type="button" class="chip is-on" data-value="' + escapeAttr(v) + '">' +
          escapeHtml(v) + ' ×</button>';
      }).join('');
      Array.prototype.forEach.call(el.querySelectorAll('.chip'), function (btn) {
        btn.addEventListener('click', function () {
          toggleMulti(field, btn.dataset.value);
        });
      });
    }
    paint('apiSelectedGroups', state.groups, 'groups');
    paint('apiSelectedServers', state.servers, 'servers');
  }

  function ingestFacets(facets) {
    if (!facets) return;
    var groups = facets.groups || [];
    var servers = facets.countries || facets.servers || [];
    var gOut = [];
    var gSeen = Object.create(null);
    groups.forEach(function (g) {
      var name = typeof g === 'string' ? g : (g.name || g.group || '');
      if (!name) return;
      var key = name.toLowerCase();
      if (gSeen[key]) return;
      gSeen[key] = true;
      gOut.push({
        value: name,
        count: typeof g === 'object' && g.count != null ? g.count : null,
      });
    });
    var sOut = [];
    var sSeen = Object.create(null);
    servers.forEach(function (c) {
      var code = typeof c === 'string' ? c : (c.code || c.country || c.server || '');
      if (!code) return;
      code = String(code).toUpperCase();
      if (sSeen[code]) return;
      sSeen[code] = true;
      sOut.push({
        value: code,
        name: typeof c === 'object' && c.name ? c.name : code,
        count: typeof c === 'object' && c.count != null ? c.count : null,
      });
    });
    catalog.groups = gOut;
    catalog.servers = sOut;

    var gList = $('apiGroupList');
    var sList = $('apiServerList');
    gList.innerHTML = '';
    sList.innerHTML = '';
    gOut.forEach(function (g) {
      var opt = document.createElement('option');
      opt.value = g.value;
      gList.appendChild(opt);
    });
    sOut.forEach(function (s) {
      var opt = document.createElement('option');
      opt.value = s.value;
      sList.appendChild(opt);
    });
    renderFacetChips();
  }

  function renderFacetChips() {
    var gSel = selectedSet(state.groups);
    var sSel = selectedSet(state.servers);
    var gBox = $('apiFacetGroupChips');
    var sBox = $('apiFacetServerChips');
    var gf = groupFilter.trim().toLowerCase();
    var sf = serverFilter.trim().toLowerCase();

    gBox.innerHTML = '';
    var gShown = 0;
    catalog.groups.forEach(function (g) {
      if (gf && g.value.toLowerCase().indexOf(gf) < 0) return;
      gShown += 1;
      if (gShown > 80) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip' + (gSel[g.value.toLowerCase()] ? ' is-on' : '');
      btn.textContent = g.count != null ? g.value + ' (' + g.count + ')' : g.value;
      btn.title = g.value;
      btn.addEventListener('click', function () { toggleMulti('groups', g.value); });
      gBox.appendChild(btn);
    });
    if (!catalog.groups.length) {
      gBox.innerHTML = '<span class="pg-loading">No categories yet — retry refresh in Expert mode</span>';
    } else if (!gShown) {
      gBox.innerHTML = '<span class="pg-loading">No categories match that filter</span>';
    }

    sBox.innerHTML = '';
    var sShown = 0;
    catalog.servers.forEach(function (s) {
      var hay = (s.value + ' ' + (s.name || '')).toLowerCase();
      if (sf && hay.indexOf(sf) < 0) return;
      sShown += 1;
      if (sShown > 80) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip' + (sSel[s.value.toLowerCase()] ? ' is-on' : '');
      btn.textContent = s.count != null
        ? s.value + ' · ' + s.name + ' (' + s.count + ')'
        : s.value + ' · ' + s.name;
      btn.title = s.value;
      btn.addEventListener('click', function () { toggleMulti('servers', s.value); });
      sBox.appendChild(btn);
    });
    if (!catalog.servers.length) {
      sBox.innerHTML = '<span class="pg-loading">No servers yet — retry refresh in Expert mode</span>';
    } else if (!sShown) {
      sBox.innerHTML = '<span class="pg-loading">No servers match that filter</span>';
    }
    syncFilterAvailability();
  }

  async function runQuery() {
    readEasyInputs();
    refreshUrl();
    var now = Date.now();
    if (now < busyUntil) {
      setMeta('Cooling down — wait ' + Math.ceil((busyUntil - now) / 1000) + 's (rate limit)', false);
      return;
    }
    var url = base + buildPath();
    runBtn.disabled = true;
    setMeta('Fetching…');
    outEl.textContent = '// loading';
    var t0 = performance.now();
    try {
      var res = await fetch(url, {
        headers: {
          Accept: state.format === 'json'
            ? 'application/json'
            : 'application/vnd.apple.mpegurl, text/plain, */*',
        },
      });
      var ms = Math.round(performance.now() - t0);
      var retry = Number(res.headers.get('Retry-After') || 0);
      if (res.status === 429) {
        busyUntil = Date.now() + Math.max(retry, waitHint) * 1000;
        setMeta('HTTP 429 — too fast. Retry in ' + Math.max(retry, waitHint) + 's', false);
        outEl.textContent = await res.text();
        renderPreview([]);
        setView('raw');
        return;
      }
      var text = await res.text();
      var pretty = text;
      var summary = 'HTTP ' + res.status + ' · ' + ms + 'ms';
      if (state.format === 'json') {
        try {
          var data = JSON.parse(text);
          pretty = JSON.stringify(data, null, 2);
          var extra = summarizeJson(data);
          if (extra) summary += ' · ' + extra;
          if (data.facets) ingestFacets(data.facets);
          renderPreview(data.channels || []);
          setView(viewMode === 'raw' ? 'raw' : 'cards');
          if (data.wait_seconds) busyUntil = Date.now() + Number(data.wait_seconds) * 1000;
        } catch (e) {
          summary += ' · (non-JSON body)';
          renderPreview([]);
          setView('raw');
        }
      } else {
        summary += ' · ' + text.split(/\r?\n/).filter(Boolean).length + ' lines';
        renderPreview([]);
        setView('raw');
      }
      outEl.textContent = pretty;
      setMeta(summary, res.ok);
    } catch (err) {
      outEl.textContent = String(err && err.message ? err.message : err);
      setMeta('Request failed', false);
      renderPreview([]);
      setView('raw');
    } finally {
      runBtn.disabled = false;
    }
  }

  async function loadFacets(opts) {
    opts = opts || {};
    setMeta(opts.silent ? metaEl.textContent : 'Loading categories & servers…');
    try {
      var res = await fetch(base + '/api/v1/channels?include=facets&limit=1', {
        headers: { Accept: 'application/json' },
      });
      if (res.status === 429) {
        var retry = Number(res.headers.get('Retry-After') || waitHint);
        busyUntil = Date.now() + retry * 1000;
        setMeta('HTTP 429 loading catalog — wait ' + retry + 's', false);
        return false;
      }
      var data = await res.json();
      ingestFacets(data.facets || {});
      if (data.wait_seconds) busyUntil = Date.now() + Number(data.wait_seconds) * 1000;
      setMeta(
        catalog.groups.length
          ? 'Ready — tap categories or servers, then Run'
          : 'Catalog loaded, but no categories yet',
        true
      );
      return true;
    } catch (err) {
      setMeta('Could not load categories — open Expert mode to retry', false);
      $('apiFacetGroupChips').innerHTML = '<span class="pg-loading">Failed to load</span>';
      $('apiFacetServerChips').innerHTML = '<span class="pg-loading">Failed to load</span>';
      return false;
    }
  }

  mountChips($('apiFormat'), [
    { value: 'json', label: 'JSON list' },
    { value: 'm3u', label: 'M3U page' },
    { value: 'playlist', label: 'Full playlist' },
  ], 'format');
  mountChips($('apiStatus'), [
    { value: 'online', label: 'online' },
    { value: 'offline', label: 'offline' },
    { value: 'unchecked', label: 'unchecked' },
    { value: 'all', label: 'all' },
  ], 'status');
  mountChips($('apiSort'), [
    { value: 'trending', label: 'trending' },
    { value: 'name', label: 'name' },
    { value: 'scanned', label: 'recently scanned' },
  ], 'sort');
  mountChips($('apiSortDir'), [
    { value: '', label: 'default' },
    { value: 'asc', label: 'asc' },
    { value: 'desc', label: 'desc' },
  ], 'sort_dir', true);
  mountChips($('apiMedia'), [
    { value: 'live', label: 'live' },
    { value: 'vod', label: 'vod' },
  ], 'media_type');

  var presets = [
    { label: 'Trending News', apply: function () {
      Object.assign(state, { format: 'json', status: 'online', sort: 'trending', sort_dir: '', media_type: 'live', q: '', groups: ['News'], servers: [], page: 1, limit: 10, facets: false, proxy: false });
    }},
    { label: 'US Sports', apply: function () {
      Object.assign(state, { format: 'json', status: 'online', sort: 'trending', sort_dir: '', media_type: 'live', q: '', groups: ['Sports'], servers: ['US'], page: 1, limit: 15, facets: false, proxy: false });
    }},
    { label: 'News + Sports', apply: function () {
      Object.assign(state, { format: 'json', status: 'online', sort: 'trending', sort_dir: '', media_type: 'live', q: '', groups: ['News', 'Sports'], servers: [], page: 1, limit: 20, facets: false, proxy: false });
    }},
    { label: 'Search BBC', apply: function () {
      Object.assign(state, { format: 'json', status: 'online', sort: 'name', sort_dir: 'asc', media_type: 'live', q: 'BBC', groups: [], servers: [], page: 1, limit: 10, facets: false, proxy: false });
    }},
    { label: 'Offline News', apply: function () {
      Object.assign(state, { format: 'json', status: 'offline', sort: 'scanned', sort_dir: '', media_type: 'live', q: '', groups: ['News'], servers: [], page: 1, limit: 20, facets: false, proxy: false });
    }},
    { label: 'Recently scanned', apply: function () {
      Object.assign(state, { format: 'json', status: 'online', sort: 'scanned', sort_dir: 'desc', media_type: 'live', q: '', groups: [], servers: [], page: 1, limit: 24, facets: false, proxy: false });
    }},
    { label: 'News M3U', apply: function () {
      Object.assign(state, { format: 'm3u', status: 'online', sort: 'trending', sort_dir: '', media_type: 'live', q: '', groups: ['News'], servers: [], page: 1, limit: 50, facets: false, proxy: false });
    }},
    { label: 'Full playlist', apply: function () {
      Object.assign(state, { format: 'playlist', status: 'online', sort: 'trending', sort_dir: '', media_type: 'live', q: '', groups: [], servers: [], page: 1, limit: pageSize, facets: false, proxy: false });
    }},
  ];

  var presetBox = $('apiPresets');
  presets.forEach(function (p) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip chip-preset';
    btn.textContent = p.label;
    btn.addEventListener('click', function () {
      p.apply();
      applyStateToForm();
    });
    presetBox.appendChild(btn);
  });

  $('apiQ').addEventListener('input', function () {
    state.q = ($('apiQ').value || '').trim();
    refreshUrl();
  });
  $('apiQ').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); runQuery(); }
  });

  ['apiGroup', 'apiServer', 'apiPage', 'apiLimit'].forEach(function (id) {
    $(id).addEventListener('input', function () {
      if (!state.expert) return;
      applyManualToChips();
      refreshUrl();
    });
    $(id).addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); runQuery(); }
    });
  });
  $('apiFacets').addEventListener('change', function () {
    state.facets = !!$('apiFacets').checked;
    refreshUrl();
  });
  $('apiProxy').addEventListener('change', function () {
    state.proxy = !!$('apiProxy').checked;
    refreshUrl();
  });

  $('apiGroupFilter').addEventListener('input', function () {
    groupFilter = $('apiGroupFilter').value || '';
    renderFacetChips();
  });
  $('apiServerFilter').addEventListener('input', function () {
    serverFilter = $('apiServerFilter').value || '';
    renderFacetChips();
  });
  $('apiClearGroups').addEventListener('click', function () {
    state.groups = [];
    if (state.expert) syncManualFromChips();
    renderFacetChips();
    renderSelected();
    refreshUrl();
  });
  $('apiClearServers').addEventListener('click', function () {
    state.servers = [];
    if (state.expert) syncManualFromChips();
    renderFacetChips();
    renderSelected();
    refreshUrl();
  });

  $('apiExpert').addEventListener('change', function () {
    setExpert($('apiExpert').checked);
  });

  $('apiCopy').addEventListener('click', async function () {
    readEasyInputs(); refreshUrl();
    try {
      await navigator.clipboard.writeText(urlEl.textContent);
      setMeta('URL copied', true);
    } catch (e) {
      setMeta('Copy failed — select the URL manually', false);
    }
  });
  $('apiOpen').addEventListener('click', function () {
    readEasyInputs(); refreshUrl();
    window.open(urlEl.textContent, '_blank', 'noopener');
  });
  runBtn.addEventListener('click', runQuery);
  $('apiLoadFacets').addEventListener('click', function () { loadFacets(); });
  $('apiViewRaw').addEventListener('click', function () { setView('raw'); });
  $('apiViewCards').addEventListener('click', function () { setView('cards'); });

  setExpert(false);
  applyStateToForm();
  setView('cards');
  renderPreview([]);
  loadFacets({ silent: true });
})();
