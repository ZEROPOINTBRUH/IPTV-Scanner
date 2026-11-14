"""Static configuration for IPTV Scanner (constants, env-derived settings, shared lock)."""

import os
import threading


BATCH_SIZE = 10  # number of channels to process in each batch.

FILES = {
    "streams": 'jsons/IPTV_STREAMS_FILE.json',
    "dead": 'jsons/DEAD_STREAMS_FILE.json',
    "invalid": 'jsons/INVALID_LINKS_FILE.json',
    "master": 'jsons/MASTER_CACHE_FILE.json',
}

DIRECTORIES = ['templates', 'static', 'static/js', 'static/css', 'static/icons']

WRITE_LOCK = threading.Lock()

SWEEP_INTERVAL_SEC = int(os.environ.get("IPTV_SWEEP_INTERVAL_SEC", str(45 * 60)))
MAX_EXPANSION_DEPTH = int(os.environ.get("IPTV_MAX_EXPANSION_DEPTH", "2"))
MAX_VARIANTS_PER_CHANNEL = int(os.environ.get("IPTV_MAX_VARIANTS_PER_CHANNEL", "8"))
SCRAPE_VARIANT_MODE = os.environ.get("IPTV_VARIANT_MODE", "all_variants").lower()
EXTRA_M3U_URLS_ENV = os.environ.get("IPTV_EXTRA_M3U_URLS", "")
IPTV_PUBLIC_BASE_URL = os.environ.get("IPTV_PUBLIC_BASE_URL", "").strip().rstrip("/")
IPTV_SITE_NAME = os.environ.get("IPTV_SITE_NAME", "IPTV Scanner").strip() or "IPTV Scanner"
# Optional: require ?token=SECRET on /jellyfin/live.m3u and friends when set.
IPTV_PLAYLIST_SECRET = os.environ.get("IPTV_PLAYLIST_SECRET", "").strip()
# Password required for HTTP catalog purge (More → Purge). Falls back to
# IPTV_PLAYLIST_SECRET when unset. If both are empty, /admin/purge is disabled.
IPTV_ADMIN_PASSWORD = (
    os.environ.get("IPTV_ADMIN_PASSWORD", "").strip() or IPTV_PLAYLIST_SECRET
)
# Fetch every playlist URL during M3U ingest to expand master manifests (very slow; default off).
EXPAND_ON_INGEST = os.environ.get("IPTV_EXPAND_ON_INGEST", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# Phase 2: SQLite storage + network binding + XMLTV + checker cadence.
IPTV_DB_PATH = os.environ.get("IPTV_DB_PATH", os.path.join("data", "iptv.db"))
IPTV_BIND_HOST = os.environ.get("IPTV_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
try:
    IPTV_PORT = int(os.environ.get("IPTV_PORT", "40006"))
except (TypeError, ValueError):
    IPTV_PORT = 40006
IPTV_XMLTV_URL = os.environ.get("IPTV_XMLTV_URL", "").strip()
try:
    IPTV_ACTIVE_CHECK_INTERVAL_SEC = int(
        os.environ.get("IPTV_ACTIVE_CHECK_INTERVAL_SEC", str(15 * 60))
    )
except (TypeError, ValueError):
    IPTV_ACTIVE_CHECK_INTERVAL_SEC = 15 * 60
try:
    IPTV_DEAD_CHECK_INTERVAL_SEC = int(
        os.environ.get("IPTV_DEAD_CHECK_INTERVAL_SEC", str(6 * 60 * 60))
    )
except (TypeError, ValueError):
    IPTV_DEAD_CHECK_INTERVAL_SEC = 6 * 60 * 60


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Stream proxy limits: cap simultaneous upstream pulls and bound connect time.
IPTV_PROXY_MAX_CONCURRENT = max(1, _env_int("IPTV_PROXY_MAX_CONCURRENT", 32))
IPTV_PROXY_CONNECT_TIMEOUT = max(0.5, _env_float("IPTV_PROXY_CONNECT_TIMEOUT", 8.0))
# Honour X-Forwarded-For / X-Real-IP only when running behind a trusted reverse proxy.
IPTV_TRUST_PROXY = os.environ.get("IPTV_TRUST_PROXY", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
# Seconds a claimed health-check row stays leased before another worker may take it.
IPTV_CHECK_LEASE_SEC = max(1.0, _env_float("IPTV_CHECK_LEASE_SEC", 300.0))
# Thread pool size when serving with Waitress (ignored by the Flask dev server).
# Keep well above IPTV_PROXY_MAX_CONCURRENT so HLS + guide API don't starve.
IPTV_WAITRESS_THREADS = max(1, _env_int("IPTV_WAITRESS_THREADS", 48))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# Stream proxy must NOT send Upgrade-Insecure-Requests — many IPTV origins are
# plain HTTP only and will 301/302 to a broken HTTPS URL if that header is set.
STREAM_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Public streams API (VRChat / Unity / etc.) — no key, paginated, lightly rate-limited.
API_PUBLIC_PAGE_SIZE = 50
try:
    API_PUBLIC_MIN_INTERVAL_SEC = float(os.environ.get("IPTV_API_MIN_INTERVAL_SEC", "2"))
except (TypeError, ValueError):
    API_PUBLIC_MIN_INTERVAL_SEC = 2.0
try:
    API_PUBLIC_BURST = int(os.environ.get("IPTV_API_BURST", "3"))
except (TypeError, ValueError):
    API_PUBLIC_BURST = 3
# Hard cap for the ``limit`` query param on /api/v1/channels (default page stays 50).
API_PUBLIC_MAX_LIMIT = max(API_PUBLIC_PAGE_SIZE, _env_int("IPTV_API_MAX_LIMIT", 200))
# Looser buckets for the interactive guide (/channels) and the stream/image proxy.
# Tokens refill one per interval, up to ``burst`` (per client IP, per scope).
IPTV_CHANNELS_MIN_INTERVAL_SEC = max(0.01, _env_float("IPTV_CHANNELS_MIN_INTERVAL_SEC", 0.25))
IPTV_CHANNELS_BURST = max(1, _env_int("IPTV_CHANNELS_BURST", 20))
IPTV_PROXY_MIN_INTERVAL_SEC = max(0.01, _env_float("IPTV_PROXY_MIN_INTERVAL_SEC", 0.05))
IPTV_PROXY_BURST = max(1, _env_int("IPTV_PROXY_BURST", 200))
# Playback reports / viewer heartbeats (POST) per client IP.
IPTV_REPORT_MIN_INTERVAL_SEC = max(0.01, _env_float("IPTV_REPORT_MIN_INTERVAL_SEC", 0.5))
IPTV_REPORT_BURST = max(1, _env_int("IPTV_REPORT_BURST", 20))
# Catalog purge attempts (wrong password + spam). Burst then slow refill.
IPTV_ADMIN_PURGE_MIN_INTERVAL_SEC = max(1.0, _env_float("IPTV_ADMIN_PURGE_MIN_INTERVAL_SEC", 60.0))
IPTV_ADMIN_PURGE_BURST = max(1, _env_int("IPTV_ADMIN_PURGE_BURST", 3))
# Cooldown after a successful purge (seconds) per client IP.
IPTV_ADMIN_PURGE_COOLDOWN_SEC = max(30, _env_int("IPTV_ADMIN_PURGE_COOLDOWN_SEC", 600))

# Kind support reminder (shown in public JSON + /api docs). Optional override via env.
SUPPORT_MESSAGE = (
    os.environ.get("IPTV_SUPPORT_MESSAGE")
    or "Please be kind and consider supporting this project :3"
).strip()
SUPPORT_KOFI = os.environ.get("IPTV_SUPPORT_KOFI", "https://ko-fi.com/zeropointbruh").strip()
SUPPORT_PAYPAL = os.environ.get("IPTV_SUPPORT_PAYPAL", "wegj1@hotmail.com").strip()
SUPPORT_CASHAPP = os.environ.get("IPTV_SUPPORT_CASHAPP", "$wegj1").strip()

# Self-host / upstream project (shown in /api docs + banners).
SELF_HOST_REPO = os.environ.get(
    "IPTV_SELF_HOST_REPO",
    "https://github.com/ZEROPOINTBRUH/IPTV-Scanner",
).strip().rstrip("/")

