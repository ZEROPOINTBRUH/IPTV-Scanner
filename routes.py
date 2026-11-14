"""All Flask route handlers.

Imported for side-effects: importing this module attaches every `@app.route`
handler to the singleton :data:`app_factory.app`.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.parse
from email.utils import format_datetime
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

import requests
import yt_dlp
from flask import (
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
)

import state
from app_factory import app
from features.storage.channels_io import get_valid_channels
from config import (
    HEADERS,
    IPTV_ADMIN_PASSWORD,
    IPTV_ADMIN_PURGE_COOLDOWN_SEC,
    IPTV_PLAYLIST_SECRET,
    IPTV_PROXY_CONNECT_TIMEOUT,
    IPTV_PROXY_MAX_CONCURRENT,
    IPTV_SITE_NAME,
    IPTV_WAITRESS_THREADS,
    STREAM_HEADERS,
    SWEEP_INTERVAL_SEC,
)
from features.public_api import (
    check_admin_purge_rate_limit,
    check_channels_rate_limit,
    check_proxy_rate_limit,
    check_report_rate_limit,
    client_ip,
    client_ip,
)
from features.storage.geo import resolve_country_code
from features.icons.icons import download_channel_icon
from features.ingest.ingest import infer_country, resolve_channel_icon_urls
from features.seo.seo import (
    SEO_RESERVED_SLUGS,
    SEO_SLUG_RE,
    build_proxied_live_m3u,
    seo_abs_url,
    seo_json_ld_broadcast,
    seo_meta_for_channel,
    seo_og_image_for_channel,
    seo_public_base_url,
    seo_refresh_slug_index,
    seo_slug_snapshot,
)


# --- Shared helpers: concurrency slots, request payloads ----------------------

class _Slot:
    """One held permit of a BoundedSemaphore; ``release`` is idempotent."""

    __slots__ = ("_sem", "_lock", "_held")

    def __init__(self, sem: threading.BoundedSemaphore) -> None:
        self._sem = sem
        self._lock = threading.Lock()
        self._held = True

    @classmethod
    def try_acquire(cls, sem: threading.BoundedSemaphore):
        return cls(sem) if sem.acquire(blocking=False) else None

    def release(self) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
        self._sem.release()


# Cap simultaneous upstream pulls through /proxy/stream (IPTV_PROXY_MAX_CONCURRENT).
_PROXY_SLOTS = threading.BoundedSemaphore(IPTV_PROXY_MAX_CONCURRENT)
# Each SSE client pins a server thread, so keep half the pool free for normal requests.
_SSE_SLOTS = threading.BoundedSemaphore(max(2, IPTV_WAITRESS_THREADS // 2))
SSE_MAX_LIFETIME_SEC = 300  # EventSource reconnects on its own after the stream ends
SSE_RETRY_MS = 5000
PROXY_RETRY_AFTER_SEC = 3


def _busy_response(message: str, retry_after: int = PROXY_RETRY_AFTER_SEC):
    resp = jsonify({'ok': False, 'error': message, 'retry_after': retry_after})
    resp.status_code = 503
    resp.headers['Retry-After'] = str(retry_after)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _request_payload() -> dict:
    """JSON body (or form / query fallback) as a plain dict."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    for key in ('url', 'event', 'session_id', 'load_ms'):
        if payload.get(key) is None:
            val = request.form.get(key) or request.args.get(key)
            if val is not None:
                payload[key] = val
    return payload


# --- SEO endpoints ------------------------------------------------------------

@app.route("/robots.txt")
def seo_robots_txt():
    base = seo_public_base_url()
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /streams.json\n"
        "Allow: /api\n"
        "Disallow: /proxy/\n"
        "Disallow: /export/\n"
        "Disallow: /admin/\n"
        f"\nSitemap: {base}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml")
def seo_sitemap_xml():
    """Sitemap index pointing at live + videos sitemaps."""
    base = seo_public_base_url()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    body = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        "<sitemap>",
        f"  <loc>{xml_escape(base + '/sitemap-live.xml')}</loc>",
        f"  <lastmod>{now}</lastmod>",
        "</sitemap>",
        "<sitemap>",
        f"  <loc>{xml_escape(base + '/sitemap-videos.xml')}</loc>",
        f"  <lastmod>{now}</lastmod>",
        "</sitemap>",
        "</sitemapindex>",
    ])
    return Response(body, mimetype="application/xml; charset=utf-8")


def _sitemap_urlset(media_type=None):
    seo_refresh_slug_index()
    slug_map, slugs_sorted = seo_slug_snapshot()
    base = seo_public_base_url()
    urls = [base + "/"] if media_type in (None, "live") else []
    for slug in slugs_sorted:
        ch = slug_map.get(slug) or {}
        if (ch.get("group_title") or "") == "Test":
            continue
        mt = (ch.get("media_type") or "unknown").lower()
        if media_type == "live" and mt == "vod":
            continue
        if media_type == "vod" and mt != "vod":
            continue
        if (ch.get("status") or "").lower() != "online":
            continue
        urls.append(f"{base}/{slug}")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    for loc in urls:
        lines.append("<url>")
        lines.append(f"  <loc>{xml_escape(loc)}</loc>")
        lines.append(f"  <lastmod>{now}</lastmod>")
        lines.append("  <changefreq>hourly</changefreq>")
        lines.append(f"  <priority>{'0.9' if loc.rstrip('/').endswith(base.rstrip('/')) or loc == base + '/' else '0.7'}</priority>")
        lines.append("</url>")
    lines.append("</urlset>")
    return Response("\n".join(lines), mimetype="application/xml; charset=utf-8")


@app.route("/sitemap-live.xml")
def seo_sitemap_live():
    return _sitemap_urlset("live")


@app.route("/sitemap-videos.xml")
def seo_sitemap_videos():
    return _sitemap_urlset("vod")


@app.route("/feed.xml")
def seo_rss_feed():
    seo_refresh_slug_index()
    slug_map, slugs_sorted = seo_slug_snapshot()
    base = seo_public_base_url()
    site = xml_escape(IPTV_SITE_NAME)
    link = xml_escape(base + "/")
    now = format_datetime(datetime.datetime.now(datetime.timezone.utc))
    items = []
    for slug in slugs_sorted:
        ch = slug_map[slug]
        meta = seo_meta_for_channel(ch)
        item_link = xml_escape(f"{base}/{slug}")
        title = xml_escape(meta["name"])
        desc = xml_escape(meta["description"])
        items.append(
            f"<item><title>{title}</title><link>{item_link}</link>"
            f"<guid isPermaLink=\"true\">{item_link}</guid>"
            f"<description>{desc}</description><pubDate>{now}</pubDate></item>"
        )
    rss = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
            f'<channel><title>{site} — Live channels</title><link>{link}</link>',
            f"<description>Directory of live streams from {site}</description>",
            f'<atom:link href="{xml_escape(base + "/feed.xml")}" rel="self" type="application/rss+xml"/>',
            f"<lastBuildDate>{now}</lastBuildDate>",
            *items,
            "</channel></rss>",
        ]
    )
    return Response(rss, mimetype="application/rss+xml; charset=utf-8")


@app.route("/atom.xml")
def seo_atom_feed():
    seo_refresh_slug_index()
    slug_map, slugs_sorted = seo_slug_snapshot()
    base = seo_public_base_url()
    site = xml_escape(IPTV_SITE_NAME)
    self_link = xml_escape(base + "/atom.xml")
    index_link = xml_escape(base + "/")
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    updated = format_datetime(now)
    entries = []
    for slug in slugs_sorted:
        ch = slug_map[slug]
        meta = seo_meta_for_channel(ch)
        u = xml_escape(f"{base}/{slug}")
        title = xml_escape(meta["name"])
        summary = xml_escape(meta["description"])
        entries.append(
            f"<entry><title>{title}</title><link href=\"{u}\"/><id>{u}</id>"
            f"<updated>{updated}</updated><summary>{summary}</summary></entry>"
        )
    atom = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<feed xmlns="http://www.w3.org/2005/Atom">',
            f"<title>{site} — Channels</title>",
            f'<link href="{index_link}" rel="alternate"/>',
            f'<link href="{self_link}" rel="self"/>',
            f"<id>{index_link}</id><updated>{updated}</updated>",
            *entries,
            "</feed>",
        ]
    )
    return Response(atom, mimetype="application/atom+xml; charset=utf-8")


# --- Core UI + status ---------------------------------------------------------

@app.route('/')
def index():
    """Render the main TV guide page."""
    return render_template('index.html')


@app.route('/manifest.webmanifest')
def pwa_manifest():
    """Serve the PWA manifest at a root path (many browsers expect this)."""
    try:
        path = os.path.join(app.static_folder, 'manifest.webmanifest')
        if not os.path.exists(path):
            return jsonify({'error': 'manifest missing'}), 404
        return send_file(path, mimetype='application/manifest+json')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/sw.js')
def pwa_service_worker():
    """Serve the service worker from origin root so its scope is '/'."""
    try:
        path = os.path.join(app.static_folder, 'sw.js')
        if not os.path.exists(path):
            return Response("// no sw", mimetype='application/javascript')
        resp = send_file(path, mimetype='application/javascript')
        resp.headers['Cache-Control'] = 'no-cache'
        resp.headers['Service-Worker-Allowed'] = '/'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/variants')
def api_variants():
    """Return HLS variant streams recorded for a given channel URL."""
    try:
        from features.storage.db import get_default_store
        url = (request.args.get('url') or '').strip()
        if not url:
            return jsonify({'variants': []})
        variants = get_default_store().get_variants(url) or []
        cleaned = []
        for v in variants:
            cleaned.append({
                'url': v.get('variant_url') or v.get('url') or '',
                'resolution': v.get('resolution') or v.get('variant_quality') or '',
                'bandwidth': v.get('bandwidth') or v.get('variant_bandwidth') or 0,
                'codecs': v.get('codecs') or '',
                'audio_language': v.get('audio_language') or '',
            })
        return jsonify({'variants': cleaned})
    except Exception as e:
        logging.error(f"variants api failed: {e}")
        return jsonify({'variants': [], 'error': str(e)})


def _enqueue_verify(url: str, reason: str) -> str:
    """Queue a ``verify_channel`` task (thread fallback when no queue is running)."""
    try:
        from features.workers import fw_tasks
        return fw_tasks.enqueue_verify(url, reason)
    except Exception:
        logging.debug('enqueue verify failed', exc_info=True)
        return 'failed'


@app.route('/api/report-alive', methods=['POST'])
@app.route('/api/v1/report-alive', methods=['POST'])
def api_report_alive():
    """Crowd-promote a channel after a client successfully plays it.

    Dead / pending / unknown streams that load for a viewer are marked
    ``online`` immediately so other users see them in the live list right away.
    A background verify then confirms (or demotes) the stream.
    """
    limited = check_report_rate_limit()
    if limited is not None:
        return limited
    try:
        from features.storage.db import get_default_store

        payload = _request_payload()
        url = str(payload.get('url') or '').strip()
        if not url:
            return jsonify({'ok': False, 'error': 'url required'}), 400

        store = get_default_store()
        existing = store.get_channel(url)
        if not existing:
            return jsonify({'ok': False, 'error': 'unknown channel'}), 404

        prev = (existing.get('status') or '').lower()
        promoted = prev != 'online'

        store.update_channel_results([{
            'url': url,
            'status': 'online',
            'fail_reason': None,
            'fail_count': 0,
            'playing_now': existing.get('playing_now') or 'Live',
        }])
        # Crowd-reported plays jump the health queue (unchecked-first).
        try:
            store.mark_priority_check(url, 100)
        except Exception:
            logging.debug('mark_priority_check failed', exc_info=True)

        verify = _enqueue_verify(url, 'alive')

        rev = store.get_revision()
        state.CHANNEL_STATE_REVISION = rev
        return jsonify({
            'ok': True,
            'promoted': promoted,
            'previous_status': prev,
            'status': 'online',
            'priority_check_queued': True,
            'verify': verify,
            'revision': rev,
            'message': (
                'Marked online and queued for a priority health check.'
                if promoted else
                'Already online; re-queued for a priority health check.'
            ),
        })
    except Exception as e:
        logging.error('report-alive failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500


PLAYBACK_EVENTS = {
    # event -> check_priority handed to mark_priority_check
    'no_playback': 90,
    'slow_load': 20,
}


@app.route('/api/report-playback', methods=['POST'])
@app.route('/api/v1/report-playback', methods=['POST'])
def api_report_playback():
    """Client-side playback problem report: ``{url, event, load_ms?}``.

    ``no_playback`` bumps the channel to the front of the health queue and
    queues a verify task; the stream is NOT flipped offline here (only a failed
    verify can do that). ``slow_load`` just raises its check priority a little.
    """
    limited = check_report_rate_limit()
    if limited is not None:
        return limited
    try:
        from features.storage.db import get_default_store

        payload = _request_payload()
        url = str(payload.get('url') or '').strip()
        event = str(payload.get('event') or '').strip().lower()
        if not url:
            return jsonify({'ok': False, 'error': 'url required'}), 400
        if event not in PLAYBACK_EVENTS:
            return jsonify({
                'ok': False,
                'error': 'event must be one of: ' + ', '.join(sorted(PLAYBACK_EVENTS)),
            }), 400
        load_ms = None
        if payload.get('load_ms') is not None:
            try:
                load_ms = max(0, int(float(payload['load_ms'])))
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'load_ms must be a number'}), 400

        store = get_default_store()
        existing = store.get_channel(url)
        if not existing:
            return jsonify({'ok': False, 'error': 'unknown channel'}), 404

        queued = store.mark_priority_check(url, PLAYBACK_EVENTS[event])
        verify = _enqueue_verify(url, event) if event == 'no_playback' else None

        rev = store.get_revision()
        state.CHANNEL_STATE_REVISION = rev
        return jsonify({
            'ok': True,
            'event': event,
            'load_ms': load_ms,
            'status': (existing.get('status') or 'unknown').lower(),
            'priority_check_queued': bool(queued),
            'verify': verify,
            'revision': rev,
        })
    except Exception as e:
        logging.error('report-playback failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/v1/watching', methods=['POST'])
@app.route('/api/watching', methods=['POST'])
def api_watching():
    """Viewer heartbeat: ``{url, session_id?}`` -> current viewers for that channel."""
    limited = check_report_rate_limit()
    if limited is not None:
        return limited
    try:
        from features.storage.db import get_default_store

        payload = _request_payload()
        url = str(payload.get('url') or '').strip()
        if not url:
            return jsonify({'ok': False, 'error': 'url required'}), 400

        store = get_default_store()
        if not store.get_channel(url):
            return jsonify({'ok': False, 'error': 'unknown channel'}), 404

        session_id = re.sub(r'[^A-Za-z0-9._:-]', '', str(payload.get('session_id') or ''))[:128]
        if not session_id:
            # Anonymous client: stable per ip + browser so repeat beats reuse one row.
            fingerprint = f"{client_ip()}|{request.headers.get('User-Agent', '')}"
            session_id = 'anon-' + hashlib.sha1(fingerprint.encode('utf-8', 'replace')).hexdigest()[:24]

        store.heartbeat_viewer(session_id, url)
        return jsonify({
            'ok': True,
            'url': url,
            'session_id': session_id,
            'viewers': store.count_viewers(url),
        })
    except Exception as e:
        logging.error('watching failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/watch', methods=['POST'])
def api_watch():
    """Record a channel play for the trending / popularity chart.

    Uses a time-decayed score so channels rise when watched and sink when
    interest cools. Same URL is rate-limited server-side (~2 minutes).
    """
    try:
        from features.storage.db import get_default_store

        payload = request.get_json(silent=True) or {}
        url = (payload.get('url') or request.form.get('url') or request.args.get('url') or '').strip()
        if not url:
            return jsonify({'ok': False, 'error': 'url required'}), 400

        store = get_default_store()
        result = store.record_watch(url)
        if result is None:
            return jsonify({'ok': False, 'error': 'unknown channel'}), 404
        return jsonify(result)
    except Exception as e:
        logging.error('api/watch failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/status')
def get_status():
    """Return current scanning status and channel count."""
    try:
        from features.storage.db import get_default_store
        store = get_default_store()
        counts = store.status_counts(exclude_test=True)
        total = counts['online']
        online_ct = total
        rev = store.get_revision()
        state.CHANNEL_STATE_REVISION = rev
        return jsonify({
            'total_channels': total,
            'online_channels': online_ct,
            'counts': counts,
            'scanning': state.SCAN_ACTIVE,
            'revision': rev,
            'sweep_interval_sec': SWEEP_INTERVAL_SEC,
            'last_sweep_started_at': state.LAST_SWEEP_STARTED_AT,
            'last_sweep_completed_at': state.LAST_SWEEP_COMPLETED_AT,
            'last_sweep_counts': state.LAST_SWEEP_COUNTS,
            'last_update': time.time(),
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/events')
def sse_channel_events():
    """Server-Sent Events: revision + status counts (small payload, client reconnects).

    Each stream pins one server thread, so the number of concurrent streams is
    capped and every stream ends after ``SSE_MAX_LIFETIME_SEC``; browsers
    (EventSource) simply reconnect, honouring the ``retry:`` hint.
    """
    slot = _Slot.try_acquire(_SSE_SLOTS)
    if slot is None:
        return _busy_response('too many event streams', retry_after=SSE_RETRY_MS // 1000)

    def event_stream():
        last_rev = -1
        started = time.monotonic()
        try:
            yield f"retry: {SSE_RETRY_MS}\n\n"
            while time.monotonic() - started < SSE_MAX_LIFETIME_SEC:
                try:
                    from features.storage.db import get_default_store
                    store = get_default_store()
                    rev = store.get_revision()
                    if rev != last_rev:
                        last_rev = rev
                        state.CHANNEL_STATE_REVISION = rev
                        counts = store.status_counts(exclude_test=True)
                        payload = json.dumps({
                            'revision': rev,
                            'total_channels': counts['online'],
                            'online_channels': counts['online'],
                            'counts': counts,
                            'scanning': state.SCAN_ACTIVE,
                        }, separators=(',', ':'))
                        yield f"data: {payload}\n\n"
                    else:
                        yield ": ping\n\n"
                except Exception as err:
                    yield f"data: {json.dumps({'error': str(err)})}\n\n"
                time.sleep(2)
        finally:
            slot.release()

    resp = Response(
        stream_with_context(event_stream()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
    resp.call_on_close(slot.release)  # covers streams that never start iterating
    return resp


# --- Icons --------------------------------------------------------------------

@app.route('/icons/<filename>')
def serve_icon(filename):
    """Serve cached channel icons."""
    try:
        icon_path = f'static/icons/{filename}'
        if os.path.exists(icon_path):
            mt = mimetypes.guess_type(icon_path)[0] or 'image/png'
            return send_file(icon_path, mimetype=mt)
        else:
            return "Icon not found", 404
    except Exception as e:
        logging.error(f"Error serving icon {filename}: {e}")
        return "Error serving icon", 500


def get_channel_info(channel_name, channel_url):
    """Get current program information for a channel."""
    try:
        # For YouTube channels, try to get video title
        if 'youtube.com' in channel_url or 'youtu.be' in channel_url:
            try:
                if '/live' in channel_url:
                    return f" LIVE - {channel_name}"
                else:
                    return " Live Stream"
            except Exception:
                return " Live Stream"

        # For Twitch channels
        elif 'twitch.tv' in channel_url:
            return " Live Stream"

        # For other M3U8 streams
        elif '.m3u8' in channel_url:
            try:
                response = requests.get(channel_url, timeout=5, headers=HEADERS)
                if response.status_code == 200:
                    content = response.text
                    title_match = re.search(r'#EXT-X-STREAM-TITLE:(.+)', content, re.IGNORECASE)
                    if title_match:
                        return title_match.group(1).strip()
            except Exception:
                pass

        return f" {channel_name}"

    except Exception as e:
        logging.debug(f"Error getting channel info for {channel_name}: {e}")
        return f" {channel_name}"


@app.route('/channel-info/<channel_name>')
def get_channel_info_endpoint(channel_name):
    """API endpoint to get channel information."""
    try:
        channels = get_valid_channels()
        channel = next((ch for ch in channels if ch.get('name') == channel_name), None)

        if channel:
            info = get_channel_info(channel['name'], channel['url'])
            return jsonify({
                'name': channel['name'],
                'playing_now': info,
                'status': channel.get('status', 'unknown')
            })
        else:
            return jsonify({'error': 'Channel not found'}), 404

    except Exception as e:
        logging.error(f"Error getting channel info: {e}")
        return jsonify({'error': str(e)}), 500


# --- Scan controls ------------------------------------------------------------

@app.route('/scan')
def trigger_scan():
    """Enqueue ingest + health on the shared task queue (no second Client)."""
    try:
        from features.workers import fw_tasks
        ok_ingest = fw_tasks.enqueue("ingest_sources")
        ok_health = fw_tasks.enqueue("active_health_batch")
        if not (ok_ingest or ok_health):
            return jsonify({'error': 'task queue not running'}), 503
        return jsonify({'message': 'Ingest queued', 'status': 'scanning'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/sweep-now')
def trigger_sweep_now():
    """Enqueue active + dead health batches on the shared task queue."""
    try:
        from features.workers import fw_tasks
        ok_a = fw_tasks.enqueue("active_health_batch")
        ok_d = fw_tasks.enqueue("dead_revival_batch")
        if not (ok_a or ok_d):
            return jsonify({'error': 'task queue not running'}), 503
        return jsonify({'message': 'Health check queued', 'status': 'sweeping'}), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- Channel listing / export -------------------------------------------------

@app.route('/channels')
def get_channels():
    """Paginated channel list from SQLite (default page size 50) for the web guide.

    Third-party clients should use ``/api/v1/channels`` (slim envelope).
    """
    limited = check_channels_rate_limit()
    if limited is not None:
        return limited
    try:
        from features.storage.db import get_default_store
        store = get_default_store()
        page = max(1, int(request.args.get('page', 1) or 1))
        limit = min(100, max(1, int(request.args.get('limit', 50) or 50)))
        q = (request.args.get('q') or request.args.get('query') or '').strip() or None
        group = (request.args.get('group') or '').strip() or None
        country = (request.args.get('country') or '').strip() or None
        online_only = request.args.get('online_only', '0').lower() in ('1', 'true', 'yes')
        include_test = request.args.get('include_test', '0').lower() in ('1', 'true', 'yes')
        media_type = (request.args.get('media_type') or '').strip() or None
        # Default guide: live + unknown; exclude vod unless asked
        if media_type is None and request.args.get('videos', '0') not in ('1', 'true', 'yes'):
            media_type = 'live'
        if request.args.get('videos', '0').lower() in ('1', 'true', 'yes'):
            media_type = 'vod'
        status = 'online' if online_only or not request.args.get('status') else request.args.get('status')
        status_in = None
        if request.args.get('pending', '0').lower() in ('1', 'true', 'yes'):
            # Pending toggle = dig through unchecked + dead so viewers can revive them.
            status = None
            status_in = ('pending', 'offline', 'error', 'unknown')
            media_type = None
        sort = (request.args.get('sort') or 'trending').strip()
        default_dir = (
            'desc'
            if sort.lower()
            in (
                'trending',
                'popular',
                'trend_score',
                'watch_count',
                'last_checked_at',
                'last_checked',
                'scanned',
                'recently_scanned',
                'last_watched_at',
                'updated_at',
            )
            else 'asc'
        )
        sort_dir = (request.args.get('sort_dir') or default_dir).strip()

        result = store.list_channels(
            page=page,
            limit=limit,
            q=q,
            group=group,
            country=country,
            online_only=online_only,
            include_test=include_test,
            media_type=media_type if media_type not in ('', 'all') else None,
            status=status,
            status_in=status_in,
            sort=sort,
            sort_dir=sort_dir,
        )
        channels = result.get('channels') or []
        for channel in channels:
            icon_url, generic_icon_url = resolve_channel_icon_urls(channel)
            channel['icon_url'] = icon_url
            channel['generic_icon_url'] = generic_icon_url
            # Prefer a real ISO code even when the DB still says GLOBAL
            # (many playlists stuffed the country into group-title).
            resolved = resolve_country_code(channel)
            if resolved:
                channel['country'] = resolved
            else:
                channel['country'] = channel.get('country') or infer_country(channel) or 'GLOBAL'
            # quality_count hint for UI
            try:
                channel['quality_count'] = len(store.get_variants(channel.get('url') or ''))
            except Exception:
                channel['quality_count'] = 0

        state.CHANNEL_STATE_REVISION = result.get('revision') or store.get_revision()
        return jsonify({
            'channels': channels,
            'countries': result.get('countries') or [],
            'groups': result.get('groups') or [],
            'total_channels': result.get('total') or 0,
            'revision': state.CHANNEL_STATE_REVISION,
            'current_page': result.get('page') or page,
            'per_page': limit,
            'has_more': bool(result.get('has_more')),
            'total_pages': result.get('total_pages') or 1,
        })
    except Exception as e:
        logging.error(f"Error loading channels: {e}")
        return jsonify({'channels': [], 'total_channels': 0, 'error': str(e)})


@app.route('/export/IPTV_STREAMS_FILE.json')
def export_streams_file():
    """Full JSON array of online channels for external apps."""
    try:
        from features.storage.db import get_default_store
        channels = get_default_store().get_export_channels()
        return Response(
            json.dumps(channels, indent=2, ensure_ascii=False),
            mimetype='application/json',
            headers={'Content-Disposition': 'inline; filename=IPTV_STREAMS_FILE.json'},
        )
    except Exception as e:
        logging.error("export failed: %s", e)
        return jsonify([])


# --- Image proxy + icon bulk downloader ---------------------------------------

@app.route('/proxy/image')
def proxy_image():
    """Proxy image requests with caching and rate limiting."""
    limited = check_proxy_rate_limit()
    if limited is not None:
        return limited
    try:
        image_url = request.args.get('url')
        if not image_url:
            return "No URL provided", 400

        # Check cache first
        if image_url in state.image_cache:
            cached_data = state.image_cache[image_url]
            if time.time() - cached_data['timestamp'] < 3600:  # Cache for 1 hour
                return Response(
                    cached_data['content'],
                    mimetype=cached_data['mimetype'],
                    headers={
                        'Cache-Control': 'public, max-age=3600',
                        'Access-Control-Allow-Origin': '*'
                    }
                )

        # Rate limiting - clear old cache entries periodically
        if time.time() - state.last_cache_clear > 300:  # Clear cache every 5 minutes
            current_time = time.time()
            state.image_cache = {k: v for k, v in state.image_cache.items()
                                 if current_time - v['timestamp'] < 1800}  # Keep entries < 30 minutes
            state.last_cache_clear = current_time

        response = requests.get(image_url, timeout=5, headers=HEADERS)

        if response.status_code == 200:
            state.image_cache[image_url] = {
                'content': response.content,
                'mimetype': response.headers.get('content-type', 'image/png'),
                'timestamp': time.time()
            }

            return Response(
                response.content,
                mimetype=response.headers.get('content-type', 'image/png'),
                headers={
                    'Cache-Control': 'public, max-age=3600',
                    'Access-Control-Allow-Origin': '*'
                }
            )
        else:
            return f"Failed to fetch image: {response.status_code}", response.status_code

    except Exception as e:
        logging.error(f"Error proxying image: {e}")
        return f"Error: {str(e)}", 500


@app.route('/download-icons')
def download_all_icons():
    """Download icons for all channels."""
    try:
        channels = get_valid_channels()
        downloaded = 0
        failed = 0

        for channel in channels:
            try:
                icon_url = download_channel_icon(channel['name'], channel['url'], channel.get('tvg_logo', ''))
                if icon_url:
                    downloaded += 1
                    logging.debug("Downloaded icon for %s", channel['name'])
                else:
                    failed += 1
                    logging.debug("No icon found for %s", channel['name'])
            except Exception as e:
                failed += 1
                logging.debug("Error downloading icon for %s: %s", channel['name'], e)

        logging.info(
            "[icons]\n[found] %s\n[reworking] %s\n[how many found] %s\n[how many not found] %s",
            downloaded,
            len(channels),
            downloaded,
            failed,
        )

        return jsonify({
            'message': 'Icon download complete',
            'downloaded': downloaded,
            'failed': failed,
            'total': len(channels)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/search')
def search_channels():
    """Search for channels by name."""
    try:
        query = request.args.get('query', '').lower()
        channels = get_valid_channels()
        return jsonify([ch for ch in channels if query in (ch.get('name') or '').lower()])
    except Exception as e:
        logging.error(f"Error searching channels: {e}")
        return jsonify([])


# --- YouTube / Twitch stream URL extraction ----------------------------------

def get_youtube_stream_url(url):
    """Extract actual stream URL from YouTube using yt-dlp for reliable extraction."""
    try:
        logging.debug("youtube extract")

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'format': 'best[height<=720]',  # Limit to 720p for performance
            'noplaylist': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                if info:
                    if info.get('is_live'):
                        logging.debug("youtube extract")
                        formats = info.get('formats', [])
                        if formats:
                            best_format = None
                            for fmt in formats:
                                if fmt.get('vcodec') != 'none' and fmt.get('acodec') != 'none':
                                    if not best_format or fmt.get('height', 0) > best_format.get('height', 0):
                                        best_format = fmt

                            if best_format and best_format.get('url'):
                                stream_url = best_format['url']
                                logging.debug("youtube extract")
                                return stream_url
                    else:
                        formats = info.get('formats', [])
                        if formats:
                            best_format = None
                            for fmt in formats:
                                if fmt.get('vcodec') != 'none' and fmt.get('acodec') != 'none':
                                    if not best_format or fmt.get('height', 0) > best_format.get('height', 0):
                                        best_format = fmt

                            if best_format and best_format.get('url'):
                                stream_url = best_format['url']
                                logging.debug("youtube extract")
                                return stream_url

                    # Fallback to embed URL if no direct stream found
                    video_id = info.get('id')
                    if video_id:
                        embed_url = f"https://www.youtube.com/embed/{video_id}?autoplay=1&rel=0"
                        logging.debug("youtube extract")
                        return embed_url

        except Exception as e:
            logging.debug(f"yt-dlp extraction failed: {e}")
            return extract_youtube_url_basic(url)

    except ImportError:
        logging.error("yt-dlp not installed, falling back to basic extraction")
        return extract_youtube_url_basic(url)
    except Exception as e:
        logging.error(f"Error extracting YouTube URL: {e}")
        logging.error(f"Exception details: {type(e).__name__}: {str(e)}")
        return None


def extract_youtube_url_basic(url):
    """Basic YouTube URL extraction as fallback."""
    try:
        logging.debug("youtube extract")

        video_id = None

        if 'youtube.com/watch?v=' in url:
            video_id = url.split('v=')[1].split('&')[0]
            logging.debug("youtube extract")
        elif 'youtu.be/' in url:
            video_id = url.split('youtu.be/')[1].split('?')[0]
            logging.debug("youtube extract")
        elif 'youtube.com/embed/' in url:
            video_id = url.split('embed/')[1].split('?')[0]
            logging.debug("youtube extract")
        elif '/live' in url:
            logging.debug("youtube extract")
            if '/@' in url:
                channel_handle = url.split('/@')[1].split('/')[0]
                logging.debug("youtube extract")
                return f"https://www.youtube.com/embed/live_stream?channel={channel_handle}"
            elif '/channel/' in url:
                channel_id = url.split('/channel/')[1].split('/')[0]
                logging.debug("youtube extract")
                return f"https://www.youtube.com/embed/live_stream?channel={channel_id}"
            elif '/c/' in url:
                channel_name = url.split('/c/')[1].split('/')[0]
                logging.debug("youtube extract")
                return f"https://www.youtube.com/embed/live_stream?channel={channel_name}"
            elif '/user/' in url:
                username = url.split('/user/')[1].split('/')[0]
                logging.debug("youtube extract")
                return f"https://www.youtube.com/embed/live_stream?channel={username}"
            else:
                logging.debug(f"Unknown live stream format: {url}")
                return None
        else:
            # Try to extract video ID using regex
            patterns = [
                r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
                r'youtube\.com.*[?&]v=([a-zA-Z0-9_-]{11})'
            ]
            for i, pattern in enumerate(patterns):
                match = re.search(pattern, url)
                if match:
                    video_id = match.group(1)
                    logging.debug("youtube extract")
                    break

        if video_id:
            if len(video_id) != 11:
                logging.debug(f"Invalid video ID format: {video_id} (length: {len(video_id)})")
                return None

            logging.debug("youtube extract")

            embed_url = f"https://www.youtube.com/embed/{video_id}?autoplay=1&rel=0"
            logging.debug("youtube extract")
            return embed_url
        else:
            logging.debug(f"Could not extract video ID from URL: {url}")
            logging.warning("URL patterns checked: youtube.com/watch?v=, youtu.be/, youtube.com/embed/, /live, regex patterns")
            return None

    except Exception as e:
        logging.error(f"Error in basic YouTube extraction: {e}")
        return None


def get_twitch_stream_url(url):
    """Extract actual stream URL from Twitch using direct API approach."""
    try:
        if 'twitch.tv/' in url:
            channel = url.split('twitch.tv/')[1].split('/')[0]
        else:
            return None

        if not channel:
            return None

        # Embed URL for iframes — parents must match the browsing host.
        host = (request.host.split(":")[0] if request else "") or "localhost"
        parents = ["localhost", "127.0.0.1"]
        if host and host not in parents:
            parents.insert(0, host)
        parent_q = "&".join(f"parent={p}" for p in parents)
        return f"https://player.twitch.tv/?channel={channel}&{parent_q}&autoplay=true"

    except Exception as e:
        logging.error(f"Error extracting Twitch URL: {e}")
        return None


# --- HLS proxy helpers --------------------------------------------------------

# HLS manifests are small text; rewrite so every segment/variant URL loads via same-origin proxy (CORS + Firefox).
MAX_HLS_PLAYLIST_BYTES = 4 * 1024 * 1024


def _url_looks_like_hls_manifest(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith('.m3u8') or path.endswith('.m3u')


def rewrite_hls_playlist_for_proxy(body: str, resolved_base_url: str) -> str:
    """Rewrite manifest lines so absolute/relative URLs are fetched through /proxy/stream.

    This keeps the browser same-origin even when the upstream playlist is plain
    ``http://`` (avoids mixed-content blocks on an HTTPS site).
    """
    uri_in_tag = re.compile(r'URI=(["\'])([^"\']+)\1')
    lines_out = []

    def proxied(target: str) -> str:
        target = (target or "").strip()
        if not target or target.startswith("/proxy/stream?"):
            return target
        if target.startswith(("http://", "https://")):
            resolved = target
        else:
            resolved = urllib.parse.urljoin(resolved_base_url, target)
        return f"/proxy/stream?url={quote(resolved, safe='')}"

    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            if "URI=" in raw:

                def repl_tag(m):
                    quote_ch, inner = m.group(1), m.group(2)
                    return f"URI={quote_ch}{proxied(inner)}{quote_ch}"

                raw = uri_in_tag.sub(repl_tag, raw)
            lines_out.append(raw)
            continue
        if not stripped:
            lines_out.append(raw)
            continue
        lines_out.append(proxied(stripped))

    return "\n".join(lines_out)


@app.route('/proxy/stream')
def proxy_stream():
    """Proxy YouTube and Twitch streams to work with HTML5 video player.

    At most ``IPTV_PROXY_MAX_CONCURRENT`` upstream pulls run at once; extra
    callers get ``503`` + ``Retry-After``. The slot is held for the whole
    relay and freed as soon as the client disconnects.
    """
    limited = check_proxy_rate_limit()
    if limited is not None:
        return limited
    slot = None
    handed_off = False  # proxy_direct_stream owns the slot once it is called
    try:
        stream_url = request.args.get('url')
        if not stream_url:
            logging.error("No URL provided to proxy/stream endpoint")
            return jsonify({'error': 'No URL provided'}), 400

        slot = _Slot.try_acquire(_PROXY_SLOTS)
        if slot is None:
            return _busy_response('proxy busy, try again shortly')

        logging.debug("Proxy stream request for URL: %s", stream_url)
        logging.debug(
            "URL type check - YouTube: %s, Twitch: %s",
            "youtube.com" in stream_url or "youtu.be" in stream_url,
            "twitch.tv" in stream_url,
        )

        # YouTube handling
        if 'youtube.com' in stream_url or 'youtu.be' in stream_url:
            logging.debug("Processing YouTube URL: %s", stream_url)
            direct_url = get_youtube_stream_url(stream_url)
            if direct_url:
                logging.debug("YouTube extraction successful, redirecting")
                return redirect(direct_url, code=302)
            else:
                logging.debug("YouTube extraction failed for URL: %s", stream_url)
                return jsonify({'error': 'Failed to extract YouTube stream'}), 500

        # Twitch handling
        elif 'twitch.tv' in stream_url:
            logging.debug("Processing Twitch URL: %s", stream_url)
            direct_url = get_twitch_stream_url(stream_url)
            if direct_url:
                logging.debug("Twitch extraction successful, redirecting")
                return redirect(direct_url, code=302)
            else:
                logging.debug("Twitch extraction failed for URL: %s", stream_url)
                return jsonify({'error': 'Failed to extract Twitch stream'}), 500

        # Direct stream for other sources
        else:
            logging.debug("Processing direct stream URL")
            handed_off = True
            return proxy_direct_stream(stream_url, slot)

    except Exception as e:
        logging.error(f"Error proxying stream: {e}")
        logging.error(f"Exception details: {type(e).__name__}: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        if slot is not None and not handed_off:
            slot.release()


def _looks_like_hls_text(text: str) -> bool:
    head = (text or "").lstrip()[:64]
    return head.startswith("#EXTM3U")


def proxy_direct_stream(url, slot=None):
    """Proxy direct streams (progressive passthrough or HLS manifest rewrite + segment relay).

    Upstream fetches intentionally omit ``Upgrade-Insecure-Requests`` so plain
    ``http://`` IPTV origins are not coerced onto HTTPS.

    ``slot`` (a held :class:`_Slot`) is released when the response is finished
    or the client disconnects (the generator is closed), and immediately for
    short playlist / error responses.
    """
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Cache-Control": "no-store",
    }
    streaming = False  # True once the relay generator owns the slot
    try:
        # Prefer treating known playlist URLs as text up-front.
        force_playlist = _url_looks_like_hls_manifest(url)

        upstream = requests.get(
            url,
            headers=STREAM_HEADERS,
            # (connect, read): bound connect time; reads stay patient for live feeds.
            timeout=(IPTV_PROXY_CONNECT_TIMEOUT, 45 if force_playlist else 120),
            stream=not force_playlist,
            allow_redirects=True,
        )
        if upstream.status_code != 200:
            try:
                upstream.close()
            except Exception:
                pass
            return jsonify({"error": f"Upstream HTTP {upstream.status_code}"}), (
                upstream.status_code if upstream.status_code >= 400 else 502
            )

        # Keep relative URL joins on the final upstream location (after redirects),
        # but never invent an https upgrade ourselves.
        resolved_base = upstream.url or url
        ct = (upstream.headers.get("Content-Type") or "").lower()

        if force_playlist:
            body = upstream.content
            text = body.decode("utf-8", errors="replace")
            if _looks_like_hls_text(text) and len(body) <= MAX_HLS_PLAYLIST_BYTES:
                text = rewrite_hls_playlist_for_proxy(text, resolved_base)
                return Response(
                    text,
                    mimetype="application/vnd.apple.mpegurl",
                    headers=cors,
                )
            return Response(
                body,
                content_type=ct or "application/octet-stream",
                headers=cors,
            )

        # Peek first bytes for playlists that omit .m3u8 in the path
        # (common for http://host:port/play/xxxx endpoints).
        peek = b""
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                peek = chunk
                break

        peek_text = peek.decode("utf-8", errors="replace") if peek else ""
        is_playlist = (
            "mpegurl" in ct
            or "application/vnd.apple.mpegurl" in ct
            or "audio/mpegurl" in ct
            or "application/x-mpegurl" in ct
            or _looks_like_hls_text(peek_text)
        )

        if is_playlist and len(peek) <= MAX_HLS_PLAYLIST_BYTES:
            parts = [peek]
            total = len(peek)
            for chunk in upstream.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                parts.append(chunk)
                total += len(chunk)
                if total > MAX_HLS_PLAYLIST_BYTES:
                    break
            body = b"".join(parts)
            upstream.close()
            text = body.decode("utf-8", errors="replace")
            if _looks_like_hls_text(text):
                text = rewrite_hls_playlist_for_proxy(text, resolved_base)
                return Response(
                    text,
                    mimetype="application/vnd.apple.mpegurl",
                    headers=cors,
                )
            return Response(
                body,
                content_type=ct or "application/octet-stream",
                headers=cors,
            )

        def generate():
            # The server closes this generator when the client disconnects, which
            # drops the upstream connection and frees the proxy slot right away.
            try:
                if peek:
                    yield peek
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
                if slot is not None:
                    slot.release()

        resp = Response(
            stream_with_context(generate()),
            content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
            headers=cors,
        )
        if slot is not None:
            resp.call_on_close(slot.release)  # also covers a generator that never started
        streaming = True
        return resp

    except requests.exceptions.ConnectTimeout:
        logging.warning("Proxy connect timeout (%.1fs): %s", IPTV_PROXY_CONNECT_TIMEOUT, url)
        return jsonify({"error": "Upstream connect timeout"}), 504
    except Exception as e:
        logging.error(f"Error proxying direct stream: {e}")
        return jsonify({"error": "Failed to proxy stream"}), 500
    finally:
        if slot is not None and not streaming:
            slot.release()


# Successful purge cooldown per client IP (monotonic deadline).
_purge_cooldown_until: dict[str, float] = {}
_purge_cooldown_lock = threading.Lock()


def _playlist_token_ok() -> bool:
    """Optional shared-secret gate for playlist backup / full M3U (not purge)."""
    if not IPTV_PLAYLIST_SECRET:
        return True
    token = request.args.get("token") or request.headers.get("X-Admin-Token") or ""
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            token = token or str(body.get("token") or "")
    try:
        return secrets.compare_digest(str(token), IPTV_PLAYLIST_SECRET)
    except (TypeError, ValueError):
        return False


def _extract_admin_password(payload: dict) -> str:
    """Password from JSON/form/query/header (never trust loopback alone)."""
    for key in ("password", "token", "admin_password"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    for key in ("password", "token"):
        val = request.args.get(key) or request.form.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    header = (
        request.headers.get("X-Admin-Password")
        or request.headers.get("X-Admin-Token")
        or ""
    ).strip()
    return header


def _purge_password_configured() -> bool:
    return bool(IPTV_ADMIN_PASSWORD)


def _purge_password_ok(supplied: str) -> bool:
    expected = IPTV_ADMIN_PASSWORD
    if not expected or not supplied:
        return False
    # Hash both sides so compare_digest never length-leaks.
    a = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    b = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    return secrets.compare_digest(a, b)


def _purge_cooldown_remaining() -> int:
    ip = client_ip()
    now = time.monotonic()
    with _purge_cooldown_lock:
        until = float(_purge_cooldown_until.get(ip) or 0)
        left = int(round(until - now))
        if left <= 0:
            _purge_cooldown_until.pop(ip, None)
            return 0
        return left


def _mark_purge_cooldown() -> None:
    ip = client_ip()
    with _purge_cooldown_lock:
        _purge_cooldown_until[ip] = time.monotonic() + float(IPTV_ADMIN_PURGE_COOLDOWN_SEC)


@app.route("/admin/backup")
def admin_backup():
    """Download a SQLite backup (optional token via IPTV_PLAYLIST_SECRET)."""
    if not _playlist_token_ok():
        abort(403)
    try:
        from features.storage.db import get_default_store
        path = get_default_store().backup()
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/purge", methods=["POST"])
def admin_purge():
    """Purge the channel catalog and enqueue a fresh ingest + health pass.

    Requires ``IPTV_ADMIN_PASSWORD`` (or ``IPTV_PLAYLIST_SECRET`` fallback) as
    JSON/form ``password`` (aliases: ``token``, header ``X-Admin-Password``).
    Loopback alone is **not** enough — reverse proxies make every client look
    local. Also rate-limited and cooled down after a successful wipe.
    Body ``confirm=purge`` is required.
    """
    limited = check_admin_purge_rate_limit()
    if limited is not None:
        return limited

    cooldown = _purge_cooldown_remaining()
    if cooldown > 0:
        resp = jsonify({
            "ok": False,
            "error": "purge_cooldown",
            "message": f"Purge was just used. Try again in about {cooldown} seconds.",
            "retry_after": cooldown,
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(cooldown)
        return resp

    if not _purge_password_configured():
        return jsonify({
            "ok": False,
            "error": "admin_password_not_configured",
            "message": (
                "HTTP purge is disabled until you set IPTV_ADMIN_PASSWORD "
                "(or IPTV_PLAYLIST_SECRET). Use the startup menu / CLI to wipe locally."
            ),
        }), 503

    payload = request.get_json(silent=True) if request.is_json else {}
    if not isinstance(payload, dict):
        payload = {}

    if not _purge_password_ok(_extract_admin_password(payload)):
        return jsonify({
            "ok": False,
            "error": "invalid_password",
            "message": "Admin password required.",
        }), 403

    confirm = (
        str(payload.get("confirm") or request.args.get("confirm") or request.form.get("confirm") or "")
        .strip()
        .lower()
    )
    if confirm != "purge":
        return jsonify({
            "error": "confirmation required",
            "hint": 'POST JSON {"confirm":"purge","password":"…"}',
        }), 400

    clear_geo = str(
        payload.get("clear_geo") or request.args.get("clear_geo") or ""
    ).lower() in ("1", "true", "yes")

    try:
        from features.storage.db import get_default_store
        from features.workers import fw_tasks
        from config import FILES

        store = get_default_store()
        before = store.count_channels(exclude_test=False)
        summary = store.purge_catalog(keep_host_geo=not clear_geo)

        for key in ("streams", "dead", "invalid", "master"):
            path = FILES.get(key)
            if not path:
                continue
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("[]\n")
            except OSError:
                pass
        try:
            with open(os.path.join("jsons", "SEO_SLUG_MANIFEST.json"), "w", encoding="utf-8") as fh:
                fh.write('{"slug_order":[],"slugs":{}}\n')
        except OSError:
            pass
        try:
            from features.seo import seo as seo_mod
            seo_mod._seo_slug_to_channel = {}
            seo_mod._seo_slugs_sorted = []
            seo_mod._seo_slug_revision = -1
        except Exception:
            pass

        fw_tasks.REQUEST_FRESH_INGEST = True
        ok_ingest = fw_tasks.enqueue("ingest_sources")
        ok_health = fw_tasks.enqueue("active_health_batch")
        state.SCAN_ACTIVE = True
        _mark_purge_cooldown()
        return jsonify({
            "ok": True,
            "purged_channels": summary.get("channels", before),
            "revision": summary.get("revision"),
            "ingest_queued": bool(ok_ingest),
            "health_queued": bool(ok_health),
            "cooldown_seconds": IPTV_ADMIN_PURGE_COOLDOWN_SEC,
            "message": "Catalog purged; fresh ingest queued.",
        })
    except Exception as e:
        logging.error("admin purge failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# --- Jellyfin / VLC M3U tuner endpoint ----------------------------------------

@app.route("/jellyfin/live.m3u")
@app.route("/iptv/live.m3u")
@app.route("/live-proxy.m3u")
def jellyfin_style_proxied_m3u():
    """Expose current lineup as an M3U whose stream URLs hit /proxy/stream (Jellyfin Live TV tuner, VLC, etc.)."""
    if IPTV_PLAYLIST_SECRET and request.args.get("token") != IPTV_PLAYLIST_SECRET:
        abort(403)
    online_only = request.args.get("online", "").lower() in ("1", "true", "yes")
    include_test = request.args.get("include_test", "").lower() in ("1", "true", "yes")
    country = (request.args.get("country") or "").strip() or None
    group = (request.args.get("group") or "").strip() or None
    media_type = (request.args.get("media_type") or "live").strip() or "live"
    if request.args.get("videos", "").lower() in ("1", "true", "yes"):
        media_type = "vod"
    base = seo_public_base_url().rstrip("/")
    body = build_proxied_live_m3u(
        base,
        online_only=online_only or True,
        country=country,
        group=group,
        include_test=include_test,
        media_type=media_type,
    )
    return Response(
        body,
        mimetype="audio/x-mpegurl",
        headers={
            "Content-Disposition": 'inline; filename="iptv-scanner-proxy.m3u"',
            "Cache-Control": "no-store, max-age=0",
        },
    )


# --- SEO catch-all landing page (MUST stay last) -----------------------------

@app.route("/<slug>")
def seo_channel_page(slug):
    """SEO landing + player for /<slug>-style URLs (e.g. /fox-news)."""
    if not SEO_SLUG_RE.match(slug):
        abort(404)
    if slug.lower() in SEO_RESERVED_SLUGS:
        abort(404)
    seo_refresh_slug_index()
    slug_map, _ = seo_slug_snapshot()
    ch = slug_map.get(slug)
    canonical = seo_abs_url(slug)

    if not ch or not (ch.get("url") or "").strip():
        return (
            render_template(
                "seo_missing.html",
                site_name=IPTV_SITE_NAME,
                slug=slug,
                canonical=seo_abs_url(""),
                home_url=seo_public_base_url().rstrip("/"),
                error_title="Stream not found",
                error_body="That channel slug is not in the current lineup, or the listing has no stream URL yet. Try scanning again from the guide or browse the home page.",
            ),
            404,
        )

    raw_url = ch.get("url", "").strip()
    meta = seo_meta_for_channel(ch)
    og_img = seo_og_image_for_channel(ch)
    json_ld = seo_json_ld_broadcast(ch, canonical)

    youtube = ("youtube.com" in raw_url) or ("youtu.be" in raw_url)
    twitch = "twitch.tv" in raw_url
    embed_player = youtube or twitch
    path_lc = raw_url.split("?")[0].lower()
    progressive = path_lc.endswith((".mp4", ".webm", ".ogv"))

    proxied_play = "/proxy/stream?url=" + quote(raw_url, safe="")

    public_root = seo_public_base_url().rstrip("/")

    return render_template(
        "seo_channel.html",
        site_name=IPTV_SITE_NAME,
        channel_name=meta["name"],
        description=meta["description"],
        group_title=meta.get("group") or "",
        country=meta.get("country") or "",
        canonical=canonical,
        og_image=og_img or "",
        json_ld=json_ld,
        slug=slug,
        stream_url_encoded=proxied_play,
        embed_player=embed_player,
        progressive=progressive,
        raw_stream_url=raw_url,
        home_url=public_root,
    )
