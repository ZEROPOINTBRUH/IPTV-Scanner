"""Shared Extended-M3U parsing for IPTV playlists.

Follows the conventions used by iptv-org and common IPTV players:

* ``#EXTINF:-1 tvg-id="…" tvg-logo="…" group-title="…",Title``
* Attribute values are double-quoted and may contain spaces
* Stream URL on the next non-tag line (absolute or relative to the playlist)
* Optional ``#EXTVLCOPT:http-user-agent=…`` / ``http-referrer=…`` before the URL
* Optional ``#EXTGRP:Category`` as a group-title fallback

Online-check helpers classify HLS / DASH URLs even when the extension is hidden
behind query strings (``format=m3u8``, ``type=m3u8``, ``.mpd``, etc.).
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Optional


# key="value" pairs on EXTINF / EXTM3U header lines.
_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"')

# Schemes we keep as playable / scrapeable stream URLs.
_STREAM_SCHEMES = frozenset(
    {
        "http",
        "https",
        "rtmp",
        "rtmps",
        "rtsp",
        "mms",
        "mmsh",
        "udp",
        "rtp",
        "srt",
    }
)

_HLS_QUERY_MARKERS = (
    "m3u8",
    "m3u",
    "hls",
    "playlist",
    "chunklist",
    "format=m3u8",
    "type=m3u8",
    "type=hls",
    "ext=m3u8",
)
_DASH_QUERY_MARKERS = ("mpd", "dash", "format=mpd", "type=mpd", "type=dash")


def parse_extinf_attributes(line: str) -> dict:
    """Parse an ``#EXTINF:`` line into name + metadata fields.

    Title is everything after the first comma that is not inside quotes
    (iptv-org / OTT Navigator convention). Quoted attributes survive spaces.
    """
    out: dict = {}
    if not line.startswith("#EXTINF:"):
        return out

    body = line[len("#EXTINF:") :]
    # Split title on the first comma outside quotes.
    title = ""
    attr_src = body
    in_quotes = False
    for i, ch in enumerate(body):
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "," and not in_quotes:
            attr_src = body[:i]
            title = body[i + 1 :].strip()
            break
    if title:
        out["name"] = title

    attrs = {m.group(1).lower(): m.group(2).strip() for m in _ATTR_RE.finditer(attr_src)}
    if attrs.get("tvg-name"):
        out["name"] = attrs["tvg-name"]
    if attrs.get("tvg-logo") or attrs.get("logo"):
        out["tvg_logo"] = attrs.get("tvg-logo") or attrs.get("logo")
    if attrs.get("tvg-id") or attrs.get("channel-id") or attrs.get("ch-id"):
        out["tvg_id"] = attrs.get("tvg-id") or attrs.get("channel-id") or attrs.get("ch-id")
    if attrs.get("group-title"):
        # Keep full value; normalize_group_title later picks the primary group.
        out["group_title"] = attrs["group-title"]
    if attrs.get("tvg-country"):
        out["tvg_country"] = attrs["tvg-country"]
    if attrs.get("tvg-language"):
        out["tvg_language"] = attrs["tvg-language"]
        out.setdefault("audio_language", attrs["tvg-language"])
    if attrs.get("radio", "").lower() in ("true", "1", "yes"):
        out["media_type"] = "live"
    return out


def _parse_extvlcopt(line: str) -> dict:
    out: dict = {}
    if not line.startswith("#EXTVLCOPT:"):
        return out
    payload = line[len("#EXTVLCOPT:") :].strip()
    if "=" not in payload:
        return out
    key, val = payload.split("=", 1)
    key = key.strip().lower()
    val = val.strip().strip('"')
    if key in ("http-user-agent", "user-agent"):
        out["http_user_agent"] = val
    elif key in ("http-referrer", "http-referer", "referrer", "referer"):
        out["http_referrer"] = val
    return out


def resolve_stream_url(raw: str, base_url: str = "") -> Optional[str]:
    """Return an absolute stream URL, or None if the line is not a stream URL."""
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None
    # Drop accidental wrapping quotes.
    if (line.startswith('"') and line.endswith('"')) or (line.startswith("'") and line.endswith("'")):
        line = line[1:-1].strip()
    if not line:
        return None

    if "://" in line:
        scheme = line.split("://", 1)[0].lower()
        if scheme not in _STREAM_SCHEMES:
            return None
        return line

    # Relative path — only resolve when we have a playlist base.
    if not base_url:
        return None
    if line.startswith("//"):
        # Scheme-relative URL.
        try:
            base_scheme = urllib.parse.urlsplit(base_url).scheme or "https"
        except Exception:
            base_scheme = "https"
        return f"{base_scheme}:{line}"

    joined = urllib.parse.urljoin(base_url, line)
    if joined.startswith(("http://", "https://")):
        return joined
    return None


def looks_like_hls_url(url: str) -> bool:
    """True when the URL should be validated as HLS (path or query cues)."""
    raw = (url or "").strip().lower()
    if not raw:
        return False
    if any(x in raw for x in ("youtube.com/", "youtu.be/", "twitch.tv/")):
        return False
    try:
        parts = urllib.parse.urlsplit(raw)
        path = (parts.path or "").lower()
        query = (parts.query or "").lower()
    except Exception:
        path, query = raw, ""
    if path.endswith(".m3u8") or path.endswith(".m3u"):
        return True
    if ".m3u8" in path or "/hls/" in path or "chunklist" in path or "playlist.m3u8" in path:
        return True
    blob = f"{path}?{query}"
    return any(marker in blob for marker in _HLS_QUERY_MARKERS)


def looks_like_dash_url(url: str) -> bool:
    raw = (url or "").strip().lower()
    if not raw:
        return False
    try:
        parts = urllib.parse.urlsplit(raw)
        path = (parts.path or "").lower()
        query = (parts.query or "").lower()
    except Exception:
        path, query = raw, ""
    if path.endswith(".mpd") or ".mpd" in path or "/dash/" in path:
        return True
    blob = f"{path}?{query}"
    return any(marker in blob for marker in _DASH_QUERY_MARKERS)


def parse_m3u_channels(content: str, base_url: str = "") -> list[dict]:
    """Parse Extended-M3U text into channel dicts (one per stream URL)."""
    if not content:
        return []

    channels: list[dict] = []
    current: dict = {}
    pending_headers: dict = {}
    url_logo_prefix = ""

    # Playlist-level url-logo= from #EXTM3U header.
    first = (content.lstrip().splitlines() or [""])[0].strip()
    if first.upper().startswith("#EXTM3U"):
        for m in _ATTR_RE.finditer(first):
            if m.group(1).lower() in ("url-logo", "tvg-logo"):
                url_logo_prefix = m.group(2).strip()

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            current = parse_extinf_attributes(line)
            continue

        if line.startswith("#EXTGRP:"):
            group = line[len("#EXTGRP:") :].strip().strip('"')
            if group:
                current.setdefault("group_title", group)
            continue

        if line.startswith("#EXTVLCOPT:"):
            pending_headers.update(_parse_extvlcopt(line))
            continue

        if line.startswith("#"):
            # Other tags apply to the next URL; ignore unknown ones.
            continue

        url = resolve_stream_url(line, base_url)
        if not url:
            continue
        if not current:
            # Bare URL without EXTINF — still collect it.
            current = {"name": urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "Stream"}

        logo = (current.get("tvg_logo") or "").strip()
        if logo and url_logo_prefix and "://" not in logo:
            current["tvg_logo"] = urllib.parse.urljoin(url_logo_prefix, logo)

        if pending_headers.get("http_user_agent"):
            current["http_user_agent"] = pending_headers["http_user_agent"]
        if pending_headers.get("http_referrer"):
            current["http_referrer"] = pending_headers["http_referrer"]

        # Prefer playlist tvg-country as country when present.
        tvg_country = (current.pop("tvg_country", None) or "").strip()
        if tvg_country and not current.get("country"):
            current["country"] = tvg_country

        current["url"] = url
        current.setdefault("playing_now", "Not available")
        current.setdefault("status", "unknown")
        channels.append(current)
        current = {}
        pending_headers = {}

    return channels
