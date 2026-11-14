"""Async stream validation (HTTP reachability, HLS parsing + segment probe, YouTube/Twitch live checks).

Rule of thumb: a channel is only ``online`` when we positively saw playable
media bytes — a bare HTTP 200 / HEAD is not enough. HLS must expose a
fetchable *recent* segment with media-like payload, generic streams must
return a non-empty non-HTML body (content-type alone does not pass), and
YouTube/Twitch must be confirmed live (fail closed otherwise).
"""

import asyncio
import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin

import aiohttp

from features.storage.channels_io import save_json_atomic
from config import BATCH_SIZE, FILES, STREAM_HEADERS as HEADERS
from features.icons.icons import download_channel_icon


RETRYABLE_STATUSES = {500, 502, 503, 504, 429, 403}  # include 403 for Cloudflare

# Content-Type hints that a generic URL really is media.
_MEDIA_CT_PREFIXES = ("video/", "audio/")
_MEDIA_CT_EXACT = {
    "application/octet-stream",
    "binary/octet-stream",
    "application/ogg",
    "application/mp2t",
    "application/x-flv",
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "application/dash+xml",
    "application/vnd.ms-sstr+xml",
}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}

_YT_LIVE_RE = re.compile(r'"isLiveNow"\s*:\s*true')
_TWITCH_LIVE_RE = re.compile(r'"isLiveBroadcast"\s*:\s*true|data-is-live="true"')


# Fast-fail defaults for health scanning (env override).
_PROBE_CONNECT = float(os.environ.get("IPTV_PROBE_CONNECT_SEC", "4"))
_PROBE_TOTAL = float(os.environ.get("IPTV_PROBE_TOTAL_SEC", "8"))
_PROBE_READ = float(os.environ.get("IPTV_PROBE_READ_SEC", "6"))


def _probe_timeout(total=None) -> aiohttp.ClientTimeout:
    t = float(total if total is not None else _PROBE_TOTAL)
    return aiohttp.ClientTimeout(
        total=t,
        sock_connect=min(_PROBE_CONNECT, t),
        sock_read=min(_PROBE_READ, t),
    )


async def check_link_exists(session, url, retries=1, delay=1):
    """Cheap reachability probe — one fast attempt by default (no multi-minute backoff)."""
    timeout = _probe_timeout()
    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, timeout=timeout, headers=HEADERS) as response:
                if response.status in {200, 302}:
                    return True
                if response.status in RETRYABLE_STATUSES:
                    logging.debug("Retryable error %s for %s, attempt %s", response.status, url, attempt)
                    if attempt < retries:
                        await asyncio.sleep(delay * attempt)
                    continue
                logging.debug("Invalid link %s (status: %s)", url, response.status)
                return False
        except aiohttp.ClientError as e:
            logging.debug("Network error attempt %s for %s: %s", attempt, url, e)
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
            continue
        except Exception as e:
            logging.debug("Unexpected error attempt %s for %s: %s", attempt, url, e)
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
            continue

    return False


# ---------------------------------------------------------------------------
# YouTube / Twitch (fail closed: online only when live is confirmed)
# ---------------------------------------------------------------------------

def _ytdlp_probe(url):
    """Blocking yt-dlp probe: ``live`` | ``not_live`` | ``error``."""
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logging.debug("yt-dlp probe failed for %s: %s", url, e)
        return "error"
    if not info:
        return "error"
    if info.get("is_live") is True or info.get("live_status") == "is_live":
        return "live"
    return "not_live"


async def _ytdlp_status(url):
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _ytdlp_probe, url), timeout=45)
    except Exception as e:
        logging.debug("yt-dlp probe timed out/failed for %s: %s", url, e)
        return "error"


async def _page_says_live(session, url, platform):
    """Cheap pre-check: does the public page markup explicitly say the stream is live?"""
    pattern = _YT_LIVE_RE if platform == "youtube" else _TWITCH_LIVE_RE
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
        ) as response:
            if response.status != 200:
                return False
            raw = await response.content.read(512 * 1024)
        return bool(pattern.search(raw.decode("utf-8", errors="replace")))
    except Exception as e:
        logging.debug("%s page live check error for %s: %s", platform, url, e)
        return False


async def _youtube_oembed_ok(session, url):
    api_url = f"https://youtube.com/oembed?url={url}&format=json"
    try:
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            return response.status == 200
    except Exception as e:
        logging.debug("YouTube oembed error for %s: %s", url, e)
        return False


async def check_platform_live_status(session, url, require_live=True):
    """True only when a YouTube/Twitch channel is confirmed live.

    ``require_live=False`` (VOD entries) accepts any resolvable video instead.
    Any doubt - markup silent, yt-dlp error, timeout - means ``False``.
    """
    try:
        if "youtube.com" in url or "youtu.be" in url:
            platform = "youtube"
        elif "twitch.tv" in url:
            platform = "twitch"
        else:
            return await check_link_exists(session, url)

        if await _page_says_live(session, url, platform):
            logging.debug("%s %s confirmed live from page markup", platform, url)
            return True

        status = await _ytdlp_status(url)
        if status == "live":
            logging.debug("%s %s confirmed live by yt-dlp", platform, url)
            return True
        if not require_live:
            if status == "not_live":
                return True
            if status == "error" and platform == "youtube":
                return await _youtube_oembed_ok(session, url)
        logging.debug("%s %s not confirmed live (yt-dlp: %s)", platform, url, status)
        return False

    except Exception as e:
        logging.debug("Platform live check error for %s: %s", url, e)
        return False


# ---------------------------------------------------------------------------
# HLS
# ---------------------------------------------------------------------------

def _is_html(content_type, sample):
    base = (content_type or "").split(";")[0].strip().lower()
    if base in _HTML_CONTENT_TYPES:
        return True
    head = (sample or b"").lstrip()[:16].lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _looks_like_media_bytes(sample: bytes) -> bool:
    """True when the first bytes look like TS / fMP4 / common A/V containers."""
    if not sample or len(sample) < 4:
        return False
    # MPEG-TS sync byte every 188 bytes (spot-check first few packets).
    if sample[0] == 0x47:
        ok = 1
        for off in (188, 376, 564):
            if len(sample) > off and sample[off] == 0x47:
                ok += 1
        if ok >= 2 or len(sample) < 188:
            return True
    head = sample[:12]
    if head[4:8] == b"ftyp" or b"moof" in sample[:64] or b"mdat" in sample[:64]:
        return True
    if sample.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf1", b"OggS", b"RIFF", b"\x1aE\xdf\xa3")):
        return True
    # Encrypted HLS (EXT-X-KEY) often returns opaque ciphertext — accept non-HTML bulk.
    if len(sample) >= 256 and not _is_html("", sample):
        return True
    return False


async def probe_segment(session, segment_url, headers=None):
    """Ranged GET the segment and require real media bytes (HEAD alone is not enough).

    Many CDNs answer HEAD 200 for stale or placeholder URIs while GET returns
    empty / HTML / an error page — that used to mark channels online with no
    playable data in the player.
    """
    timeout = _probe_timeout()
    req_headers = {**(headers or HEADERS), "Range": "bytes=0-2047"}
    try:
        async with session.get(
            segment_url,
            timeout=timeout,
            headers=req_headers,
            allow_redirects=True,
        ) as response:
            if response.status not in (200, 206):
                return False, f"Segment HTTP {response.status}"
            ctype = response.headers.get("Content-Type", "")
            if response.headers.get("Content-Length") == "0":
                return False, "Segment is empty"
            sample = await asyncio.wait_for(
                response.content.read(2048), timeout=min(5.0, _PROBE_READ)
            )
            if not sample:
                return False, "Segment is empty"
            if _is_html(ctype, sample):
                return False, "Segment is an HTML page"
            if not _looks_like_media_bytes(sample):
                return False, "Segment is not media data"
            return True, "segment ok"
    except Exception as e:
        logging.debug("Segment GET failed for %s: %s", segment_url, e)
        return False, f"Segment unreachable: {e}"


async def _probe_playlist_segments(session, base_url, segment_uris, headers=None):
    """Probe the newest live segment (what the player pulls). One fallback max."""
    uris = [u.strip() for u in segment_uris if (u or "").strip()]
    if not uris:
        return False, "No media segments found"
    # Newest first; at most one older fallback so dead CDNs fail fast.
    candidates = []
    for u in (uris[-1], uris[0] if len(uris) > 1 else None):
        if u and u not in candidates:
            candidates.append(u)
    last_why = "Segment unreachable"
    for uri in candidates:
        ok, why = await probe_segment(session, urljoin(base_url, uri), headers=headers)
        if ok:
            return True, why
        last_why = why
        # Hard failures: don't burn another round-trip on the same CDN.
        if why.startswith("Segment HTTP") and any(c in why for c in ("404", "410", "403", "401")):
            break
    return False, last_why


async def validate_m3u8_stream(session, url, headers=None):
    """M3U8 validation: playlist parses, has segments, and a recent segment is fetchable.

    Does **not** call ``check_link_exists`` first — that used to triple-retry with
    long sleeps and then re-fetch the same playlist, making batches crawl.
    """
    req_headers = headers or HEADERS
    try:
        logging.debug("Validating M3U8 stream: %s", url)

        try:
            import m3u8
        except ImportError:
            logging.debug("m3u8 package not available, using fallback")
            return await validate_m3u8_stream_fallback(session, url, headers=req_headers)

        async with session.get(url, timeout=_probe_timeout(10), headers=req_headers) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"

            content = await response.text()
            if not content.strip():
                return False, "Empty playlist"
            if not content.lstrip().startswith("#EXTM3U"):
                return False, "Invalid M3U8 format"
            base_url = str(response.url)

        playlist = m3u8.loads(content)

        if playlist.is_variant:
            if not playlist.playlists:
                return False, "Master playlist has no variants"

            best_variant = max(playlist.playlists, key=lambda x: x.stream_info.bandwidth or 0)
            variant_url = urljoin(base_url, best_variant.uri)
            return await validate_media_playlist(session, variant_url, headers=req_headers)

        uris = [(seg.uri or "").strip() for seg in playlist.segments]
        ok, why = await _probe_playlist_segments(session, base_url, uris, headers=req_headers)
        if not ok:
            return False, why

        count = len(playlist.segments)
        if playlist.is_endlist:
            return True, f"VOD stream ({count} segments)"
        return True, f"Live stream ({count} segments)"

    except Exception as e:
        logging.debug("M3U8 validation error for %s: %s", url, e)
        return False, f"Validation error: {str(e)}"


async def validate_m3u8_stream_fallback(session, url, headers=None):
    """Fallback M3U8 validation method."""
    req_headers = headers or HEADERS
    try:
        async with session.get(url, timeout=_probe_timeout(10), headers=req_headers) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"

            content = await response.text()
            if not content.strip():
                return False, "Empty playlist"
            base_url = str(response.url)

        lines = [ln.strip() for ln in content.strip().splitlines()]

        # Check if it's a master playlist or media playlist
        is_master = any('#EXT-X-STREAM-INF:' in line or '#EXT-X-MEDIA:' in line for line in lines)
        is_media = any('#EXTINF:' in line or '#EXT-X-TARGETDURATION:' in line for line in lines)

        if is_master:
            logging.debug("Master playlist detected for %s", url)
            # Parse master playlist for variants
            variants = []
            for i, line in enumerate(lines):
                if line.startswith('#EXT-X-STREAM-INF:'):
                    bandwidth = 0
                    m = re.search(r'BANDWIDTH=(\d+)', line)
                    if m:
                        bandwidth = int(m.group(1))

                    # Get the URL for this variant (next non-comment line)
                    if i + 1 < len(lines) and lines[i + 1] and not lines[i + 1].startswith('#'):
                        variants.append({
                            'url': urljoin(base_url, lines[i + 1]),
                            'bandwidth': bandwidth,
                        })

            if not variants:
                return False, "No valid variants in master playlist"

            # Test the best quality variant
            variants.sort(key=lambda x: x.get('bandwidth', 0), reverse=True)
            return await validate_media_playlist(
                session, variants[0]['url'], headers=req_headers
            )

        elif is_media:
            logging.debug("Media playlist detected for %s", url)
            return await validate_media_playlist(session, url, headers=req_headers)

        else:
            return False, "Invalid M3U8 format"

    except Exception as e:
        logging.debug("Fallback M3U8 validation error for %s: %s", url, e)
        return False, f"Fallback validation error: {str(e)}"


async def validate_media_playlist(session, playlist_url, headers=None):
    """Validate a media playlist has real content: tags, segments, and a fetchable tip segment."""
    req_headers = headers or HEADERS
    try:
        async with session.get(
            playlist_url, timeout=_probe_timeout(8), headers=req_headers
        ) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"

            content = await response.text()
            base_url = str(response.url)

        if not content.strip():
            return False, "Empty media playlist"
        if not content.lstrip().startswith("#EXTM3U"):
            return False, "Invalid M3U8 format"

        lines = [ln.strip() for ln in content.strip().splitlines()]

        if not any('#EXT-X-TARGETDURATION:' in line for line in lines):
            return False, "Missing target duration"

        segments = [line for line in lines if line and not line.startswith('#')]
        ok, why = await _probe_playlist_segments(
            session, base_url, segments, headers=req_headers
        )
        if not ok:
            return False, why

        segment_count = len(segments)
        # ENDLIST often means a looping 24/7 channel, not an on-demand movie.
        # Keep the human string neutral so we don't re-tag it as Videos.
        if any('#EXT-X-ENDLIST' in line for line in lines):
            logging.debug("Finite/looping playlist: %s segments", segment_count)
            return True, f"Live stream ({segment_count} segments)"
        logging.debug("Live stream detected: %s segments", segment_count)
        return True, f"Live stream ({segment_count} segments)"

    except Exception as e:
        logging.debug("Media playlist validation error for %s: %s", playlist_url, e)
        return False, f"Media validation error: {str(e)}"


# ---------------------------------------------------------------------------
# Generic (non-m3u8) streams
# ---------------------------------------------------------------------------

def _media_ct_hint(content_type):
    base = (content_type or "").split(";")[0].strip().lower()
    return (
        base.startswith(_MEDIA_CT_PREFIXES)
        or base in _MEDIA_CT_EXACT
        or "mpegurl" in base
        or "dash+xml" in base
        or base.endswith("+xml") and "mpd" in base
    )


async def validate_generic_stream(session, url, attempts=1, delay=1, headers=None):
    """Validate a non-m3u8 URL: 200/206 AND (media content-type OR a non-empty non-HTML body).

    HLS playlists served from extension-less URLs are handed to the HLS validator.
    """
    req_headers = headers or HEADERS
    last = "URL not accessible"
    timeout = _probe_timeout(10)
    for attempt in range(1, attempts + 1):
        retry = False
        is_hls = False
        try:
            async with session.get(url, timeout=timeout, headers=req_headers) as response:
                if response.status in RETRYABLE_STATUSES:
                    last = f"HTTP {response.status}"
                    retry = True
                elif response.status not in (200, 206):
                    return False, f"HTTP {response.status}"
                else:
                    ctype = response.headers.get("Content-Type", "")
                    try:
                        sample = await asyncio.wait_for(
                            response.content.read(2048), timeout=min(5.0, _PROBE_READ)
                        )
                    except asyncio.TimeoutError:
                        sample = b""
                    hinted = _media_ct_hint(ctype)
                    sample_l = sample.lstrip()

                    if "mpegurl" in ctype.lower() or sample_l.startswith(b"#EXTM3U"):
                        is_hls = True
                    else:
                        if _is_html(ctype, sample):
                            return False, "HTML page, not a stream"
                        if not sample:
                            return False, "Empty stream body"
                        # DASH MPD manifests are XML, not media bytes.
                        if (
                            "dash+xml" in ctype.lower()
                            or b"<MPD" in sample[:512]
                            or b"<mpd" in sample[:512]
                        ):
                            return True, "DASH manifest"
                        if hinted or _looks_like_media_bytes(sample):
                            base = ctype.split(";")[0].strip().lower() or "unknown type"
                            return True, f"Stream ({base})"
                        return False, "Response is not media data"
            if is_hls:
                return await validate_m3u8_stream(session, url, headers=req_headers)
        except asyncio.TimeoutError:
            last, retry = "Timeout", True
        except aiohttp.ClientError as e:
            last, retry = f"Connection error: {e}", True
        except Exception as e:
            logging.debug("Generic stream check error for %s: %s", url, e)
            return False, f"Validation error: {e}"

        if retry and attempt < attempts:
            await asyncio.sleep(delay * attempt)
    return False, last


async def get_stream_metadata(session, url):
    """Extract metadata about what's currently playing."""
    try:
        if '.m3u8' in url.lower():
            # Try to extract title from M3U8
            async with session.get(url, timeout=10, headers=HEADERS) as response:
                if response.status == 200:
                    content = await response.text()

                    # Look for title metadata
                    for line in content.split('\n'):
                        if '#EXT-X-STREAM-TITLE:' in line:
                            title = line.split(':', 1)[1].strip()
                            if title:
                                return title
                        elif '#EXTINF:' in line:
                            # Extract title from EXTINF
                            parts = line.split(',')
                            if len(parts) > 1:
                                title = parts[-1].strip()
                                if title and title != '':
                                    return title

        # For YouTube, try to get video title
        elif 'youtube.com' in url or 'youtu.be' in url:
            video_id = None
            if 'watch?v=' in url:
                video_id = url.split('v=')[1].split('&')[0]
            elif 'youtu.be/' in url:
                video_id = url.split('youtu.be/')[1].split('?')[0]

            if video_id:
                api_url = f"https://youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
                try:
                    async with session.get(api_url, timeout=10) as response:
                        if response.status == 200:
                            data = await response.json()
                            return data.get('title', 'Unknown')
                except Exception:
                    pass

        # For Twitch, try to get stream title
        elif 'twitch.tv' in url:
            channel_name = url.split('twitch.tv/')[1].split('/')[0]
            try:
                async with session.get(f"https://www.twitch.tv/{channel_name}", timeout=10) as response:
                    if response.status == 200:
                        content = await response.text()
                        # Look for stream title in HTML
                        title_match = re.search(r'"title":"([^"]+)"', content)
                        if title_match:
                            return title_match.group(1)
            except Exception:
                pass

        return "Live Stream"

    except Exception as e:
        logging.debug(f"Error extracting metadata for {url}: {e}")
        return "Live Stream"


def _channel_request_headers(channel: Optional[dict] = None) -> dict:
    """Merge playlist EXTVLCOPT headers onto the default stream User-Agent."""
    headers = dict(HEADERS)
    if not isinstance(channel, dict):
        return headers
    ua = (channel.get("http_user_agent") or "").strip()
    ref = (channel.get("http_referrer") or "").strip()
    if ua:
        headers["User-Agent"] = ua
    if ref:
        headers["Referer"] = ref
    return headers


async def validate_channel(session, channel):
    """Asynchronously validate a single channel."""
    try:
        from features.ingest.m3u_parse import looks_like_dash_url, looks_like_hls_url
        from features.storage.normalize import classify_media_type

        logging.debug("Validating channel: %s", channel.get("url"))
        url = channel.get("url") or ""
        req_headers = _channel_request_headers(channel)

        # Keep media_type honest for Live vs Videos (HLS loops stay live).
        classified = classify_media_type(url)
        if classified in ("live", "vod"):
            channel["media_type"] = classified

        # YouTube / Twitch: prefer live; fall back to playable VOD if not live.
        if 'youtube.com' in url or 'youtu.be' in url or 'twitch.tv' in url:
            if await check_platform_live_status(session, url, require_live=True):
                channel['status'] = 'online'
                channel['media_type'] = 'live'
                channel['playing_now'] = await get_stream_metadata(session, url)
                return channel, True
            if await check_platform_live_status(session, url, require_live=False):
                channel['status'] = 'online'
                channel['media_type'] = 'vod'
                channel['playing_now'] = await get_stream_metadata(session, url)
                return channel, True
            channel['status'] = 'offline'
            channel['playing_now'] = "Stream error: not available"
            return channel, False

        # HLS: path .m3u8/.m3u OR query cues (format=m3u8, type=hls, …)
        if looks_like_hls_url(url):
            is_valid, details = await validate_m3u8_stream(session, url, headers=req_headers)
            if is_valid:
                channel['status'] = 'online'
                channel['media_type'] = 'live'
                channel['playing_now'] = details
                return channel, True
            channel['status'] = 'offline'
            channel['playing_now'] = f"Stream error: {details}"
            return channel, False

        # DASH: treat as live catalog entry; probe via generic media (MPD XML).
        if looks_like_dash_url(url):
            is_valid, details = await validate_generic_stream(
                session, url, headers=req_headers
            )
            if is_valid:
                channel['status'] = 'online'
                channel['media_type'] = 'live'
                channel['playing_now'] = details if details.startswith("Stream") else f"DASH ({details})"
                return channel, True
            channel['status'] = 'offline'
            channel['playing_now'] = f"Stream error: {details}"
            return channel, False

        # Non-HTTP schemes (rtmp/rtsp/…) — cannot probe with aiohttp.
        scheme = (url.split("://", 1)[0] or "").lower()
        if scheme and scheme not in ("http", "https"):
            channel['status'] = 'offline'
            channel['playing_now'] = f"Stream error: unsupported scheme ({scheme})"
            return channel, False

        # Progressive / MPEG-TS / opaque HTTP — require real media bytes.
        is_valid, details = await validate_generic_stream(
            session, url, headers=req_headers
        )
        if is_valid:
            channel['status'] = 'online'
            if classified == "unknown":
                channel["media_type"] = (
                    "vod" if classify_media_type(url) == "vod" else "live"
                )
            channel['playing_now'] = await get_stream_metadata(session, url)
            return channel, True
        channel['status'] = 'offline'
        channel['playing_now'] = f"Stream error: {details}"
        return channel, False

    except Exception as e:
        logging.debug("Error validating channel %s: %s", channel.get("url"), e)
        channel['status'] = 'error'
        channel['playing_now'] = f"Validation error: {str(e)}"
        return channel, False


async def process_channels(channels, invalid_links, delay=5):
    """Process channels in batches asynchronously."""
    valid_channels = []
    dead_channels = []

    # Create session with SSL settings to handle certificate issues
    connector = aiohttp.TCPConnector(
        ssl=False,  # Disable SSL verification for problematic certificates
        limit=100,   # Increase connection pool size
        limit_per_host=20
    )

    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0, len(channels), BATCH_SIZE):
            batch = channels[i:i + BATCH_SIZE]
            tasks = [validate_channel(session, channel) for channel in batch if channel['url'] not in invalid_links]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logging.debug("Batch processing error: %s", result)
                    continue

                channel, is_valid = result
                if is_valid:
                    icon_url = download_channel_icon(channel['name'], channel['url'], channel.get('tvg_logo', ''))
                    channel['icon_url'] = icon_url
                    valid_channels.append(channel)
                else:
                    dead_channels.append(channel)

            # Save progress after each batch
            try:
                save_json_atomic(FILES['streams'], valid_channels)
                save_json_atomic(FILES['dead'], dead_channels)

                logging.info(
                    "[found] %s\n[reworking] batch %s\n[how many found] %s\n[how many not found] %s",
                    len(valid_channels),
                    i // BATCH_SIZE + 1,
                    len(valid_channels),
                    len(dead_channels),
                )

            except Exception as e:
                logging.error(f"Error saving batch: {e}")

            await asyncio.sleep(delay)  # play about with this to control processing speed

    return valid_channels, dead_channels
