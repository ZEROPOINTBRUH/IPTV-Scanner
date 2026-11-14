"""Universal public channels API (browsers, VR worlds, game engines, IPTV apps).

One DB-backed builder (:func:`build_channels_page`) serves everything:

* ``GET /api/v1/channels``        JSON, slim envelope
* ``GET /api/v1/channels.m3u``    same query / paging, M3U body
* ``GET /api/v1/playlist.m3u``    full online lineup (Jellyfin / Plex / Emby / VLC)

The older ``/streams*.json|.m3u``, ``/unchecked/*`` and ``/api/v1/streams``
URLs stay alive as thin aliases of that same builder and carry a
``Deprecation`` header plus a ``Link: <...>; rel="successor-version"``.

No API key. Pages are small and rate-limited lightly so worlds/mods cannot
hammer the server. Support links live in headers and the ``/api/v1`` index,
not in every page body.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlencode

from flask import Response, render_template, request, send_file

from config import (
    API_PUBLIC_BURST,
    API_PUBLIC_MAX_LIMIT,
    API_PUBLIC_MIN_INTERVAL_SEC,
    API_PUBLIC_PAGE_SIZE,
    IPTV_ADMIN_PURGE_BURST,
    IPTV_ADMIN_PURGE_MIN_INTERVAL_SEC,
    IPTV_CHANNELS_BURST,
    IPTV_CHANNELS_MIN_INTERVAL_SEC,
    IPTV_PLAYLIST_SECRET,
    IPTV_PROXY_BURST,
    IPTV_PROXY_MIN_INTERVAL_SEC,
    IPTV_REPORT_BURST,
    IPTV_REPORT_MIN_INTERVAL_SEC,
    IPTV_SITE_NAME,
    IPTV_TRUST_PROXY,
    SELF_HOST_REPO,
    SUPPORT_CASHAPP,
    SUPPORT_KOFI,
    SUPPORT_MESSAGE,
    SUPPORT_PAYPAL,
)
from features.public_base import resolve_public_base_url
from features.ingest.ingest import GENERIC_ICON_URL, resolve_channel_icon_urls, infer_country
from features.storage.geo import resolve_country_code


log = logging.getLogger("public_api")

API_ID = "iptv-scanner-public/v1"
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

_rate_lock = threading.Lock()
# "scope:ip" -> {"tokens": float, "updated": float}
_rate_buckets: dict[str, dict[str, float]] = {}


def support_block() -> dict:
    """Kind support reminder (discovery index, 429 bodies; headers elsewhere)."""
    return {
        "please_consider_supporting": SUPPORT_MESSAGE,
        "kofi": SUPPORT_KOFI,
        "paypal": SUPPORT_PAYPAL,
        "cashapp": SUPPORT_CASHAPP,
        "self_host": SELF_HOST_REPO,
        "self_host_tree": f"{SELF_HOST_REPO}/tree/main",
    }


def platform_guides() -> list[dict[str, Any]]:
    """Distinct integration guides for /api docs (clones collapsed)."""
    wait = max(1, int(round(API_PUBLIC_MIN_INTERVAL_SEC)) or 2)
    vrc_wait = max(wait, 5)
    page = API_PUBLIC_PAGE_SIZE
    return [
        {
            "id": "media-servers",
            "name": "Jellyfin / Plex / Emby",
            "tagline": "Live TV via one M3U playlist",
            "official": [
                {"label": "Jellyfin Live TV", "url": "https://jellyfin.org/docs/general/server/live-tv/"},
                {"label": "Emby Live TV", "url": "https://emby.media/support/articles/Live-TV.html"},
                {"label": "Plex Live TV & DVR", "url": "https://support.plex.tv/articles/live-tv-dvr-overview/"},
            ],
            "compliance": [
                "Use only publicly listed streams you are allowed to redistribute on your server.",
                "Point the tuner at our M3U — do not scrape private admin APIs.",
                "Refresh on a slow schedule (hours), never every few seconds.",
                "Plex expects an HDHomeRun-compatible tuner: put Threadfin / xTeVe in front of the M3U.",
            ],
            "steps": [
                "Jellyfin / Emby: Dashboard → Live TV → Add tuner → M3U Tuner.",
                "Playlist URL: {BASE}/api/v1/playlist.m3u (add ?proxy=1 to route through this server).",
                "Plex: set the same URL as the source in Threadfin / xTeVe and expose it as HDHomeRun.",
                "Optional: set IPTV_XMLTV_URL on the server if you have an XMLTV guide source.",
            ],
            "snippet": "Jellyfin / Emby → Live TV → M3U Tuner\nURL: {BASE}/api/v1/playlist.m3u",
        },
        {
            "id": "vlc",
            "name": "VLC",
            "tagline": "Open Network Stream / playlist",
            "official": [
                {"label": "VLC", "url": "https://www.videolan.org/vlc/"},
            ],
            "compliance": [
                "Local playback of public streams only.",
            ],
            "steps": [
                "Media → Open Network Stream → {BASE}/api/v1/playlist.m3u (or one channel url).",
                "Or save the M3U locally and use Media → Open File.",
            ],
            "snippet": "VLC → Open Network Stream → {BASE}/api/v1/playlist.m3u",
        },
        {
            "id": "http",
            "name": "HTTP / REST",
            "tagline": "Browsers, Unity, Unreal, Godot, bots, scripts, IoT",
            "official": [
                {"label": "OpenAPI spec", "url": "/openapi.yaml"},
                {"label": "Fetch API", "url": "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API"},
            ],
            "compliance": [
                "GET only. No API key. Identify your client with a clear User-Agent.",
                f"One page (default {page} rows) per call; sleep wait_seconds between pages.",
                "Back off on HTTP 429 using Retry-After.",
                "CORS is open (*); still do not hammer from many tabs or clients — cache locally.",
            ],
            "steps": [
                "GET /api/v1 for discovery.",
                "GET /api/v1/channels?page=1 (filters: status, server, group, q, media_type, sort).",
                "Follow next_url until it is null; wait wait_seconds between calls.",
                "Play url directly, or proxy_url when you need same-origin HLS through our proxy.",
                "If a stream plays or fails, POST /api/v1/report-playback so health checks prioritise it.",
            ],
            "snippet": (
                "curl -sS \"$BASE/api/v1/channels?status=offline&group=News&q=BBC&limit=20\"\n"
                "curl -sS \"$BASE/api/v1/channels?group=News&sort=trending&limit=20\" | jq '.channels[].name'\n"
                "curl -sS \"$BASE/api/v1/channels?include=facets&limit=1\" | jq '.facets.groups,.facets.countries'\n"
                "const r = await fetch(`${base}/api/v1/channels?page=1`); const {channels, next_url} = await r.json();"
            ),
        },
        {
            "id": "vr-string-loader",
            "name": "VR / game string loaders",
            "tagline": "VRChat Udon, Resonite, ChilloutVR and similar",
            "official": [
                {"label": "VRChat String Loading", "url": "https://creators.vrchat.com/worlds/udon/string-loading/"},
                {"label": "VRCJson", "url": "https://creators.vrchat.com/worlds/udon/data-containers/vrcjson/"},
                {"label": "Resonite GET String", "url": "https://wiki.resonite.com/ProtoFlux:GETString"},
            ],
            "compliance": [
                "Use only the platform's official network APIs; do not bypass its networking or consent prompts.",
                f"VRChat allows about one string download every 5 seconds — wait at least {vrc_wait}s between our pages.",
                "Hosts outside the platform's URL allowlist need the user to enable untrusted URLs / grant host access.",
                f"Fetch one page at a time (≤{page} channels); never loop next_url tightly.",
                "Play media only through the platform's approved video player components.",
            ],
            "steps": [
                "Load {BASE}/api/v1/channels (VRCStringDownloader.LoadUrl / Resonite GET String).",
                "Parse the JSON string (VRCJson.TryDeserializeFromJson / ProtoFlux JSON).",
                "Read channels[]: name, url, logo, generic_logo, group, country, proxy_url.",
                "For more, wait wait_seconds then load next_url.",
            ],
            "snippet": "VRCStringDownloader.LoadUrl(channelsUrl, (IUdonEventReceiver)this);\n// OnStringLoadSuccess → VRCJson.TryDeserializeFromJson(result.Result, out token)",
        },
    ]


def supported_platforms() -> list[str]:
    return [g["name"] for g in platform_guides()]


def integrations_map() -> dict[str, str]:
    return {g["id"]: _abs_url(f"/api#integrate-{g['id']}") for g in platform_guides()}


def response_styles() -> dict[str, Any]:
    wait = max(1, int(round(API_PUBLIC_MIN_INTERVAL_SEC)) or 2)
    return {
        "channels_json": {
            "method": "GET",
            "content_type": "application/json",
            "endpoint": "/api/v1/channels?page=1&status=online&sort=trending",
            "params": [
                "page", "limit", "status", "server", "country", "group", "q",
                "media_type", "sort", "sort_dir", "include=facets",
            ],
        },
        "channels_m3u": {
            "description": "Same query and paging as JSON, M3U body.",
            "endpoint": "/api/v1/channels.m3u?page=1",
        },
        "playlist_m3u": {
            "description": "Full online lineup as one M3U (media servers, VLC).",
            "endpoint": "/api/v1/playlist.m3u",
            "options": ["server", "group", "media_type", "proxy=1"],
        },
        "reports": {
            "playback": "POST /api/v1/report-playback {url, event: no_playback|slow_load, load_ms?}",
            "alive": "POST /api/report-alive {url}",
            "watching": "POST /api/v1/watching {url, session_id?}",
        },
        "legacy_aliases": {
            "deprecated": True,
            "description": "Thin aliases of /api/v1/channels (Deprecation + Link successor headers).",
            "endpoints": [
                "/streams.json", "/streams{N}.json", "/streams.m3u", "/streams{N}.m3u",
                "/api/v1/streams", "/api/v1/streams.m3u",
                "/unchecked/", "/api/v1/unchecked",
            ],
        },
        "cors": {
            "Access-Control-Allow-Origin": "*",
            "methods": ["GET", "OPTIONS", "HEAD"],
        },
        "rate_limit": {
            "wait_seconds": wait,
            "burst": max(1, int(API_PUBLIC_BURST or 3)),
            "on_limit": "HTTP 429 + Retry-After + JSON body",
        },
        "page_size": API_PUBLIC_PAGE_SIZE,
        "max_limit": API_PUBLIC_MAX_LIMIT,
        "auth": "none",
    }


def _attach_support(payload: dict) -> dict:
    """Support block for rare / discovery bodies (not for every channel page)."""
    out = dict(payload)
    out["support"] = support_block()
    return out


# --------------------------------------------------------------------------
# Client IP + rate limiting
# --------------------------------------------------------------------------

def client_ip() -> str:
    """Caller address; proxy headers are honoured only when ``IPTV_TRUST_PROXY``."""
    if IPTV_TRUST_PROXY:
        forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
        real = (request.headers.get("X-Real-IP") or "").strip()
        if real:
            return real
    return request.remote_addr or "unknown"


_client_ip = client_ip  # backwards-compatible private name


def _prune_buckets(now: float) -> None:
    stale = [k for k, b in _rate_buckets.items() if now - b.get("updated", 0) > 600]
    for k in stale:
        _rate_buckets.pop(k, None)


# scope -> (min interval seconds per token, burst). "api" is read lazily from config.
_RATE_SCOPES: dict[str, tuple[float, int]] = {
    "channels": (IPTV_CHANNELS_MIN_INTERVAL_SEC, IPTV_CHANNELS_BURST),
    "proxy": (IPTV_PROXY_MIN_INTERVAL_SEC, IPTV_PROXY_BURST),
    "report": (IPTV_REPORT_MIN_INTERVAL_SEC, IPTV_REPORT_BURST),
    "admin_purge": (IPTV_ADMIN_PURGE_MIN_INTERVAL_SEC, IPTV_ADMIN_PURGE_BURST),
}


def _scope_limits(scope: str) -> tuple[float, int]:
    if scope in _RATE_SCOPES:
        return _RATE_SCOPES[scope]
    return (
        max(0.5, float(API_PUBLIC_MIN_INTERVAL_SEC or 2.0)),
        max(1, int(API_PUBLIC_BURST or 3)),
    )


def check_rate_limit(scope: str = "api") -> Optional[Response]:
    """Token-bucket limit per ``(scope, client ip)``. Returns a 429 when the client should wait."""
    interval, burst = _scope_limits(scope)
    key = f"{scope}:{client_ip()}"
    now = time.monotonic()

    with _rate_lock:
        _prune_buckets(now)
        bucket = _rate_buckets.get(key)
        if not bucket:
            _rate_buckets[key] = {"tokens": float(burst - 1), "updated": now}
            return None

        elapsed = now - float(bucket.get("updated") or now)
        tokens = float(bucket.get("tokens") or 0) + elapsed / interval
        if tokens > burst:
            tokens = float(burst)

        if tokens < 1.0:
            wait = max(1, int(round((1.0 - tokens) * interval)) or 1)
            bucket["updated"] = now
            bucket["tokens"] = tokens
            body = _attach_support({
                "ok": False,
                "error": "rate_limited",
                "message": (
                    f"Please wait a few seconds - you are making too many requests. "
                    f"Try again in about {wait} second{'s' if wait != 1 else ''}."
                ),
                "retry_after": wait,
                "wait_seconds": wait,
            })
            resp = Response(
                json.dumps(body, ensure_ascii=False),
                status=429,
                mimetype="application/json; charset=utf-8",
            )
            resp.headers["Retry-After"] = str(wait)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Support-Ko-fi"] = SUPPORT_KOFI
            return resp

        bucket["tokens"] = tokens - 1.0
        bucket["updated"] = now
        return None


def check_public_rate_limit() -> Optional[Response]:
    """Public API bucket (default ~1 request / 2 s, burst 3)."""
    return check_rate_limit("api")


def check_channels_rate_limit() -> Optional[Response]:
    """Looser bucket for the interactive guide's ``/channels``."""
    return check_rate_limit("channels")


def check_proxy_rate_limit() -> Optional[Response]:
    """Generous bucket for ``/proxy/*`` (HLS pulls many segments per second)."""
    return check_rate_limit("proxy")


def check_report_rate_limit() -> Optional[Response]:
    """Bucket for POST reports / heartbeats."""
    return check_rate_limit("report")


def check_admin_purge_rate_limit() -> Optional[Response]:
    """Strict bucket for catalog purge attempts (auth failures count too)."""
    return check_rate_limit("admin_purge")


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------

def _public_base() -> str:
    return resolve_public_base_url()


def _abs_url(path: str) -> str:
    base = _public_base()
    if not path.startswith("/"):
        path = "/" + path
    if not base:
        return path
    return base + path


UNCHECKED_STATUSES = ("pending", "unknown")
DEAD_PUBLIC_STATUSES = ("offline", "error")
UNVERIFIED_WARNING = (
    "WARNING: Channels in this list are unchecked or previously dead and may not load. "
    "If a stream plays successfully, POST /api/report-alive with {\"url\": \"...\"} "
    "so we promote it and run a priority health check."
)


def streams_page_path(page: int) -> str:
    page = max(1, int(page))
    if page <= 1:
        return "/streams.json"
    return f"/streams{page}.json"


def streams_m3u_path(page: int) -> str:
    page = max(1, int(page))
    if page <= 1:
        return "/streams.m3u"
    return f"/streams{page}.m3u"


def unchecked_page_path(page: int, *, kind: str = "all") -> str:
    """Paths under the legacy /unchecked/ directory."""
    page = max(1, int(page))
    kind = (kind or "all").lower()
    if kind not in ("all", "unchecked", "dead"):
        kind = "all"
    if kind == "all":
        base = "/unchecked/all"
    elif kind == "dead":
        base = "/unchecked/dead"
    else:
        base = "/unchecked/streams"
    if page <= 1:
        return f"{base}.json"
    return f"{base}{page}.json"


# --------------------------------------------------------------------------
# Channel serialisation
# --------------------------------------------------------------------------

def serialize_public_channel(
    channel: dict,
    *,
    include_proxy: bool = True,
    viewers: Optional[int] = 0,
) -> dict:
    """Flat, engine-friendly channel object.

    ``server`` mirrors ``country`` (where the stream host is located). ``viewers``
    is the live "watching now" count; pass ``None`` to omit it.
    """
    name = (channel.get("name") or "Untitled").strip() or "Untitled"
    url = (channel.get("url") or "").strip()
    # Never block the request path on DNS/geo network — use stored country /
    # cache only (ingest + geo_backfill fill host geo asynchronously).
    country = (
        resolve_country_code(channel)
        or (channel.get("country") or "").strip()
        or infer_country(channel, live=False)
        or "GLOBAL"
    )
    if isinstance(country, str):
        country = country.strip().upper() or "GLOBAL"
    if country == "UK":
        country = "GB"

    specific, generic = resolve_channel_icon_urls(channel)
    logo = _abs_url(specific) if specific.startswith("/") else specific
    generic_logo = _abs_url(generic if generic.startswith("/") else GENERIC_ICON_URL)

    row: dict[str, Any] = {
        "name": name,
        "url": url,
        "logo": logo or "",
        "generic_logo": generic_logo,
        "group": (channel.get("group_title") or "").strip(),
        "server": country,
        "country": country,
        "status": (channel.get("status") or "unknown").lower(),
        "media_type": (channel.get("media_type") or "live").lower(),
    }
    if include_proxy and url:
        row["proxy_url"] = _abs_url("/proxy/stream?url=" + quote(url, safe=""))
    if viewers is not None:
        row["viewers"] = max(0, int(viewers or 0))
    return row


# Backwards-compatible name used by older callers.
serialize_public_stream = serialize_public_channel


def _viewer_counts(store, urls: list[str]) -> dict[str, int]:
    try:
        return store.viewer_counts_for_urls(urls)
    except Exception:
        log.debug("viewer counts unavailable", exc_info=True)
        return {}


# --------------------------------------------------------------------------
# The one builder
# --------------------------------------------------------------------------

CHANNEL_SORTS = ("trending", "name", "server", "group", "last_checked_at", "scanned")
_SORT_ALIASES = {
    "trending": "trending", "popular": "trending", "trend": "trending", "trend_score": "trending",
    "name": "name",
    "server": "server", "country": "server",
    "group": "group", "group_title": "group",
    "last_checked_at": "last_checked_at",
    "last_checked": "last_checked_at",
    "scanned": "last_checked_at",
    "recently_scanned": "last_checked_at",
}
_STATUS_ALIASES = {
    "online": "online", "live": "online",
    "unchecked": "unchecked",
    "dead": "dead",
    "all": "all",
    "unverified": "unverified",  # unchecked + dead (legacy /unchecked/all)
    "pending": "pending", "unknown": "unknown", "offline": "offline", "error": "error",
}
_BASE_DEFAULTS: dict[str, Any] = {
    "status": "online",
    "media_type": "live",
    "sort": "trending",
    "limit": API_PUBLIC_PAGE_SIZE,
}


def _default_dir(sort: str) -> str:
    return (
        "desc"
        if sort
        in (
            "trending",
            "popular",
            "trend_score",
            "watch_count",
            "last_checked_at",
            "last_checked",
            "scanned",
            "recently_scanned",
            "last_watched_at",
            "updated_at",
        )
        else "asc"
    )


def _arg(args: Mapping, *keys: str) -> str:
    for key in keys:
        val = args.get(key)
        if val is None:
            continue
        val = str(val).strip()
        if val:
            return val
    return ""


def _csv_args(args: Mapping, *keys: str) -> list[str]:
    """Collect repeated and/or comma-separated query values (de-duplicated)."""
    raw: list[str] = []
    for key in keys:
        if hasattr(args, "getlist"):
            raw.extend([str(v) for v in (args.getlist(key) or [])])
        else:
            val = args.get(key) if hasattr(args, "get") else None
            if val is not None:
                raw.append(str(val))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in item.split(","):
            s = part.strip()
            if not s:
                continue
            key = s.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
    return out


def parse_channels_params(
    args: Optional[Mapping] = None,
    *,
    defaults: Optional[Mapping] = None,
    overrides: Optional[Mapping] = None,
) -> dict[str, Any]:
    """Normalise query args into the builder's filter dict."""
    args = args if args is not None else {}
    d = {**_BASE_DEFAULTS, **(defaults or {})}

    try:
        page = max(1, int(_arg(args, "page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = int(_arg(args, "limit", "per_page") or d["limit"])
    except (TypeError, ValueError):
        limit = int(d["limit"])
    limit = max(1, min(limit, API_PUBLIC_MAX_LIMIT))

    status = _STATUS_ALIASES.get(_arg(args, "status").lower() or str(d["status"]).lower())
    if status is None:
        status = _STATUS_ALIASES.get(str(d["status"]).lower(), "online")

    media_type = (_arg(args, "media_type", "type") or str(d["media_type"])).lower()
    if media_type not in ("live", "vod", "unknown", "all"):
        media_type = str(d["media_type"]).lower()

    sort = _SORT_ALIASES.get(_arg(args, "sort").lower()) or _SORT_ALIASES.get(str(d["sort"]).lower(), "trending")
    sort_dir = _arg(args, "sort_dir", "dir", "order").lower()
    if sort_dir not in ("asc", "desc"):
        sort_dir = str(d.get("sort_dir") or _default_dir(sort)).lower()

    include = {t.strip().lower() for t in _arg(args, "include").split(",") if t.strip()}
    groups = _csv_args(args, "group")
    servers = _csv_args(args, "server", "country")

    params: dict[str, Any] = {
        "page": page,
        "limit": limit,
        "status": status,
        "server": ",".join(servers),
        "group": ",".join(groups),
        "q": _arg(args, "q", "search", "query"),
        "media_type": media_type,
        "sort": sort,
        "sort_dir": sort_dir,
        "include": include,
        "_defaults": d,
    }
    if overrides:
        params.update(overrides)
        # Re-normalise multi filters if overrides set raw strings.
        if "group" in overrides:
            params["group"] = ",".join(_csv_args({"group": params["group"]}, "group"))
        if "server" in overrides or "country" in overrides:
            params["server"] = ",".join(
                _csv_args({"server": params["server"]}, "server")
            )
    return params


def _status_filter(status: str) -> tuple[Optional[str], Optional[tuple]]:
    """Map the public ``status`` value to ``list_channels`` (status, status_in)."""
    if status == "unchecked":
        return None, UNCHECKED_STATUSES
    if status == "dead":
        return None, DEAD_PUBLIC_STATUSES
    if status == "unverified":
        return None, UNCHECKED_STATUSES + DEAD_PUBLIC_STATUSES
    if status == "all":
        return None, None
    return status, None


def _query_path(base_path: str, params: dict[str, Any], page: int) -> str:
    """``base_path?...`` preserving every non-default filter."""
    d = params.get("_defaults") or _BASE_DEFAULTS
    pairs: list[tuple[str, str]] = []
    if page > 1:
        pairs.append(("page", str(page)))
    if params["limit"] != int(d["limit"]):
        pairs.append(("limit", str(params["limit"])))
    if params["status"] != str(d["status"]).lower():
        pairs.append(("status", params["status"]))
    if params["server"]:
        pairs.append(("server", params["server"]))
    if params["group"]:
        pairs.append(("group", params["group"]))
    if params["q"]:
        pairs.append(("q", params["q"]))
    if params["media_type"] != str(d["media_type"]).lower():
        pairs.append(("media_type", params["media_type"]))
    if params["sort"] != _SORT_ALIASES.get(str(d["sort"]).lower(), "trending"):
        pairs.append(("sort", params["sort"]))
    if params["sort_dir"] != str(d.get("sort_dir") or _default_dir(params["sort"])).lower():
        pairs.append(("sort_dir", params["sort_dir"]))
    if params["include"]:
        pairs.append(("include", ",".join(sorted(params["include"]))))
    for key, val in (params.get("_extra") or {}).items():
        pairs.append((str(key), str(val)))
    if not pairs:
        return base_path
    return base_path + "?" + urlencode(pairs, quote_via=quote)


def build_channels_page(
    args: Optional[Mapping] = None,
    *,
    base_path: str = "/api/v1/channels",
    page_path: Optional[Callable[[int], str]] = None,
    defaults: Optional[Mapping] = None,
    overrides: Optional[Mapping] = None,
    legacy: bool = False,
    extra_query: Optional[Mapping] = None,
) -> dict[str, Any]:
    """The one DB-backed channel page builder.

    ``args`` are query params (``page, limit, status, server|country, group, q,
    media_type, sort, sort_dir, include``). ``page_path`` lets file-style legacy
    URLs (``/streams2.json``) produce their own ``next_url``; otherwise
    ``next_url`` / ``prev_url`` are ``base_path`` + the filters that are in effect.
    """
    from features.storage.db import get_default_store

    p = parse_channels_params(args, defaults=defaults, overrides=overrides)
    p["_extra"] = dict(extra_query or {})  # fixed params re-emitted in next/prev urls
    store = get_default_store()
    status, status_in = _status_filter(p["status"])
    media_type = None if p["media_type"] == "all" else p["media_type"]

    result = store.list_channels(
        page=p["page"],
        limit=p["limit"],
        q=p["q"] or None,
        group=p["group"] or None,
        country=p["server"] or None,
        include_test=False,
        media_type=media_type,
        status=status,
        status_in=status_in,
        sort=p["sort"],
        sort_dir=p["sort_dir"],
    )
    rows = result.get("channels") or []
    page = int(result.get("page") or p["page"])
    per_page = int(result.get("limit") or p["limit"])
    total = int(result.get("total") or 0)
    total_pages = max(1, int(result.get("total_pages") or 1))
    has_more = bool(result.get("has_more"))

    viewers = _viewer_counts(store, [r.get("url") or "" for r in rows])
    channels = [
        serialize_public_channel(r, viewers=viewers.get((r.get("url") or "").strip(), 0))
        for r in rows
    ]

    next_page = page + 1 if has_more and page < total_pages else None
    prev_page = page - 1 if page > 1 else None
    build = page_path or (lambda n: _query_path(base_path, p, n))
    wait = max(1, int(round(API_PUBLIC_MIN_INTERVAL_SEC)) or 2)

    payload: dict[str, Any] = {
        "ok": True,
        "api": API_ID,
        "revision": int(result.get("revision") or store.get_revision() or 0),
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_more": has_more,
        "next_url": _abs_url(build(next_page)) if next_page else None,
        "prev_url": _abs_url(build(prev_page)) if prev_page else None,
        "wait_seconds": wait,
        "channels": channels,
    }
    if "facets" in p["include"]:
        payload["facets"] = {
            "countries": result.get("countries") or [],
            "groups": result.get("groups") or [],
        }
    if p["status"] != "online":
        payload["warning"] = UNVERIFIED_WARNING
    if legacy:
        payload["streams"] = channels  # old key (VR worlds already parse it)
        payload["next_page"] = next_page
        payload["prev_page"] = prev_page
        payload["count"] = len(channels)
        if p["status"] != "online":
            payload["may_not_load"] = True
            payload["report_alive_url"] = _abs_url("/api/report-alive")
    return payload


def _catalog_status(catalog: Optional[str]) -> str:
    """Old ``catalog`` names -> public ``status`` values."""
    key = (catalog or "online").lower().strip()
    if key in ("unchecked", "pending"):
        return "unchecked"
    if key in ("dead", "offline"):
        return "dead"
    if key in ("all_unverified", "unverified", "all"):
        return "unverified"
    return "online"


def build_streams_page(
    page: int = 1,
    *,
    per_page: int = API_PUBLIC_PAGE_SIZE,
    country: Optional[str] = None,
    group: Optional[str] = None,
    q: Optional[str] = None,
    catalog: str = "online",
) -> dict[str, Any]:
    """Compatibility wrapper (old call shape) around :func:`build_channels_page`."""
    args = {"page": page, "limit": per_page, "server": country or "", "group": group or "", "q": q or ""}
    return build_channels_page(
        args,
        defaults={"sort": "name", "sort_dir": "asc"},
        overrides={"status": _catalog_status(catalog)},
        legacy=True,
    )


def _m3u_attr(value: str) -> str:
    return (value or "").replace('"', "").replace("\r", " ").replace("\n", " ").strip()


def _m3u_entry(lines: list[str], ch: dict, *, url: str) -> None:
    name = _m3u_attr(ch.get("name") or "Channel") or "Channel"
    attrs = [f'tvg-name="{name}"']
    logo = _m3u_attr(ch.get("logo") or "")
    if logo:
        attrs.append(f'tvg-logo="{logo}"')
    country = _m3u_attr(ch.get("country") or "")
    if country:
        attrs.append(f'tvg-country="{country}"')
    group = _m3u_attr(ch.get("group") or "")
    if group:
        attrs.append(f'group-title="{group}"')
    lines.append(f"#EXTINF:-1 {' '.join(attrs)},{name.replace(',', ' ')}")
    lines.append(url)


def build_channels_m3u(args: Optional[Mapping] = None, **kwargs) -> tuple[str, dict]:
    """Return ``(m3u_body, payload)`` for one page (same query as the JSON builder)."""
    kwargs.setdefault("base_path", "/api/v1/channels.m3u")
    payload = build_channels_page(args, **kwargs)
    lines = [
        "#EXTM3U",
        f"#IPTV-SCANNER:page={payload['page']} per_page={payload['per_page']} "
        f"total={payload['total']} wait_seconds={payload['wait_seconds']}",
        f"#IPTV-SCANNER-SUPPORT:{SUPPORT_MESSAGE} {SUPPORT_KOFI}",
    ]
    if payload.get("warning"):
        lines.append(f"#IPTV-SCANNER-WARNING:{payload['warning']}")
    for ch in payload["channels"]:
        url = (ch.get("url") or "").strip()
        if url:
            _m3u_entry(lines, ch, url=url)
    if payload.get("next_url"):
        lines.append(f"#IPTV-SCANNER-NEXT:{payload['next_url']}")
    return "\n".join(lines) + "\n", payload


def build_streams_m3u(page: int = 1, **filters) -> tuple[str, dict]:
    """Compatibility wrapper (old call shape)."""
    status = _catalog_status(filters.get("catalog"))
    args = {
        "page": page,
        "server": filters.get("country") or "",
        "group": filters.get("group") or "",
        "q": filters.get("q") or "",
    }
    return build_channels_m3u(
        args,
        base_path="/api/v1/streams.m3u",
        page_path=streams_m3u_path,
        defaults={"sort": "name", "sort_dir": "asc"},
        overrides={"status": status},
        legacy=True,
    )


_PLAYLIST_CACHE_TTL = 60.0
_playlist_cache: dict[tuple, tuple[float, str]] = {}
_playlist_cache_lock = threading.Lock()


def build_playlist_m3u(args: Optional[Mapping] = None) -> str:
    """Full online lineup as one M3U. ``proxy=1`` routes URLs through ``/proxy/stream``."""
    from features.storage.db import get_default_store

    args = args if args is not None else {}
    server = _arg(args, "server", "country")
    group = _arg(args, "group")
    media_type = (_arg(args, "media_type", "type") or "live").lower()
    if media_type not in ("live", "vod", "unknown", "all"):
        media_type = "live"
    proxied = _arg(args, "proxy").lower() in ("1", "true", "yes", "on")

    store = get_default_store()
    key = (server, group, media_type, proxied, _public_base(), store.get_revision())
    now = time.monotonic()
    with _playlist_cache_lock:
        hit = _playlist_cache.get(key)
        if hit and now - hit[0] < _PLAYLIST_CACHE_TTL:
            return hit[1]

    lines = ["#EXTM3U", f"#IPTV-SCANNER-SUPPORT:{SUPPORT_MESSAGE} {SUPPORT_KOFI}"]
    page = 1
    while True:  # list_channels caps one page at 1000 rows
        result = store.list_channels(
            page=page,
            limit=1000,
            country=server or None,
            group=group or None,
            include_test=False,
            media_type=None if media_type == "all" else media_type,
            status="online",
            sort="name",
            sort_dir="asc",
        )
        for row in result.get("channels") or []:
            ch = serialize_public_channel(row, include_proxy=proxied, viewers=None)
            url = ch.get("proxy_url") if proxied else ch.get("url")
            if url:
                _m3u_entry(lines, ch, url=url)
        if not result.get("has_more"):
            break
        page += 1
    body = "\n".join(lines) + "\n"

    with _playlist_cache_lock:
        if len(_playlist_cache) >= 8:
            _playlist_cache.clear()
        _playlist_cache[key] = (now, body)
    return body


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------

def json_response(payload: dict, *, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    resp = Response(body, status=status, mimetype="application/json; charset=utf-8")
    _apply_cors_headers(resp)
    resp.headers["Cache-Control"] = "public, max-age=15"
    resp.headers["X-Support-Message"] = SUPPORT_MESSAGE
    resp.headers["X-Support-Ko-fi"] = SUPPORT_KOFI
    if status == 429 and isinstance(payload.get("retry_after"), int):
        resp.headers["Retry-After"] = str(payload["retry_after"])
        resp.headers["Cache-Control"] = "no-store"
    return resp


def m3u_response(body: str, *, filename: str = "channels.m3u") -> Response:
    resp = Response(body, status=200, mimetype="application/vnd.apple.mpegurl; charset=utf-8")
    _apply_cors_headers(resp)
    resp.headers["Cache-Control"] = "public, max-age=15"
    resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    resp.headers["X-Support-Message"] = SUPPORT_MESSAGE
    resp.headers["X-Support-Ko-fi"] = SUPPORT_KOFI
    return resp


def _apply_cors_headers(resp: Response) -> None:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept"


def _cors_preflight() -> Response:
    resp = Response(status=204)
    _apply_cors_headers(resp)
    return resp


def _add_link(resp: Response, url: str, rel: str) -> None:
    link = f'<{url}>; rel="{rel}"'
    existing = resp.headers.get("Link")
    resp.headers["Link"] = f"{existing}, {link}" if existing else link


def _mark_deprecated(resp: Response, successor_path: str) -> None:
    """Flag a legacy alias; the successor keeps the caller's query string."""
    qs = request.query_string.decode("utf-8", "replace")
    url = _abs_url(successor_path) + (f"?{qs}" if qs else "")
    resp.headers["Deprecation"] = "true"
    _add_link(resp, url, "successor-version")


def _prepare_guides_for_template(base: str) -> list[dict]:
    host = base.rstrip("/") if base else "https://YOUR-HOST"
    guides = []
    for raw in platform_guides():
        g = dict(raw)
        g["steps"] = [str(s).replace("{BASE}", host) for s in (g.get("steps") or [])]
        g["snippet"] = str(g.get("snippet") or "").replace("{BASE}", host)
        guides.append(g)
    return guides


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

_JSON_MT = "application/json; charset=utf-8"
_M3U_MT = "application/vnd.apple.mpegurl"


def register_public_api(app) -> None:
    """Attach public API routes to the Flask app."""

    def _serve(
        fmt: str,
        *,
        base_path: str,
        args: Optional[Mapping] = None,
        page_path: Optional[Callable[[int], str]] = None,
        defaults: Optional[Mapping] = None,
        overrides: Optional[Mapping] = None,
        legacy_successor: Optional[str] = None,
        extra_query: Optional[Mapping] = None,
        filename: str = "channels.m3u",
    ):
        """Shared handler for every channels-shaped URL (new and legacy alias)."""
        if request.method == "OPTIONS":
            return _cors_preflight()
        limited = check_public_rate_limit()
        if limited is not None:
            return limited
        legacy = legacy_successor is not None
        if request.method == "HEAD":
            resp = Response(status=200, mimetype=_JSON_MT if fmt == "json" else _M3U_MT)
            _apply_cors_headers(resp)
            if legacy:
                _mark_deprecated(resp, legacy_successor)
            return resp

        kwargs = dict(
            base_path=base_path, page_path=page_path,
            defaults=defaults, overrides=overrides, legacy=legacy,
            extra_query=extra_query,
        )
        query = request.args if args is None else args
        if fmt == "json":
            resp = json_response(build_channels_page(query, **kwargs))
        else:
            body, payload = build_channels_m3u(query, **kwargs)
            resp = m3u_response(body, filename=filename)
            if payload.get("next_url"):
                resp.headers["X-Next-Page"] = str(payload["page"] + 1)
                _add_link(resp, payload["next_url"], "next")
        if legacy:
            _mark_deprecated(resp, legacy_successor)
        return resp

    # Legacy file-style aliases ignore the query string (fixed 50 rows, name order).
    _LEGACY_DEFAULTS = {"sort": "name", "sort_dir": "asc"}

    def _api_page_context(base: str) -> dict:
        return {
            "site_name": IPTV_SITE_NAME,
            "base_url": base,
            "page_size": API_PUBLIC_PAGE_SIZE,
            "wait_seconds": max(1, int(round(API_PUBLIC_MIN_INTERVAL_SEC)) or 2),
            "burst": max(1, int(API_PUBLIC_BURST or 3)),
            "support_message": SUPPORT_MESSAGE,
            "support_kofi": SUPPORT_KOFI,
            "support_paypal": SUPPORT_PAYPAL,
            "support_cashapp": SUPPORT_CASHAPP,
            "works_with": supported_platforms(),
            "platforms": _prepare_guides_for_template(base),
            "styles": response_styles(),
            "self_host_repo": SELF_HOST_REPO,
            "self_host_tree": f"{SELF_HOST_REPO}/tree/main",
        }

    @app.route("/api")
    @app.route("/api/")
    def public_api_docs():
        base = _public_base() or ""
        return render_template("api_docs.html", **_api_page_context(base))

    @app.route("/api/playground")
    @app.route("/api/playground/")
    @app.route("/api/try")
    def public_api_playground():
        base = _public_base() or ""
        return render_template("api_playground.html", **_api_page_context(base))

    # -- Universal endpoints -------------------------------------------------

    @app.route("/api/v1/channels", methods=["GET", "HEAD", "OPTIONS"])
    def public_channels_json():
        return _serve("json", base_path="/api/v1/channels")

    @app.route("/api/v1/channels.m3u", methods=["GET", "HEAD", "OPTIONS"])
    def public_channels_m3u():
        return _serve("m3u", base_path="/api/v1/channels.m3u", filename="channels.m3u")

    @app.route("/api/v1/playlist.m3u", methods=["GET", "HEAD", "OPTIONS"])
    def public_playlist_m3u():
        if request.method == "OPTIONS":
            return _cors_preflight()
        if IPTV_PLAYLIST_SECRET and request.args.get("token") != IPTV_PLAYLIST_SECRET:
            return json_response({"ok": False, "error": "forbidden"}, status=403)
        limited = check_public_rate_limit()
        if limited is not None:
            return limited
        if request.method == "HEAD":
            resp = Response(status=200, mimetype=_M3U_MT)
            _apply_cors_headers(resp)
            return resp
        resp = m3u_response(build_playlist_m3u(request.args), filename="playlist.m3u")
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp

    @app.route("/openapi.yaml", methods=["GET", "HEAD"])
    @app.route("/api/v1/openapi.yaml", methods=["GET", "HEAD"])
    def public_openapi_yaml():
        path = os.path.join(_REPO_ROOT, "openapi.yaml")
        if not os.path.exists(path):
            return json_response({"ok": False, "error": "openapi.yaml missing"}, status=404)
        resp = send_file(path, mimetype="application/yaml")
        _apply_cors_headers(resp)
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    # -- Legacy aliases (same builder, Deprecation + Link successor) --------

    @app.route("/streams.json", methods=["GET", "HEAD", "OPTIONS"])
    @app.route("/streams<int:page>.json", methods=["GET", "HEAD", "OPTIONS"])
    def public_streams_file(page: int = 1):
        return _serve(
            "json", base_path="/streams.json", args={}, page_path=streams_page_path,
            defaults=_LEGACY_DEFAULTS, overrides={"page": max(1, page)},
            legacy_successor="/api/v1/channels",
        )

    @app.route("/streams.m3u", methods=["GET", "HEAD", "OPTIONS"])
    @app.route("/streams<int:page>.m3u", methods=["GET", "HEAD", "OPTIONS"])
    def public_streams_m3u_file(page: int = 1):
        return _serve(
            "m3u", base_path="/streams.m3u", args={}, page_path=streams_m3u_path,
            defaults=_LEGACY_DEFAULTS, overrides={"page": max(1, page)},
            legacy_successor="/api/v1/channels.m3u",
            filename="streams.m3u" if page <= 1 else f"streams{page}.m3u",
        )

    @app.route("/api/v1/streams", methods=["GET", "HEAD", "OPTIONS"])
    def public_streams_query():
        return _serve(
            "json", base_path="/api/v1/streams", defaults=_LEGACY_DEFAULTS,
            overrides={"status": "online"}, legacy_successor="/api/v1/channels",
        )

    @app.route("/api/v1/streams.m3u", methods=["GET", "HEAD", "OPTIONS"])
    def public_streams_m3u_query():
        return _serve(
            "m3u", base_path="/api/v1/streams.m3u", defaults=_LEGACY_DEFAULTS,
            overrides={"status": "online"}, legacy_successor="/api/v1/channels.m3u",
            filename="streams.m3u",
        )

    @app.route("/unchecked/", methods=["GET", "HEAD", "OPTIONS"])
    @app.route("/unchecked", methods=["GET", "HEAD", "OPTIONS"])
    def public_unchecked_index():
        if request.method == "OPTIONS":
            return _cors_preflight()
        wait = max(1, int(round(API_PUBLIC_MIN_INTERVAL_SEC)) or 2)
        resp = json_response(
            {
                "ok": True,
                "directory": "unchecked",
                "deprecated": True,
                "warning": UNVERIFIED_WARNING,
                "may_not_load": True,
                "successor": _abs_url("/api/v1/channels?status=unchecked"),
                "report_alive_url": _abs_url("/api/report-alive"),
                "report_playback_url": _abs_url("/api/v1/report-playback"),
                "endpoints": {
                    "all_unverified": _abs_url("/unchecked/all.json"),
                    "unchecked_only": _abs_url("/unchecked/streams.json"),
                    "dead_only": _abs_url("/unchecked/dead.json"),
                    "query": _abs_url("/api/v1/unchecked?page=1&kind=all"),
                },
                "kinds": {
                    "all": "pending + unknown + offline + error",
                    "unchecked": "pending + unknown (not yet confirmed)",
                    "dead": "offline + error (previously failed)",
                },
                "wait_seconds": wait,
                "page_size": API_PUBLIC_PAGE_SIZE,
            }
        )
        resp.headers["Deprecation"] = "true"
        _add_link(resp, _abs_url("/api/v1/channels?status=unchecked"), "successor-version")
        return resp

    def _unchecked(kind: str, page: int = 1):
        status = {"all": "unverified", "unchecked": "unchecked", "dead": "dead"}[kind]
        return _serve(
            "json", base_path=unchecked_page_path(1, kind=kind), args={},
            page_path=lambda n: unchecked_page_path(n, kind=kind),
            defaults=_LEGACY_DEFAULTS, overrides={"status": status, "page": max(1, page)},
            legacy_successor="/api/v1/channels",
        )

    @app.route("/unchecked/all.json", methods=["GET", "HEAD", "OPTIONS"])
    @app.route("/unchecked/all<int:page>.json", methods=["GET", "HEAD", "OPTIONS"])
    def public_unchecked_all(page: int = 1):
        return _unchecked("all", page)

    @app.route("/unchecked/streams.json", methods=["GET", "HEAD", "OPTIONS"])
    @app.route("/unchecked/streams<int:page>.json", methods=["GET", "HEAD", "OPTIONS"])
    def public_unchecked_streams(page: int = 1):
        return _unchecked("unchecked", page)

    @app.route("/unchecked/dead.json", methods=["GET", "HEAD", "OPTIONS"])
    @app.route("/unchecked/dead<int:page>.json", methods=["GET", "HEAD", "OPTIONS"])
    def public_unchecked_dead(page: int = 1):
        return _unchecked("dead", page)

    @app.route("/api/v1/unchecked", methods=["GET", "HEAD", "OPTIONS"])
    def public_unchecked_query():
        kind = (request.args.get("kind") or "all").strip().lower()
        status = {
            "all": "unverified", "unverified": "unverified",
            "unchecked": "unchecked", "pending": "unchecked",
            "dead": "dead", "offline": "dead",
        }.get(kind, "unverified")
        return _serve(
            "json", base_path="/api/v1/unchecked",
            defaults={**_LEGACY_DEFAULTS, "status": status},
            overrides={"status": status},
            extra_query={"kind": kind},
            legacy_successor="/api/v1/channels",
        )

    # -- Discovery index -----------------------------------------------------

    @app.route("/api/v1", methods=["GET", "HEAD", "OPTIONS"])
    @app.route("/api/v1/", methods=["GET", "HEAD", "OPTIONS"])
    def public_api_v1_index():
        if request.method == "OPTIONS":
            return _cors_preflight()
        wait = max(1, int(round(API_PUBLIC_MIN_INTERVAL_SEC)) or 2)
        return json_response(
            _attach_support({
                "ok": True,
                "api": API_ID,
                "docs": _abs_url("/api"),
                "playground": _abs_url("/api/playground"),
                "openapi": _abs_url("/openapi.yaml"),
                "endpoints": {
                    "channels": _abs_url("/api/v1/channels"),
                    "channels_m3u": _abs_url("/api/v1/channels.m3u"),
                    "playlist_m3u": _abs_url("/api/v1/playlist.m3u"),
                    "report_playback": _abs_url("/api/v1/report-playback"),
                    "report_alive": _abs_url("/api/report-alive"),
                    "watching": _abs_url("/api/v1/watching"),
                },
                "query": {
                    "page": "1-based page number",
                    "limit": f"rows per page (default {API_PUBLIC_PAGE_SIZE}, max {API_PUBLIC_MAX_LIMIT})",
                    "status": "online (default) | unchecked | dead | all | pending | unknown | offline | error",
                    "server": "country code / name of the stream host (alias: country); comma-separate for multiple (OR)",
                    "group": "exact category; comma-separate for multiple (OR)",
                    "q": "text search",
                    "media_type": "live (default) | vod | all",
                    "sort": "trending (default) | name | server | group | scanned | last_checked_at",
                    "sort_dir": "asc | desc",
                    "include": "facets (adds countries[] + groups[])",
                },
                "deprecated": {
                    "note": "Thin aliases of /api/v1/channels; responses carry Deprecation + Link successor-version.",
                    "endpoints": [
                        "/streams.json", "/streams{N}.json", "/streams.m3u", "/streams{N}.m3u",
                        "/api/v1/streams", "/api/v1/streams.m3u",
                        "/unchecked/", "/unchecked/all.json", "/unchecked/streams.json",
                        "/unchecked/dead.json", "/api/v1/unchecked",
                    ],
                },
                "response_styles": list(response_styles().keys()),
                "integrations": integrations_map(),
                "page_size": API_PUBLIC_PAGE_SIZE,
                "max_limit": API_PUBLIC_MAX_LIMIT,
                "wait_seconds": wait,
                "auth": "none",
                "methods": ["GET", "HEAD", "OPTIONS", "POST (report-playback, report-alive, watching)"],
                "notes": [
                    "No API key required.",
                    f"Fetch at most one page every ~{wait}s to avoid rate limits.",
                    "Walk pages with next_url from each JSON response (or Link rel=next on M3U).",
                    "If a stream fails to play, POST /api/v1/report-playback {url, event: no_playback}.",
                    SUPPORT_MESSAGE,
                ],
            })
        )
