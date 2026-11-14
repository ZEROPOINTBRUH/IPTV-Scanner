"""Channel icon discovery + on-disk caching.

Keeps the heavier download logic (network calls, yt-dlp fallback) separate from
the lightweight name/URL helpers exposed by :mod:`ingest`.
"""

import logging
from urllib.parse import urlparse

import requests
import yt_dlp

from config import HEADERS
from features.ingest.ingest import channel_icon_safe_name, find_local_icon_url, guess_image_ext


def download_channel_icon(channel_name, channel_url, tvg_logo):
    """Download and cache channel logo under static/icons; serve locally after first scrape."""
    try:
        safe_name = channel_icon_safe_name(channel_name)
        cached_url = find_local_icon_url(safe_name)
        if cached_url:
            return cached_url

        base = f'static/icons/{safe_name}'

        def save_image_bytes(content, content_type, ref_url):
            if not content or len(content) < 80:
                return None
            ext = guess_image_ext(content_type, ref_url)
            path = f'{base}{ext}'
            with open(path, 'wb') as f:
                f.write(content)
            logging.debug(f"Cached icon for {channel_name} as {safe_name}{ext}")
            return f'/icons/{safe_name}{ext}'

        # Source 1: tvg_logo from playlist
        if tvg_logo and tvg_logo != '':
            try:
                response = requests.get(tvg_logo, timeout=12, headers=HEADERS)
                if response.status_code == 200:
                    url_found = save_image_bytes(response.content, response.headers.get('Content-Type'), tvg_logo)
                    if url_found:
                        return url_found
            except Exception as e:
                logging.debug(f"Failed to download tvg_logo for {channel_name}: {e}")

        # Source 2: YouTube thumbnails via yt-dlp
        if 'youtube.com' in channel_url or 'youtu.be' in channel_url:
            icon_url = get_youtube_channel_icon(channel_url)
            if icon_url:
                try:
                    response = requests.get(icon_url, timeout=12, headers=HEADERS)
                    if response.status_code == 200:
                        url_found = save_image_bytes(response.content, response.headers.get('Content-Type'), icon_url)
                        if url_found:
                            return url_found
                except Exception as e:
                    logging.debug(f"Failed to download YouTube icon for {channel_name}: {e}")

        icon_sources = [
            f"https://raw.githubusercontent.com/tv-logo/tv-logos/main/data/logos/{safe_name}.png",
            f"https://raw.githubusercontent.com/tv-logo/tv-logos/main/data/logos/{safe_name}.jpg",
            f"https://raw.githubusercontent.com/iptv-org/epg/master/logos/{safe_name}.png",
            f"https://raw.githubusercontent.com/iptv-org/epg/master/logos/{safe_name}.jpg",
            f"https://raw.githubusercontent.com/fanmixco/IPTV_Logos/master/{safe_name}.png",
            f"https://raw.githubusercontent.com/fanmixco/IPTV_Logos/master/{safe_name}.jpg",
        ]

        for icon_url in icon_sources:
            try:
                response = requests.get(icon_url, timeout=8, headers=HEADERS)
                if response.status_code == 200 and len(response.content) > 100:
                    url_found = save_image_bytes(response.content, response.headers.get('Content-Type'), icon_url)
                    if url_found:
                        return url_found
            except Exception:
                continue

        domain_icon = get_domain_favicon(channel_url)
        if domain_icon:
            try:
                response = requests.get(domain_icon, timeout=8, headers=HEADERS)
                if response.status_code == 200 and len(response.content) > 100:
                    url_found = save_image_bytes(response.content, response.headers.get('Content-Type'), domain_icon)
                    if url_found:
                        return url_found
            except Exception as e:
                logging.debug(f"Failed to download favicon for {channel_name}: {e}")

        try:
            parsed_url = urlparse(channel_url or '')
            domain = parsed_url.netloc
            if domain:
                google_favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
                response = requests.get(google_favicon, timeout=8, headers=HEADERS)
                if response.status_code == 200 and len(response.content) > 80:
                    url_found = save_image_bytes(response.content, response.headers.get('Content-Type'), google_favicon)
                    if url_found:
                        return url_found
                ddg_icon = f"https://icons.duckduckgo.com/ip3/{domain}.ico"
                response = requests.get(ddg_icon, timeout=8, headers=HEADERS)
                if response.status_code == 200 and len(response.content) > 80:
                    url_found = save_image_bytes(response.content, response.headers.get('Content-Type'), ddg_icon)
                    if url_found:
                        return url_found
        except Exception as e:
            logging.debug(f"Fallback favicons failed for {channel_name}: {e}")

        return None

    except Exception as e:
        logging.debug(f"Error downloading icon for {channel_name}: {e}")
        return None


def get_youtube_channel_icon(channel_url):
    """Extract YouTube channel icon using yt-dlp."""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if info and info.get('thumbnail'):
                return info['thumbnail']
    except Exception as e:
        logging.debug(f"Failed to get YouTube icon: {e}")
        return None


def get_domain_favicon(channel_url):
    """Get favicon from channel domain."""
    try:
        parsed_url = urlparse(channel_url)
        domain = parsed_url.netloc

        # Try common favicon locations
        favicon_urls = [
            f"https://{domain}/favicon.ico",
            f"https://{domain}/favicon.png",
            f"https://{domain}/apple-touch-icon.png",
            f"https://{domain}/android-chrome-192x192.png",
        ]

        for favicon_url in favicon_urls:
            try:
                response = requests.head(favicon_url, timeout=3, headers=HEADERS)
                if response.status_code == 200:
                    return favicon_url
            except Exception:
                continue
    except Exception as e:
        logging.debug(f"Failed to get domain favicon: {e}")
        return None


def get_cached_icon_url(channel_name, channel_url, tvg_logo):
    """Get cached icon URL for a channel."""
    return download_channel_icon(channel_name, channel_url, tvg_logo)
