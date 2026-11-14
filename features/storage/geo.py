"""Country code / name helpers for filter facets and inference."""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
import urllib.parse
from typing import Optional

# ISO 3166-1 alpha-2 (+ a few catalog regions) → English short name.
COUNTRY_CODE_TO_NAME: dict[str, str] = {
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AL": "Albania",
    "AM": "Armenia",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "BR": "Brazil",
    "BY": "Belarus",
    "CA": "Canada",
    "CG": "Congo",
    "CH": "Switzerland",
    "CL": "Chile",
    "CN": "China",
    "CR": "Costa Rica",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EE": "Estonia",
    "EG": "Egypt",
    "ES": "Spain",
    "FI": "Finland",
    "FO": "Faroe Islands",
    "FR": "France",
    "GB": "United Kingdom",
    "GE": "Georgia",
    "GL": "Greenland",
    "GR": "Greece",
    "HK": "Hong Kong",
    "HR": "Croatia",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IQ": "Iraq",
    "IR": "Iran",
    "IS": "Iceland",
    "IT": "Italy",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KR": "South Korea",
    "KW": "Kuwait",
    "KZ": "Kazakhstan",
    "LA": "Laos",
    "LB": "Lebanon",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MA": "Morocco",
    "MD": "Moldova",
    "ME": "Montenegro",
    "MK": "North Macedonia",
    "MO": "Macau",
    "MX": "Mexico",
    "MY": "Malaysia",
    "NG": "Nigeria",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PR": "Puerto Rico",
    "PT": "Portugal",
    "QA": "Qatar",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russia",
    "SA": "Saudi Arabia",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "TD": "Chad",
    "TH": "Thailand",
    "TN": "Tunisia",
    "TR": "Turkey",
    "TT": "Trinidad and Tobago",
    "TW": "Taiwan",
    "UA": "Ukraine",
    "US": "United States",
    "UY": "Uruguay",
    "VE": "Venezuela",
    "VN": "Vietnam",
    "XK": "Kosovo",
    "ZA": "South Africa",
    # Catalog region (not ISO) — Latino package group-title "LA"
    "LATAM": "Latin America",
}

# Extra aliases seen in playlist group-title values.
# Checked before raw ISO 2-letter interpretation so "UK"→GB and "LA"→LATAM.
_COUNTRY_ALIASES: dict[str, str] = {
    "bosnia": "BA",
    "czech": "CZ",
    "czechia": "CZ",
    "czech republic": "CZ",
    "costa": "CR",
    "costa rica": "CR",
    "dominican": "DO",
    "dominican republic": "DO",
    "faroe": "FO",
    "faroe islands": "FO",
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "united": "GB",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "korea": "KR",
    "south korea": "KR",
    "russia": "RU",
    "russian federation": "RU",
    "uae": "AE",
    "日本": "JP",
    "macedonia": "MK",
    "holland": "NL",
    "the netherlands": "NL",
    "chad": "TD",
    "greenland": "GL",
    "hong": "HK",
    "hong kong": "HK",
    "saudi": "SA",
    "saudi arabia": "SA",
    "macau": "MO",
    "macao": "MO",
    "trinidad": "TT",
    "trinidad and tobago": "TT",
    "puerto rico": "PR",
    "laos": "LA",
    # Custom Latino pack uses group-title "LA" (not Laos).
    "la": "LATAM",
    "latam": "LATAM",
    "latin america": "LATAM",
    "latino": "LATAM",
    "latinos": "LATAM",
    "latin": "LATAM",
}

COUNTRY_NAME_TO_CODE: dict[str, str] = {
    name.lower(): code for code, name in COUNTRY_CODE_TO_NAME.items()
}
COUNTRY_NAME_TO_CODE.update(_COUNTRY_ALIASES)

# Placeholders / non-category group titles to keep out of the Category menu.
_NON_CATEGORY_GROUPS = frozenset(
    {
        "undefined",
        "unknown",
        "global",
        "ungrouped",
        "all",
        "other",
        "misc",
        "miscellaneous",
        "vod",
        "country",
        "countries",
        "region",
        "regions",
    }
)

# Default category after peeling a country name out of group-title.
DEFAULT_CATEGORY_AFTER_COUNTRY_SPLIT = "General"


def country_code_to_name(code: Optional[str]) -> str:
    if not code:
        return ""
    c = str(code).strip().upper()
    if c in ("GLOBAL", "XX", "ZZ"):
        return ""
    return COUNTRY_CODE_TO_NAME.get(c, c)


def country_name_to_code(name: Optional[str]) -> Optional[str]:
    """Map a label (name, alias, or ISO code) to a country/region code."""
    if not name:
        return None
    text = str(name).strip()
    if not text:
        return None

    low = text.lower()
    # Aliases first so UK/LA/PR-style playlist labels resolve correctly.
    aliased = COUNTRY_NAME_TO_CODE.get(low)
    if aliased:
        return aliased

    if len(text) == 2 and text.isalpha():
        code = text.upper()
        if code in COUNTRY_CODE_TO_NAME or code == "XK":
            return code
    return None


def is_country_like_group(label: Optional[str]) -> bool:
    """True when a group-title is really a country/region (or empty placeholder)."""
    if not label:
        return True
    text = str(label).strip()
    if not text:
        return True
    low = text.lower()
    if low in _NON_CATEGORY_GROUPS:
        return True
    if country_name_to_code(text):
        return True
    return False


def resolve_country_code(channel: dict, source_url: Optional[str] = None) -> Optional[str]:
    """Best-effort ISO/region country code from channel metadata / source URL."""
    if not isinstance(channel, dict):
        return None

    existing = (channel.get("country") or "").strip()
    if existing and existing.upper() not in ("GLOBAL", "XX", "ZZ", "UNKNOWN", "UNDEFINED"):
        if existing.upper() == "UK":
            return "GB"
        if existing.upper() == "EL":
            return "GR"
        # Prefer alias map (LATAM, etc.) then accept ISO 2-letter as-is.
        mapped = country_name_to_code(existing)
        if mapped:
            return mapped
        if len(existing) == 2 and existing.isalpha():
            return existing.upper()
        if len(existing) <= 5 and existing.isalpha():
            return existing.upper()

    tvg_id = (channel.get("tvg_id") or "").strip().lower()
    if "." in tvg_id:
        suffix = tvg_id.split(".")[-1]
        mapped = country_name_to_code(suffix)
        if mapped:
            return mapped
        if len(suffix) == 2 and suffix.isalpha():
            return suffix.upper()

    if source_url:
        import re

        match = re.search(r"/countries/([a-z]{2})\.m3u", source_url, re.IGNORECASE)
        if match:
            return match.group(1).upper()

    group = (channel.get("group_title") or "").strip()
    mapped = country_name_to_code(group)
    if mapped:
        return mapped

    return None


# ---------------------------------------------------------------------------
# Host (server) geolocation — where the stream is *served from*
# ---------------------------------------------------------------------------

_PLACEHOLDER_COUNTRIES = frozenset({"", "GLOBAL", "XX", "ZZ", "UNKNOWN", "UNDEFINED"})

# Free HTTPS geo endpoint. ``{ip}`` is substituted. Both ``countryCode`` (ip-api
# style) and ``country_code`` (ipwho.is style) responses are understood.
GEO_API_URL = os.environ.get(
    "IPTV_GEO_API_URL", "https://ipwho.is/{ip}?fields=success,country_code"
).strip()
try:
    GEO_API_TIMEOUT_SEC = float(os.environ.get("IPTV_GEO_API_TIMEOUT_SEC", "6"))
except (TypeError, ValueError):
    GEO_API_TIMEOUT_SEC = 6.0
try:
    # ip-api.com free tier allows 45 req/min; stay comfortably below that.
    GEO_MIN_INTERVAL_SEC = float(os.environ.get("IPTV_GEO_MIN_INTERVAL_SEC", "1.5"))
except (TypeError, ValueError):
    GEO_MIN_INTERVAL_SEC = 1.5
# Transient failures (DNS / API errors) are remembered this long in memory only.
_GEO_NEGATIVE_TTL_SEC = 15 * 60.0

_host_country_cache: dict[str, str] = {}
_host_negative_until: dict[str, float] = {}
_ip_country_cache: dict[str, str] = {}
_geo_cache_lock = threading.Lock()
_geo_rate_lock = threading.Lock()
_geo_next_slot = 0.0

_PRIVATE_HOST_SUFFIXES = (".local", ".localdomain", ".internal", ".lan", ".home", ".intranet")


def is_placeholder_country(code: Optional[str]) -> bool:
    """True for empty / GLOBAL-style country values."""
    return (code or "").strip().upper() in _PLACEHOLDER_COUNTRIES


def _ip_is_public(ip_text: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _extract_hostname(url: Optional[str]) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        host = urllib.parse.urlparse(text).hostname or ""
    except ValueError:
        return ""
    return host.strip().strip(".").lower()


def _acquire_geo_slot(block: bool) -> bool:
    """Reserve the next rate-limited API slot; sleeps when ``block`` else refuses."""
    global _geo_next_slot
    with _geo_rate_lock:
        now = time.monotonic()
        wait = _geo_next_slot - now
        if wait > 0 and not block:
            return False
        _geo_next_slot = max(now, _geo_next_slot) + max(0.0, GEO_MIN_INTERVAL_SEC)
    if wait > 0:
        time.sleep(wait)
    return True


def _lookup_ip_country(ip_text: str) -> Optional[str]:
    """Query the geo API. Returns a code, ``"GLOBAL"`` (no country) or None (failure)."""
    import requests

    if not GEO_API_URL:
        return None
    try:
        resp = requests.get(
            GEO_API_URL.replace("{ip}", ip_text),
            timeout=GEO_API_TIMEOUT_SEC,
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    ok = data.get("success")
    if ok is None:
        ok = str(data.get("status") or "").lower() == "success"
    if not ok:
        return None
    code = str(data.get("countryCode") or data.get("country_code") or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        return "GB" if code == "UK" else code
    return "GLOBAL"


def _persistent_store(store=None):
    if store is not None:
        return store
    try:
        from . import db as _db

        return _db._default_store  # peek only — never create a DB from geo
    except Exception:  # noqa: BLE001
        return None


def resolve_server_country_from_url(
    url: Optional[str],
    *,
    block: bool = True,
    allow_network: bool = True,
    store=None,
) -> Optional[str]:
    """Country code of the host serving ``url`` (``"GLOBAL"`` when unknown/private).

    Lookup order: in-memory cache, persistent ``host_geo`` table (when a store
    is available), then DNS + a rate-limited HTTPS geo API call.

    * ``block=False`` never sleeps for the rate limiter; when no API slot is
      free the result is ``None`` (nothing cached) so callers can fall back.
    * ``allow_network=False`` only consults caches and returns ``None`` on a miss.
    * Private / loopback / link-local hosts resolve to ``"GLOBAL"`` without any
      network call. Transient failures return ``None``.
    """
    host = _extract_hostname(url)
    if not host:
        return "GLOBAL"

    ip_literal: Optional[str] = None
    try:
        ipaddress.ip_address(host)
        ip_literal = host
    except ValueError:
        pass

    if ip_literal is not None:
        if not _ip_is_public(ip_literal):
            return "GLOBAL"
    elif (
        "." not in host
        or host == "localhost"
        or host.endswith(_PRIVATE_HOST_SUFFIXES)
    ):
        return "GLOBAL"

    with _geo_cache_lock:
        cached = _host_country_cache.get(host)
        if cached is not None:
            return cached
        if _host_negative_until.get(host, 0.0) > time.monotonic():
            return None

    db_store = _persistent_store(store)
    if db_store is not None:
        try:
            persisted = db_store.get_host_country(host)
        except Exception:  # noqa: BLE001
            persisted = None
        if persisted:
            with _geo_cache_lock:
                _host_country_cache[host] = persisted
            return persisted

    if not allow_network:
        return None
    if not _acquire_geo_slot(block):
        return None

    result: Optional[str] = None
    if ip_literal is not None:
        ip_text: Optional[str] = ip_literal
    else:
        ip_text = None
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError):
            infos = []
        addrs = []
        for info in infos:
            addr = info[4][0]
            if addr not in addrs:
                addrs.append(addr)
        if addrs:
            public = [a for a in addrs if _ip_is_public(a)]
            if public:
                ip_text = public[0]
            else:
                # Resolves only to private space → not geolocatable.
                result = "GLOBAL"

    if result is None and ip_text:
        with _geo_cache_lock:
            result = _ip_country_cache.get(ip_text)
        if result is None:
            result = _lookup_ip_country(ip_text)
            if result is not None:
                with _geo_cache_lock:
                    _ip_country_cache[ip_text] = result

    if result is None:
        with _geo_cache_lock:
            _host_negative_until[host] = time.monotonic() + _GEO_NEGATIVE_TTL_SEC
        return None

    with _geo_cache_lock:
        _host_country_cache[host] = result
    if db_store is not None:
        try:
            db_store.set_host_country(host, result)
        except Exception:  # noqa: BLE001
            pass
    return result
