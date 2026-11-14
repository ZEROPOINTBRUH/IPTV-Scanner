"""Normalization helpers for IPTV Scanner Phase 2.

These utilities canonicalize channel data before it enters the SQLite store so
the same underlying stream cannot appear twice under slightly different URLs
or contradictory metadata. Helpers are intentionally conservative — when in
doubt, keep the original value rather than mangling it.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional


# Query params commonly used for analytics / session tracking that should be
# stripped when building a normalized URL. Anything not on this list is kept
# (and sorted alphabetically) so real stream tokens survive round-trips.
_TRACKING_QUERY_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "yclid",
    }
)

# Hosts that are well-known demo / sample streams. Anything served from these
# hosts should be treated as a Test channel regardless of the group-title we
# received.
_DEMO_HOSTS = frozenset(
    {
        "d3rlna7iyyu8wu.cloudfront.net",
        "amssamples.streaming.mediaservices.windows.net",
        "test-streams.mux.dev",
        "demo.unified-streaming.com",
        "cph-p2p-msl.akamaized.net",
        "bitdash-a.akamaihd.net",
        "sample.vodobox.com",
    }
)

# Substring markers that make a channel a Test channel. Kept conservative so
# we don't accidentally re-tag legitimate networks (e.g. "Contest TV").
_TEST_NAME_MARKERS = (
    "dolby vod test",
    "azure test",
    "big buck bunny",
    "sintel",
    "tears of steel",
    " sample",
    " demo stream",
    "test pattern",
    "hls test",
    "dash test",
)

_TEST_GROUPS = frozenset({"test", "tests", "sample", "samples", "demo", "demos"})

# Path suffixes that mean a progressive on-demand file (not an HLS live loop).
_VOD_FILE_SUFFIXES = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpg", ".mpeg")

# Path segments that strongly suggest a VOD catalog entry (not a 24/7 loop).
_VOD_PATH_MARKERS = (
    "/movies/",
    "/movie/",
    "/series/",
    "/episodes/",
    "/episode/",
    "/film/",
    "/films/",
    "/vod/movie",
    "/vod/series",
)

_LIVE_URL_TOKENS = (
    "/live/",
    "/hls/live",
    "/dash/live",
    "manifest.mpd",
    "playlist.m3u8",
    "live.m3u8",
    "master.m3u8",
    "index.m3u8",
    "chunklist",
    "/hls/",
    "youtu.be/",
    "youtube.com/",
    "twitch.tv/",
)


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Return a canonical form of ``url`` suitable for de-duplication.

    Returns ``None`` for empty / non-string input so callers can decide whether
    to skip the row entirely.
    """
    if not url:
        return None
    raw = str(url).strip()
    if not raw:
        return None

    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return raw.lower()

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https", "rtmp", "rtsp", "rtmps"):
        # Non-web schemes are returned lowercased but otherwise untouched.
        return raw.lower()

    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None:
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None

    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"

    netloc = f"{userinfo}{hostname}"
    if port is not None:
        netloc += f":{port}"

    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    if parsed.query:
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        params = [(k, v) for k, v in params if k.lower() not in _TRACKING_QUERY_PARAMS]
        params.sort()
        query = urllib.parse.urlencode(params, doseq=True)
    else:
        query = ""

    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _primary_group(raw: Optional[str]) -> str:
    """Split ``group_title`` on ``;`` / ``,`` and return the first token."""
    if not raw:
        return ""
    text = str(raw)
    for sep in (";", ","):
        if sep in text:
            text = text.split(sep, 1)[0]
    return text.strip()


def is_test_channel(channel: dict) -> bool:
    """Return True when the channel looks like a demo / test stream."""
    if not isinstance(channel, dict):
        return False

    group = (channel.get("group_title") or "").strip().lower()
    if group in _TEST_GROUPS:
        return True

    name = (channel.get("name") or "").strip().lower()
    if name:
        for marker in _TEST_NAME_MARKERS:
            if marker in name:
                return True
        if name.startswith("test ") or name.endswith(" test") or name == "test":
            return True

    url = (channel.get("url") or "").strip().lower()
    if url:
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except Exception:
            host = ""
        host = host.lower()
        if host in _DEMO_HOSTS:
            return True
        if "sample" in url or "/test/" in url or "test-streams" in url:
            return True

    return False


def normalize_group_title(channel: dict) -> str:
    """Reduce a channel's ``group_title`` to a single primary group.

    Mutates ``channel`` in place and also returns the resulting group. Test
    streams are always forced into the ``"Test"`` group so they can be excluded
    from public listings by default.

    Country / region names that playlists stuff into ``group-title`` are moved
    into ``country`` and replaced with ``General`` so Categories stay clean.
    """
    if not isinstance(channel, dict):
        return ""

    from .geo import (
        DEFAULT_CATEGORY_AFTER_COUNTRY_SPLIT,
        country_name_to_code,
    )

    primary = _primary_group(channel.get("group_title"))
    if not primary:
        primary = "Ungrouped"

    if is_test_channel({**channel, "group_title": primary}):
        primary = "Test"
    else:
        code = country_name_to_code(primary)
        if code:
            existing = (channel.get("country") or "").strip().upper()
            if not existing or existing in (
                "GLOBAL",
                "XX",
                "ZZ",
                "UNKNOWN",
                "UNDEFINED",
                "",
            ):
                channel["country"] = code
            primary = DEFAULT_CATEGORY_AFTER_COUNTRY_SPLIT

    channel["group_title"] = primary
    return primary


def _url_path_lower(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).path or "").lower()
    except Exception:
        return ""


def _is_progressive_vod_file(url: str) -> bool:
    """True only for real media files, not ``.mp4.m3u8`` or hostnames like ``mov3.co``."""
    path = _url_path_lower(url)
    if not path:
        return False
    # HLS/DASH always win over a substring like ``.mp4`` inside the path.
    if path.endswith(".m3u8") or path.endswith(".m3u") or path.endswith(".mpd"):
        return False
    return any(path.endswith(ext) for ext in _VOD_FILE_SUFFIXES)


def classify_media_type(
    content_or_url: Optional[str],
    playlist_text: Optional[str] = None,
) -> str:
    """Best-effort classification of a stream as ``live``, ``vod``, or ``unknown``.

    HLS/DASH playlists (including looping playlists with ``#EXT-X-ENDLIST``) stay
    ``live`` so 24/7 loops are not dumped into the Videos section. ``vod`` is for
    progressive files and clearly on-demand catalog paths.

    YouTube / Twitch links in this IPTV catalog are treated as ``live`` (many are
    24/7 news lives behind a watch URL). Validation can demote offline ones later.
    """
    if not content_or_url and not playlist_text:
        return "unknown"

    url = str(content_or_url or "").strip()
    low = url.lower()

    if low and ("youtube.com/" in low or "youtu.be/" in low or "twitch.tv/" in low):
        return "live"

    path = _url_path_lower(url)
    try:
        from features.ingest.m3u_parse import looks_like_dash_url, looks_like_hls_url

        hls_like = looks_like_hls_url(url)
        dash_like = looks_like_dash_url(url)
    except Exception:
        hls_like = path.endswith(".m3u8") or path.endswith(".m3u") or ".m3u8" in low
        dash_like = path.endswith(".mpd") or ".mpd" in low

    if hls_like or dash_like or path.endswith(".m3u8") or path.endswith(".m3u") or path.endswith(".mpd"):
        # Playlist body: EVENT/live tags → live. ENDLIST alone is often a
        # looping "live" channel — keep it in Live TV, not Videos.
        if playlist_text:
            text = playlist_text
            if re.search(r"#EXT-X-PLAYLIST-TYPE\s*:\s*EVENT", text, re.IGNORECASE):
                return "live"
            if re.search(r"#EXT-X-PLAYLIST-TYPE\s*:\s*VOD", text, re.IGNORECASE):
                if any(m in path for m in _VOD_PATH_MARKERS):
                    return "vod"
                return "live"
            if "#EXTM3U" in text or "#EXT-X-STREAM-INF" in text or "#EXTINF" in text:
                return "live"
        return "live"

    if _is_progressive_vod_file(url):
        return "vod"
    if path and any(m in path for m in _VOD_PATH_MARKERS):
        return "vod"

    for token in _LIVE_URL_TOKENS:
        if token in low:
            return "live"

    if playlist_text:
        text = playlist_text
        if "#EXTM3U" in text or "#EXT-X-STREAM-INF" in text or "#EXTINF" in text:
            return "live"

    return "unknown"


def normalize_channel(channel: dict) -> dict:
    """Apply every normalizer to ``channel`` and return the mutated dict."""
    if not isinstance(channel, dict):
        return channel

    from .geo import resolve_country_code

    url = channel.get("url")
    channel["url_norm"] = normalize_url(url)
    normalize_group_title(channel)

    media_type = channel.get("media_type")
    if not media_type or media_type == "unknown":
        channel["media_type"] = classify_media_type(url)

    for key in ("name", "tvg_id", "tvg_logo", "country", "playing_now"):
        val = channel.get(key)
        if isinstance(val, str):
            channel[key] = val.strip()

    # Prefer ISO codes; recover from group-title when playlists stuffed the
    # country name into group-title and left country empty/GLOBAL.
    resolved = resolve_country_code(channel)
    if resolved:
        channel["country"] = resolved
    elif (channel.get("country") or "").strip().upper() in (
        "",
        "GLOBAL",
        "XX",
        "ZZ",
        "UNKNOWN",
        "UNDEFINED",
    ):
        channel["country"] = "GLOBAL"

    return channel


def coerce_dict(row: Any) -> dict:
    """Convert an ``sqlite3.Row`` (or already-dict) into a plain dict."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row) if hasattr(row, "keys") else {}
