"""Playlist parsing, source aggregation, master-manifest expansion, and small
icon-name / country / language inference helpers used across ingest and SEO.
"""

import logging
import os
import re
import urllib.parse

import requests

from config import (
    EXPAND_ON_INGEST,
    EXTRA_M3U_URLS_ENV,
    HEADERS,
    MAX_EXPANSION_DEPTH,
    MAX_VARIANTS_PER_CHANNEL,
    SCRAPE_VARIANT_MODE,
)
from .m3u_parse import looks_like_hls_url, parse_m3u_channels
from .sources import (
    ALL_DIRECT_SOURCES,
    EXCEPTION_CHANNELS,
    GLOBAL_SOURCES,
    PUBLIC_NEWS_LIST_SOURCES,
)


def check_channels(m3u_url):
    """Parse M3U playlist and return channel list (quoted attrs + relative URLs)."""
    try:
        response = requests.get(m3u_url, timeout=30, headers=HEADERS)
        if response.status_code != 200:
            logging.debug("Failed to fetch M3U playlist")
            return []
        channels = parse_m3u_channels(response.text, m3u_url)
        channels.extend(EXCEPTION_CHANNELS)
        return channels
    except Exception as e:
        logging.debug("Error parsing M3U playlist: %s", e)
        return []


def channel_icon_safe_name(channel_name):
    return re.sub(r'[^\w\-_\.]', '', (channel_name or 'channel').lower())


def guess_image_ext(content_type, source_url):
    ct = (content_type or '').lower()
    if 'webp' in ct:
        return '.webp'
    if 'jpeg' in ct or 'jpg' in ct:
        return '.jpg'
    if 'gif' in ct:
        return '.gif'
    if 'png' in ct:
        return '.png'
    low = (source_url or '').lower()
    for ext in ('.webp', '.jpg', '.jpeg', '.png', '.gif'):
        if low.split('?')[0].endswith(ext):
            return '.jpg' if ext == '.jpeg' else ext
    return '.png'


# Shared listing art when a channel has no specific logo (never stored per-channel).
GENERIC_ICON_URL = '/static/channel-generic.svg'


def find_local_icon_url(safe_name):
    """Return URL path for an already-cached icon, or None."""
    for ext in ('.png', '.webp', '.jpg', '.jpeg', '.gif'):
        rel = f'static/icons/{safe_name}{ext}'
        if os.path.exists(rel):
            return f'/icons/{safe_name}{ext}'
    return None


def resolve_channel_icon_urls(channel):
    """Return ``(icon_url, generic_icon_url)`` for listing.

    * ``icon_url`` — channel-specific art (local cache, stored icon, or playlist
      ``tvg_logo``). Empty string when none.
    * ``generic_icon_url`` — always the shared site placeholder so every row
      can be listed with at least one image link.
    """
    channel = channel or {}
    name = channel.get('name') or ''
    local = find_local_icon_url(channel_icon_safe_name(name))
    specific = (
        local
        or (channel.get('icon_url') or '').strip()
        or (channel.get('tvg_logo') or '').strip()
    )
    if specific == GENERIC_ICON_URL or specific.endswith('/channel-generic.svg'):
        specific = ''
    return specific, GENERIC_ICON_URL


def infer_country(channel, source_url=None, live=False):
    """Infer country code, preferring where the stream host is located.

    The stream URL's host is geolocated first (cached lookups always; DNS + geo
    API only when ``live=True``, and never blocking on the API rate limit).
    Falls back to channel metadata / source URL, then ``"GLOBAL"``.
    """
    from features.storage.geo import (
        is_placeholder_country,
        resolve_country_code,
        resolve_server_country_from_url,
    )

    channel = channel or {}
    stream_url = channel.get("url") if isinstance(channel, dict) else None
    if stream_url:
        try:
            host_code = resolve_server_country_from_url(
                stream_url, block=False, allow_network=bool(live)
            )
        except Exception:
            host_code = None
        if host_code and not is_placeholder_country(host_code):
            return host_code

    return resolve_country_code(channel, source_url) or "GLOBAL"


def infer_language_code(url):
    """Best-effort language code inference from URL path."""
    try:
        match = re.search(r'/languages/([a-z]{2})\.m3u', url, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    except Exception:
        pass
    return None


def parse_url_list_content(content):
    """Parse plain-text URL list content."""
    urls = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('http://') or line.startswith('https://'):
            urls.append(line)
    return urls


def _repo_root():
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def _resolve_local_playlist(entry: str):
    """Resolve a local playlist path; return absolute path if it exists."""
    raw = (entry or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
        if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            # file:///C:/...
            raw = raw[1:]
    if raw.startswith("http://") or raw.startswith("https://"):
        return None

    candidates = []
    if os.path.isabs(raw):
        candidates.append(raw)
    else:
        for base in (os.getcwd(), _repo_root()):
            candidates.append(os.path.normpath(os.path.join(base, raw)))

    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isfile(path) and path.lower().endswith((".m3u", ".m3u8", ".txt")):
            return path
    return None


def load_playlist_text(source: str):
    """Load playlist text from an http(s) URL or a local file path."""
    source = (source or "").strip()
    if not source:
        return None
    if source.startswith("http://") or source.startswith("https://"):
        response = requests.get(source, timeout=30, headers=HEADERS)
        if response.status_code != 200:
            raise requests.HTTPError(f"HTTP {response.status_code} for {source}")
        return response.text

    path = _resolve_local_playlist(source) or (
        os.path.abspath(source) if os.path.isfile(source) else None
    )
    if not path:
        raise FileNotFoundError(f"Local playlist not found: {source}")
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def get_extra_m3u_sources():
    """Return extra playlist URLs/paths from env, ``playlists.txt``, and ``playlists/``."""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: str) -> None:
        u = (u or "").strip()
        if not u or u.startswith("#"):
            return
        if u.startswith("http://") or u.startswith("https://"):
            key = u
            value = u
        else:
            path = _resolve_local_playlist(u)
            if not path:
                return
            key = os.path.normcase(path)
            value = path
        if key in seen:
            return
        seen.add(key)
        urls.append(value)

    if EXTRA_M3U_URLS_ENV:
        raw = EXTRA_M3U_URLS_ENV.replace("\n", ",")
        for part in raw.split(","):
            _add(part)

    # Simple root file: one URL or local path per line.
    repo_root = _repo_root()
    for candidate in (
        os.path.join(os.getcwd(), "playlists.txt"),
        os.path.join(repo_root, "playlists.txt"),
    ):
        path = os.path.abspath(candidate)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    _add(line)
        except OSError:
            pass
        break

    # Auto-include every .m3u / .m3u8 under playlists/
    for playlists_dir in (
        os.path.join(os.getcwd(), "playlists"),
        os.path.join(repo_root, "playlists"),
    ):
        if not os.path.isdir(playlists_dir):
            continue
        try:
            for name in sorted(os.listdir(playlists_dir)):
                if name.lower().endswith((".m3u", ".m3u8")):
                    _add(os.path.join(playlists_dir, name))
        except OSError:
            pass
        break

    return urls


def parse_variant_attributes(attr_line):
    """Parse #EXT-X-STREAM-INF and #EXT-X-MEDIA attributes."""
    attrs = {}
    for part in attr_line.split(','):
        if '=' not in part:
            continue
        key, value = part.split('=', 1)
        attrs[key.strip().upper()] = value.strip().strip('"')
    return attrs


def expand_master_manifest(channel, content, base_url):
    """Expand master playlist variants into channel entries."""
    variants = []
    pending_stream_inf = None
    media_lang = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith('#EXT-X-MEDIA:'):
            attrs = parse_variant_attributes(line.split(':', 1)[1])
            if attrs.get('TYPE') == 'AUDIO' and attrs.get('GROUP-ID'):
                media_lang[attrs['GROUP-ID']] = attrs.get('LANGUAGE') or attrs.get('NAME')
        elif line.startswith('#EXT-X-STREAM-INF:'):
            pending_stream_inf = parse_variant_attributes(line.split(':', 1)[1])
        elif pending_stream_inf and line and not line.startswith('#'):
            resolved = urllib.parse.urljoin(base_url, line)
            bandwidth = pending_stream_inf.get('BANDWIDTH')
            resolution = pending_stream_inf.get('RESOLUTION')
            audio_group = pending_stream_inf.get('AUDIO')
            audio_lang = media_lang.get(audio_group) if audio_group else None

            v = channel.copy()
            v['url'] = resolved
            v['variant_of'] = channel.get('url')
            v['variant_bandwidth'] = int(bandwidth) if str(bandwidth).isdigit() else None
            v['variant_quality'] = resolution or 'auto'
            v['audio_language'] = (audio_lang or infer_language_code(resolved) or '').upper() or None
            display_suffix = []
            if v['variant_quality']:
                display_suffix.append(v['variant_quality'])
            if v['audio_language']:
                display_suffix.append(v['audio_language'])
            if display_suffix:
                v['display_name'] = f"{v.get('name', 'Channel')} [{' | '.join(display_suffix)}]"
            variants.append(v)
            pending_stream_inf = None

            if len(variants) >= MAX_VARIANTS_PER_CHANNEL:
                break

    if not variants:
        return [channel]

    # best_only mode preserves old behavior but still captures metadata.
    if SCRAPE_VARIANT_MODE == 'best_only':
        variants.sort(key=lambda x: x.get('variant_bandwidth') or 0, reverse=True)
        best = variants[0]
        best['name'] = best.get('display_name', best.get('name'))
        return [best]

    return variants


def maybe_expand_channel(channel, depth=0, *, expand_variants=True):
    """Expand channel recursively for nested channel lists, masters, and variants."""
    if depth >= MAX_EXPANSION_DEPTH:
        return [channel]

    url = channel.get('url', '')
    if not url:
        return [channel]

    # Only expand playlist-like URLs (path, query, or common tokens).
    if not (
        looks_like_hls_url(url)
        or any(token in url.lower() for token in ('.m3u', 'playlist', 'manifest', '.mpd'))
    ):
        return [channel]

    try:
        response = requests.get(url, timeout=10, headers=HEADERS)
        if response.status_code != 200:
            return [channel]

        content = response.text.strip()
        if not content:
            return [channel]

        # Nested *channel* playlist (EXTINF entries) — always collect these.
        if '#EXTINF:' in content:
            children = []
            for entry in parse_m3u_channels(content, url):
                child = dict(channel)
                child.update({k: v for k, v in entry.items() if v not in (None, "")})
                child['url'] = entry.get('url') or child.get('url')
                child.setdefault('variant_of', channel.get('url'))
                child['audio_language'] = (
                    child.get('audio_language')
                    or infer_language_code(child.get('url') or '')
                )
                children.append(child)
            if not children:
                return [channel]
            expanded = []
            for child in children:
                expanded.extend(
                    maybe_expand_channel(child, depth + 1, expand_variants=expand_variants)
                )
            return expanded

        # Master playlist with variant streams (optional — multiplies rows).
        if expand_variants and '#EXT-X-STREAM-INF:' in content:
            variants = expand_master_manifest(channel, content, url)
            expanded = []
            for variant in variants:
                expanded.extend(
                    maybe_expand_channel(variant, depth + 1, expand_variants=expand_variants)
                )
            return expanded[:MAX_VARIANTS_PER_CHANNEL]

        # Nested playlist that points to more links in plain text.
        plain_urls = parse_url_list_content(content)
        if plain_urls and '#EXTINF:' not in content:
            children = []
            for nested_url in plain_urls[:MAX_VARIANTS_PER_CHANNEL]:
                child = channel.copy()
                child['url'] = urllib.parse.urljoin(url, nested_url)
                child['variant_of'] = channel.get('url')
                child['audio_language'] = infer_language_code(child['url'])
                children.append(child)
            expanded = []
            for child in children:
                expanded.extend(
                    maybe_expand_channel(child, depth + 1, expand_variants=expand_variants)
                )
            return expanded[:MAX_VARIANTS_PER_CHANNEL]

        return [channel]
    except Exception:
        return [channel]


def _looks_like_channel_index_playlist(url: str) -> bool:
    """True for M3U *indexes* that list more channels (not a live media playlist)."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        path = (urllib.parse.urlsplit(raw).path or "").lower()
    except Exception:
        path = raw.lower()
    # Media playlists / masters are almost always .m3u8 — don't re-fetch those
    # during every ingest (that would be one GET per stream).
    if path.endswith(".m3u8"):
        return False
    if path.endswith(".m3u"):
        return True
    name = path.rsplit("/", 1)[-1]
    return name in {
        "playlist.m3u",
        "channels.m3u",
        "index.m3u",
        "list.m3u",
        "tv.m3u",
    }


def expand_channel_for_ingest(channel):
    """Expand nested channel indexes; optionally explode HLS variants.

    Default ingest always unwraps ``.m3u`` channel indexes so we don't store a
    playlist URL as a fake channel. Full master-variant explosion stays behind
    ``IPTV_EXPAND_ON_INGEST`` (expensive).
    """
    url = channel.get("url") or ""
    if EXPAND_ON_INGEST:
        return maybe_expand_channel(channel, expand_variants=True)
    if _looks_like_channel_index_playlist(url):
        return maybe_expand_channel(channel, expand_variants=False)
    return [channel]


def check_all_global_sources():
    """Parse ALL global sources and return aggregated channel list with deduplication."""
    all_channels = []
    seen_urls = set()
    source_stats = {}

    logging.info(
        "[ingest]\n[reworking] starting aggregation\n[Priority checks scans] expand=%s",
        "on" if EXPAND_ON_INGEST else "off",
    )

    # Add direct sources (verified + FAST/public)
    logging.debug("Adding direct source pack...")
    all_direct_sources = ALL_DIRECT_SOURCES

    for name, url, group in all_direct_sources:
        if url not in seen_urls:
            channel = {
                'name': name,
                'url': url,
                'tvg_id': f"direct_{name.lower().replace(' ', '_').replace('/', '_').replace('(', '_').replace(')', '_')}",
                'tvg_logo': '',
                'group_title': group,
                'playing_now': 'Not available',
                'status': 'unknown',
                'country': 'GLOBAL'
            }
            all_channels.append(channel)
            seen_urls.add(url)

    logging.debug("Added %s direct channels", len(all_direct_sources))

    # Process all M3U sources
    all_m3u_sources = [GLOBAL_SOURCES["main"]]

    # Add all category sources
    for category, sources in GLOBAL_SOURCES.items():
        if category != "main" and isinstance(sources, list):
            all_m3u_sources.extend(sources)
        elif category != "main" and isinstance(sources, str):
            all_m3u_sources.append(sources)

    # Add extra public news/public-service playlists
    all_m3u_sources.extend(PUBLIC_NEWS_LIST_SOURCES)

    # Add externally configured M3U sources (e.g. Jellyfin/FastChannels output URLs)
    all_m3u_sources.extend(get_extra_m3u_sources())

    logging.debug("Processing %s M3U sources...", len(all_m3u_sources))

    # Process each M3U source (http URL or local .m3u path)
    for i, source_url in enumerate(all_m3u_sources):
        try:
            logging.debug("Processing source %s/%s: %s", i + 1, len(all_m3u_sources), source_url)

            content = load_playlist_text(source_url)
            if content:
                source_channels = []
                plain_links = parse_url_list_content(content)
                if plain_links and '#EXTINF:' not in content:
                    for plain_url in plain_links:
                        if plain_url not in seen_urls:
                            channel_name = urllib.parse.urlparse(plain_url).path.split('/')[-1] or "Stream"
                            plain_channel = {
                                'name': channel_name,
                                'url': plain_url,
                                'tvg_id': '',
                                'tvg_logo': '',
                                'group_title': 'Ungrouped',
                                'playing_now': 'Not available',
                                'status': 'unknown',
                                'country': infer_country({}, source_url),
                                'audio_language': infer_language_code(plain_url),
                            }
                            for ex in expand_channel_for_ingest(plain_channel):
                                ex_url = ex.get('url')
                                if ex_url and ex_url not in seen_urls:
                                    source_channels.append(ex)
                                    seen_urls.add(ex_url)
                else:
                    for entry in parse_m3u_channels(content, source_url):
                        url = (entry.get('url') or '').strip()
                        if not url or url in seen_urls:
                            continue
                        entry.setdefault('playing_now', 'Not available')
                        entry.setdefault('status', 'unknown')
                        entry.setdefault('group_title', 'Unknown')
                        entry['country'] = infer_country(entry, source_url, live=True)
                        entry['audio_language'] = (
                            entry.get('audio_language')
                            or infer_language_code(source_url)
                            or infer_language_code(url)
                        )
                        for ex in expand_channel_for_ingest(entry):
                            ex_url = ex.get('url')
                            if ex_url and ex_url not in seen_urls:
                                source_channels.append(ex)
                                seen_urls.add(ex_url)

                all_channels.extend(source_channels)
                source_stats[source_url] = len(source_channels)
                logging.debug("Added %s channels from %s", len(source_channels), source_url)

            else:
                logging.debug("Failed to load source %s (empty)", source_url)
                source_stats[source_url] = 0

        except Exception as e:
            logging.debug("Error processing source %s: %s", source_url, e)
            source_stats[source_url] = 0

    # Add exception channels
    for channel in EXCEPTION_CHANNELS:
        if channel['url'] not in seen_urls:
            all_channels.append(channel)
            seen_urls.add(channel['url'])

    # Log comprehensive statistics
    logging.info(
        "[ingest]\n[found] %s\n[reworking] %s sources\n[how many found] %s\n[how many not found] 0",
        len(all_channels),
        len(all_m3u_sources),
        len(all_channels),
    )
    logging.debug("Direct sources: %s", len(all_direct_sources))
    logging.debug("M3U sources: %s", len(all_m3u_sources))
    logging.debug("Source breakdown:")
    for source, count in source_stats.items():
        if count > 0:
            logging.debug("  %s: %s channels", source, count)

    # Log category statistics
    category_counts = {}
    for channel in all_channels:
        group = channel.get('group_title', 'Unknown')
        category_counts[group] = category_counts.get(group, 0) + 1

    logging.debug("Category breakdown:")
    for category, count in sorted(category_counts.items()):
        logging.debug("  %s: %s channels", category, count)

    return all_channels


def process_stream_url(url):
    """Process stream URL to detect redirects and extract quality variants."""
    try:
        # Check if URL is a redirect or playlist
        if '.m3u8' in url.lower():
            return process_m3u8_playlist(url)
        else:
            # For direct streams, check for redirects
            return check_redirect_chain(url)
    except Exception as e:
        logging.debug(f"Error processing stream URL {url}: {e}")
        return url


def process_m3u8_playlist(playlist_url):
    """Process M3U8 playlist to extract all quality variants."""
    try:
        response = requests.get(playlist_url, timeout=10, headers=HEADERS)
        if response.status_code == 200:
            content = response.text
            variants = []
            bandwidth = None
            resolution = None
            codecs = None

            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('#EXT-X-STREAM-INF:'):
                    # Extract quality info
                    bandwidth = None
                    resolution = None
                    codecs = None

                    parts = line.split(',')
                    for part in parts:
                        part = part.strip()
                        if part.startswith('BANDWIDTH='):
                            bandwidth = int(part.split('=')[1])
                        elif part.startswith('RESOLUTION='):
                            resolution = part.split('=')[1]
                        elif part.startswith('CODECS='):
                            codecs = part.split('=')[1]

                elif line.startswith('http') and bandwidth:
                    variants.append({
                        'url': line,
                        'bandwidth': bandwidth,
                        'resolution': resolution,
                        'codecs': codecs
                    })

            if variants:
                # Sort by bandwidth (highest quality first)
                variants.sort(key=lambda x: x['bandwidth'], reverse=True)

                # Log all variants for debugging
                for i, variant in enumerate(variants):
                    logging.debug("Quality variant %s", i + 1)

                # Return the highest quality variant
                best_variant = variants[0]
                logging.debug("Selected best quality")
                return best_variant['url']

        return playlist_url  # Fallback to original if processing fails
    except Exception as e:
        logging.debug(f"Error processing M3U8 playlist {playlist_url}: {e}")
        return playlist_url


def check_redirect_chain(url, max_depth=3):
    """Check redirect chain and find final working URL."""
    try:
        current_url = url
        redirect_chain = []

        for depth in range(max_depth):
            # Check if current URL is accessible
            response = requests.head(current_url, timeout=10, headers=HEADERS, allow_redirects=True)

            if response.status_code == 200:
                # Check if we were redirected
                if response.url != current_url:
                    redirect_chain.append({
                        'from': current_url,
                        'to': response.url,
                        'status': response.status_code
                    })
                    logging.debug("Redirect %s", depth + 1)
                    current_url = response.url
                else:
                    # No more redirects, we found the final URL
                    if redirect_chain:
                        logging.debug("Final URL after %s redirects", len(redirect_chain))
                    return current_url
            else:
                logging.debug(f"Redirect chain broken at depth {depth}: {current_url} (status: {response.status_code})")
                return url  # Return last known good URL

        logging.debug(f"Redirect chain too deep, returning: {current_url}")
        return current_url

    except Exception as e:
        logging.debug(f"Error checking redirect chain for {url}: {e}")
        return url
