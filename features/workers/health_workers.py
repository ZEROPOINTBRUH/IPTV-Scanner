"""Phase 2 dual-worker health-check logic.

This module owns the *actual* work that ``fw_tasks`` schedules. Splitting
health from ingest means we can retire the monolithic ``workers.py`` sweep
that historically wiped the catalog on every cycle.

Workers
-------
* :func:`run_active_health_batch` — one pass over ``online``/``pending``/
  ``unknown`` channels claimed from :class:`channel_store.ChannelStore`,
  validated with :func:`validate.validate_channel`, upserted back into the
  catalog with a normalised ``fail_reason``.
* :func:`run_dead_revival_batch` — one pass over ``offline``/``error``
  channels. Any that come back alive are promoted; the streams catalog is
  never wiped, only upserted.
* :func:`run_ingest_sources` — full scrape of every global source, merged
  into the store via ``upsert_from_ingest``.
* :func:`run_discover_playlist` — depth-limited nested-playlist expansion
  for a single URL (queue-based, respects :data:`config.MAX_EXPANSION_DEPTH`).
* :func:`run_icon_prefetch_batch` — fills in missing on-disk logos.
* :func:`run_epg_refresh` — stub placeholder for a future EPG job.

The looped variants (``*_loop``) simply call the batch runner on a fixed
cadence and exist so the scheduler can hand out either shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
from functools import partial
from typing import Iterable, Optional

import aiohttp
import requests

from . import task_queue
from features.storage.db import (
    ACTIVE_STATUSES,
    DEAD_STATUSES,
    ChannelStore,
    get_default_store,
)
from config import (
    BATCH_SIZE,
    HEADERS,
    MAX_EXPANSION_DEPTH,
    MAX_VARIANTS_PER_CHANNEL,
)
from features.icons.icons import download_channel_icon
from features.ingest.ingest import (
    channel_icon_safe_name,
    check_all_global_sources,
    expand_master_manifest,
    find_local_icon_url,
    infer_country,
    infer_language_code,
    parse_url_list_content,
)
from features.validate.validate import validate_channel


log = logging.getLogger("health_workers")


# ---------------------------------------------------------------------------
# Tunables (env-first so operators can override without editing config.py)
# ---------------------------------------------------------------------------

# Cadences: health runs again soon after each batch finishes (schedule_repeat
# sleeps *after* completion). Keep this short so ~12k pending rows drain.
ACTIVE_HEALTH_INTERVAL_SEC = int(os.environ.get("IPTV_ACTIVE_HEALTH_INTERVAL_SEC", "20"))
DEAD_REVIVAL_INTERVAL_SEC = int(os.environ.get("IPTV_DEAD_REVIVAL_INTERVAL_SEC", "120"))
INGEST_INTERVAL_SEC = int(os.environ.get("IPTV_INGEST_INTERVAL_SEC", str(45 * 60)))
ICON_PREFETCH_INTERVAL_SEC = int(os.environ.get("IPTV_ICON_PREFETCH_INTERVAL_SEC", "90"))
EPG_REFRESH_INTERVAL_SEC = int(os.environ.get("IPTV_EPG_REFRESH_INTERVAL_SEC", str(6 * 60 * 60)))

GEO_BACKFILL_INTERVAL_SEC = int(os.environ.get("IPTV_GEO_BACKFILL_INTERVAL_SEC", "180"))
GEO_BACKFILL_BATCH_SIZE = int(os.environ.get("IPTV_GEO_BACKFILL_BATCH", "40"))

ACTIVE_HEALTH_BATCH_SIZE = int(os.environ.get("IPTV_ACTIVE_HEALTH_BATCH", "40"))
DEAD_REVIVAL_BATCH_SIZE = int(os.environ.get("IPTV_DEAD_REVIVAL_BATCH", str(BATCH_SIZE)))
# Parallel stream probes per batch. Higher = faster drain; still capped so
# Waitress / DNS are not starved.
VALIDATE_CONCURRENCY = max(1, int(os.environ.get("IPTV_VALIDATE_CONCURRENCY", "12")))
ICON_PREFETCH_BATCH_SIZE = int(os.environ.get("IPTV_ICON_PREFETCH_BATCH", "10"))


# ---------------------------------------------------------------------------
# aiohttp session helper (mirrors validate.process_channels)
# ---------------------------------------------------------------------------

def _new_session() -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=max(64, VALIDATE_CONCURRENCY * 4),
        limit_per_host=8,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    return aiohttp.ClientSession(connector=connector)


# ---------------------------------------------------------------------------
# fail_reason classification
# ---------------------------------------------------------------------------

_STATUS_HTTP_PATTERNS = (
    ("http 400", "http_400"),
    ("http 401", "http_401"),
    ("http 403", "http_403"),
    ("http 404", "http_404"),
    ("http 410", "http_410"),
    ("http 429", "http_429"),
    ("http 500", "http_5xx"),
    ("http 502", "http_5xx"),
    ("http 503", "http_5xx"),
    ("http 504", "http_5xx"),
)


def classify_fail_reason(channel: dict) -> Optional[str]:
    """Return a short ``fail_reason`` slug based on ``validate_channel`` output.

    ``validate_channel`` stores a human-readable string in ``playing_now``
    on failure (``"Stream error: HTTP 404"`` etc). We parse that instead of
    duplicating the network call — it keeps behaviour aligned with the
    legacy validator while still giving downstream code a stable slug.
    """
    playing = str(channel.get("playing_now") or "").lower()
    status = str(channel.get("status") or "").lower()

    if status == "online":
        return None

    for needle, slug in _STATUS_HTTP_PATTERNS:
        if needle in playing:
            return slug

    if "timeout" in playing or "timed out" in playing:
        return "timeout"
    if "ssl" in playing or "certificate" in playing:
        return "ssl_error"
    if "dns" in playing or "cannot connect to host" in playing or "name or service" in playing:
        return "dns_error"
    if "not accessible" in playing or "unreachable" in playing:
        return "unreachable"
    if "empty playlist" in playing or "no media segments" in playing:
        return "empty_playlist"
    if "invalid m3u8" in playing or "missing target duration" in playing:
        return "stream_invalid"
    if "master playlist has no variants" in playing:
        return "no_variants"
    if "validation error" in playing:
        return "validation_error"
    if status == "error":
        return "error"
    return "unknown"


# ---------------------------------------------------------------------------
# Batch execution shared by the two worker faces
# ---------------------------------------------------------------------------

def _short(text: object, width: int = 32) -> str:
    s = str(text or "").strip()
    return s if len(s) <= width else s[: width - 1] + "…"


# Hard ceiling per channel so one hung CDN cannot pin a concurrency slot.
_VALIDATE_CHANNEL_BUDGET_SEC = float(os.environ.get("IPTV_VALIDATE_CHANNEL_BUDGET_SEC", "18"))


async def _validate_batch(
    channels: list[dict],
) -> list[tuple[dict, bool, Optional[str]]]:
    """Validate ``channels`` with bounded concurrency; returns ``(channel, ok, reason)``."""
    if not channels:
        return []
    sem = asyncio.Semaphore(VALIDATE_CONCURRENCY)
    results: list[tuple[dict, bool, Optional[str]]] = []

    async with _new_session() as session:

        async def _one(ch: dict):
            async with sem:
                try:
                    return await asyncio.wait_for(
                        validate_channel(session, dict(ch)),
                        timeout=_VALIDATE_CHANNEL_BUDGET_SEC,
                    )
                except asyncio.TimeoutError:
                    channel = dict(ch)
                    channel["status"] = "offline"
                    channel["playing_now"] = "Stream error: validation timeout"
                    return channel, False

        raw = await asyncio.gather(
            *[_one(ch) for ch in channels],
            return_exceptions=True,
        )

    for orig, item in zip(channels, raw):
        if isinstance(item, Exception):
            channel = dict(orig)
            channel["status"] = "error"
            channel["playing_now"] = f"Validation error: {item}"
            results.append((channel, False, classify_fail_reason(channel)))
            continue
        channel, ok = item
        reason = classify_fail_reason(channel) if not ok else None
        results.append((channel, ok, reason))
    return results


def _results_to_dicts(
    results: list[tuple[dict, bool, Optional[str]]],
) -> list[dict]:
    """Convert ``(channel, ok, reason)`` tuples into store upsert dicts."""
    out: list[dict] = []
    for channel, ok, reason in results:
        row = dict(channel)
        if ok:
            row["status"] = "online"
            row["fail_reason"] = None
            row["fail_delta"] = 0
        else:
            row["status"] = row.get("status") if row.get("status") in ("offline", "error") else "offline"
            row["fail_reason"] = reason
            row["fail_delta"] = 1
        out.append(row)
    return out


def _summarize_results(
    results: list[tuple[dict, bool, Optional[str]]],
) -> dict:
    promoted = demoted = still_online = still_dead = 0
    for _ch, ok, _reason in results:
        if ok:
            still_online += 1
            promoted += 1  # treated as healthy after check
        else:
            demoted += 1
            still_dead += 1
    return {
        "promoted": promoted,
        "demoted": demoted,
        "still_online": still_online,
        "still_dead": still_dead,
        "updated": len(results),
    }


def _format_batch_lines(
    results: list[tuple[dict, bool, Optional[str]]],
    *,
    mark_alive: str = "✓",
    mark_dead: str = "✗",
) -> list[str]:
    lines: list[str] = []
    for channel, ok, reason in results:
        name = _short(channel.get("name") or channel.get("url"), 42)
        if ok:
            detail = _short(channel.get("playing_now") or "online", 32)
            lines.append(f"{mark_alive} {name}  ({detail})")
        else:
            lines.append(f"{mark_dead} {name}  [{reason or 'unknown'}]")
    return lines


# ---------------------------------------------------------------------------
# Active health worker
# ---------------------------------------------------------------------------

async def run_active_health_batch(
    store: Optional[ChannelStore] = None,
    *,
    limit: Optional[int] = None,
) -> dict:
    """One pass of the active-health worker.

    Claims a batch of ``online``/``pending``/``unknown`` channels, revalidates
    them, and upserts the results. Returns a summary dict suitable for
    metrics/logging.
    """
    store = store or get_default_store()
    limit = limit or ACTIVE_HEALTH_BATCH_SIZE
    batch = store.claim_active_batch(limit)
    if not batch:
        log.debug("active-health: nothing to check")
        try:
            store.decay_trend_scores()
        except Exception:  # noqa: BLE001
            log.debug("active-health: trend decay skipped", exc_info=True)
        return {"claimed": 0}

    started = time.perf_counter()
    results = await _validate_batch(batch)
    payload = _results_to_dicts(results)
    await asyncio.to_thread(store.update_channel_results, payload)
    try:
        await asyncio.to_thread(store.decay_trend_scores)
    except Exception:  # noqa: BLE001
        log.debug("active-health: trend decay skipped", exc_info=True)
    summary = _summarize_results(results)
    summary["claimed"] = len(batch)
    summary["elapsed_sec"] = round(time.perf_counter() - started, 2)

    found = int(summary.get("still_online", 0) or 0)
    not_found = int(summary.get("demoted", 0) or 0)
    try:
        catalog_found = store.count_channels(status="online", exclude_test=True)
        catalog_not_found = max(
            0,
            store.count_channels(exclude_test=True) - catalog_found,
        )
        priority_checks = store.count_priority_checks(ACTIVE_STATUSES)
        how_many_left = store.count_check_queue(ACTIVE_STATUSES)
    except Exception:  # noqa: BLE001
        catalog_found = found
        catalog_not_found = not_found
        priority_checks = 0
        how_many_left = 0

    task_queue.log_check_report(
        "active-health",
        found=found,
        not_found=not_found,
        reworking=len(batch),
        how_many_found=catalog_found,
        how_many_not_found=catalog_not_found,
        priority_checks=priority_checks,
        how_many_left=how_many_left,
        elapsed_sec=summary["elapsed_sec"],
    )
    return summary


async def run_verify_channel(
    url: str,
    store: Optional[ChannelStore] = None,
    *,
    reason: str = "",
) -> dict:
    """Re-validate one channel right now (client reports: alive / no_playback / slow_load).

    Unlike a batch run this does not claim a lease; it simply validates the row
    and writes the result back. A failed check demotes the channel the same way
    a health batch would (``fail_delta`` +1, status offline/error).
    """
    store = store or get_default_store()
    channel = store.get_channel(url)
    if not channel:
        return {"verified": False, "reason": "unknown channel"}
    results = await _validate_batch([channel])
    await asyncio.to_thread(store.update_channel_results, _results_to_dicts(results))
    _ch, ok, fail = results[0]
    log.debug("verify %s (%s): %s", url, reason or "-", "online" if ok else fail)
    return {"verified": True, "online": bool(ok), "fail_reason": fail, "trigger": reason}


async def active_health_loop(
    interval_sec: Optional[float] = None,
    store: Optional[ChannelStore] = None,
) -> None:
    """Forever-loop wrapper around :func:`run_active_health_batch`."""
    interval = float(interval_sec if interval_sec is not None else ACTIVE_HEALTH_INTERVAL_SEC)
    while True:
        try:
            await run_active_health_batch(store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("active_health_loop: batch failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Dead revival worker
# ---------------------------------------------------------------------------

async def run_dead_revival_batch(
    store: Optional[ChannelStore] = None,
    *,
    limit: Optional[int] = None,
) -> dict:
    """One pass of the dead-revival worker.

    Any channel that comes back alive is promoted into the streams catalog
    via :meth:`ChannelStore.update_channel_results`; the dead file is only
    ever upserted, never wiped.
    """
    store = store or get_default_store()
    limit = limit or DEAD_REVIVAL_BATCH_SIZE
    batch = store.claim_dead_batch(limit)
    if not batch:
        log.debug("dead-revival: nothing to retry")
        return {"claimed": 0}

    started = time.perf_counter()
    results = await _validate_batch(batch)
    # Force every survivor to "online" so update_channel_results promotes them.
    for ch, ok, _reason in results:
        if ok:
            ch["status"] = "online"
    await asyncio.to_thread(store.update_channel_results, _results_to_dicts(results))
    summary = _summarize_results(results)
    summary["claimed"] = len(batch)
    summary["elapsed_sec"] = round(time.perf_counter() - started, 2)

    found = int(summary.get("promoted", 0) or 0)
    not_found = int(summary.get("still_dead", 0) or 0)
    try:
        catalog_found = store.count_channels(status="online", exclude_test=True)
        catalog_not_found = max(
            0,
            store.count_channels(exclude_test=True) - catalog_found,
        )
        priority_checks = store.count_priority_checks(DEAD_STATUSES)
        how_many_left = store.count_check_queue(DEAD_STATUSES)
    except Exception:  # noqa: BLE001
        catalog_found = found
        catalog_not_found = not_found
        priority_checks = 0
        how_many_left = 0

    task_queue.log_check_report(
        "dead-revival",
        found=found,
        not_found=not_found,
        reworking=len(batch),
        how_many_found=catalog_found,
        how_many_not_found=catalog_not_found,
        priority_checks=priority_checks,
        how_many_left=how_many_left,
        elapsed_sec=summary["elapsed_sec"],
    )
    return summary


async def dead_revival_loop(
    interval_sec: Optional[float] = None,
    store: Optional[ChannelStore] = None,
) -> None:
    interval = float(interval_sec if interval_sec is not None else DEAD_REVIVAL_INTERVAL_SEC)
    while True:
        try:
            await run_dead_revival_batch(store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("dead_revival_loop: batch failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Ingest — full scrape merged via upsert_from_ingest
# ---------------------------------------------------------------------------

_INGEST_LOCK = asyncio.Lock()


async def run_ingest_sources(store: Optional[ChannelStore] = None) -> dict:
    """Full source scrape, upserted into the store without wiping.

    Delegates to :func:`ingest.check_all_global_sources` (blocking, so we
    hop to a worker thread) and then merges the result via
    :meth:`ChannelStore.upsert_from_ingest`.

    Concurrent callers share one lock so a second kick (``/scan``, stacked
    cadence, etc.) cannot double-scrape and freeze SQLite.
    """
    if _INGEST_LOCK.locked():
        log.info("ingest_sources: already running; skipping overlapping kick")
        return {
            "new": 0,
            "updated": 0,
            "skipped": 0,
            "scraped": 0,
            "elapsed_sec": 0,
            "skipped_busy": True,
        }

    async with _INGEST_LOCK:
        store = store or get_default_store()
        started = time.perf_counter()
        channels = await asyncio.to_thread(check_all_global_sources)
        # Upsert off the event loop — a full catalog write must not freeze
        # schedule_repeat / health while SQLite commits chunk by chunk.
        summary = await asyncio.to_thread(store.upsert_from_ingest, channels)
        summary["scraped"] = len(channels)
        summary["elapsed_sec"] = round(time.perf_counter() - started, 2)

        task_queue.log_worker_batch(
            "ingest-sources",
            [
                f"[scraped] {summary['scraped']} channels",
                f"[new] {summary['new']}",
                f"[updated] {summary['updated']}",
                f"[skipped] {summary['skipped']}",
                f"[elapsed] {summary['elapsed_sec']}s",
            ],
            limit=5,
        )
        return summary


async def ingest_sources_loop(
    interval_sec: Optional[float] = None,
    store: Optional[ChannelStore] = None,
) -> None:
    interval = float(interval_sec if interval_sec is not None else INGEST_INTERVAL_SEC)
    while True:
        try:
            await run_ingest_sources(store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("ingest_sources_loop: cycle failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Playlist discovery — nested queue with depth limit
# ---------------------------------------------------------------------------

def _fetch_text(url: str, timeout: int = 20) -> Optional[str]:
    try:
        resp = requests.get(url, timeout=timeout, headers=HEADERS)
    except Exception as exc:  # noqa: BLE001
        log.debug("discover: fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.debug("discover: %s → HTTP %s", url, resp.status_code)
        return None
    return resp.text or ""


def _parse_m3u_channels(content: str, source_url: str) -> list[dict]:
    """Parse Extended-M3U with quoted attrs + relative URLs (shared parser)."""
    from features.ingest.m3u_parse import parse_m3u_channels

    channels = []
    for entry in parse_m3u_channels(content, source_url):
        ch = dict(entry)
        ch.setdefault("playing_now", "Not available")
        ch.setdefault("status", "unknown")
        ch["country"] = infer_country(ch, source_url, live=True)
        ch["audio_language"] = (
            ch.get("audio_language")
            or infer_language_code(source_url)
            or infer_language_code(ch.get("url") or "")
        )
        channels.append(ch)
    return channels


def _expand_discovered(seed_url: str) -> list[dict]:
    """Depth-limited breadth-first walk of a playlist and its nested manifests."""
    discovered: list[dict] = []
    seen_urls: set[str] = set()
    queue: list[tuple[str, int, dict]] = [(seed_url, 0, {})]

    while queue:
        url, depth, parent_ctx = queue.pop(0)
        if url in seen_urls or depth > MAX_EXPANSION_DEPTH:
            continue
        seen_urls.add(url)
        content = _fetch_text(url)
        if content is None:
            continue

        if "#EXTINF:" in content:
            for ch in _parse_m3u_channels(content, url):
                ch_url = ch.get("url")
                if not ch_url or ch_url in seen_urls:
                    continue
                seen_urls.add(ch_url)
                for k, v in parent_ctx.items():
                    ch.setdefault(k, v)
                discovered.append(ch)
            continue

        if "#EXT-X-STREAM-INF:" in content:
            parent = parent_ctx or {"name": urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "Stream"}
            parent = dict(parent)
            parent.setdefault("url", url)
            for variant in expand_master_manifest(parent, content, url)[:MAX_VARIANTS_PER_CHANNEL]:
                v_url = variant.get("url")
                if not v_url or v_url in seen_urls:
                    continue
                variant.setdefault("status", "unknown")
                variant.setdefault("playing_now", "Not available")
                variant.setdefault("country", infer_country(variant, url, live=True))
                seen_urls.add(v_url)
                discovered.append(variant)
            continue

        plain = parse_url_list_content(content)
        if plain:
            for nested in plain[:MAX_VARIANTS_PER_CHANNEL]:
                resolved = urllib.parse.urljoin(url, nested)
                if resolved in seen_urls:
                    continue
                nested_ctx = dict(parent_ctx)
                nested_ctx.setdefault("name", urllib.parse.urlparse(resolved).path.rsplit("/", 1)[-1] or "Stream")
                queue.append((resolved, depth + 1, nested_ctx))
            continue

        log.debug("discover: %s did not look like a playlist", url)

    return discovered


async def run_discover_playlist(
    seed_url: str,
    store: Optional[ChannelStore] = None,
) -> dict:
    """Discover channels from a single playlist URL and upsert them.

    Wraps the blocking BFS walk in :func:`asyncio.to_thread` so it plays
    nicely with the async worker pool.
    """
    if not seed_url:
        return {"scraped": 0, "new": 0, "updated": 0, "skipped": 0}

    store = store or get_default_store()
    started = time.perf_counter()
    channels = await asyncio.to_thread(_expand_discovered, seed_url)
    summary = store.upsert_from_ingest(channels)
    summary["scraped"] = len(channels)
    summary["seed"] = seed_url
    summary["elapsed_sec"] = round(time.perf_counter() - started, 2)

    task_queue.log_worker_batch(
        "discover-playlist",
        [
            f"seed      {_short(seed_url, 48)}",
            f"scraped   {summary['scraped']}",
            f"new       {summary['new']}",
            f"updated   {summary['updated']}",
            f"elapsed   {summary['elapsed_sec']}s",
        ],
        limit=5,
    )
    return summary


# ---------------------------------------------------------------------------
# Icon prefetch
# ---------------------------------------------------------------------------

async def run_icon_prefetch_batch(
    store: Optional[ChannelStore] = None,
    *,
    limit: Optional[int] = None,
) -> dict:
    """Download logos for up to ``limit`` channels missing a local icon."""
    store = store or get_default_store()
    limit = limit or ICON_PREFETCH_BATCH_SIZE

    needing: list[dict] = []
    page = 1
    while len(needing) < limit:
        result = store.list_channels(
            page=page,
            limit=min(200, max(limit * 2, 50)),
            include_test=True,
            sort="name",
            sort_dir="asc",
        )
        batch = result.get("channels") or []
        if not batch:
            break
        for ch in batch:
            status = (ch.get("status") or "").lower()
            if status in DEAD_STATUSES:
                continue
            if find_local_icon_url(channel_icon_safe_name(ch.get("name", ""))):
                continue
            needing.append(ch)
            if len(needing) >= limit:
                break
        if not result.get("has_more"):
            break
        page += 1

    if not needing:
        log.debug("icon-prefetch: nothing to fetch")
        return {"fetched": 0, "skipped": 0}

    loop = asyncio.get_running_loop()
    fetched = 0
    for ch in needing:
        try:
            icon_url = await loop.run_in_executor(
                None,
                partial(
                    download_channel_icon,
                    ch.get("name", ""),
                    ch.get("url", ""),
                    ch.get("tvg_logo", ""),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("icon-prefetch: %s failed: %s", ch.get("name"), exc)
            continue
        if icon_url:
            fetched += 1
        await asyncio.sleep(0.2)

    skipped = len(needing) - fetched
    task_queue.log_check_report(
        "icon-prefetch",
        found=fetched,
        not_found=skipped,
        reworking=len(needing),
        how_many_found=fetched,
        how_many_not_found=skipped,
        priority_checks=0,
        how_many_left=0,
        quiet_if_empty=True,
    )
    return {"fetched": fetched, "skipped": skipped}


async def icon_prefetch_loop(
    interval_sec: Optional[float] = None,
    store: Optional[ChannelStore] = None,
) -> None:
    interval = float(interval_sec if interval_sec is not None else ICON_PREFETCH_INTERVAL_SEC)
    while True:
        try:
            await run_icon_prefetch_batch(store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("icon_prefetch_loop: batch failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Server geo backfill — host-based country for channels still GLOBAL
# ---------------------------------------------------------------------------

async def run_geo_backfill_batch(
    store: Optional[ChannelStore] = None,
    *,
    limit: Optional[int] = None,
) -> dict:
    """Geolocate stream hosts for up to ``limit`` GLOBAL/blank-country channels.

    The store call blocks on DNS and the rate-limited geo API, so it runs in a
    worker thread.
    """
    store = store or get_default_store()
    limit = limit or GEO_BACKFILL_BATCH_SIZE
    started = time.perf_counter()
    updated = await asyncio.to_thread(store.backfill_server_countries, limit)
    elapsed = round(time.perf_counter() - started, 2)
    if updated:
        task_queue.log_worker_batch(
            "geo-backfill",
            [f"updated   {updated} channel countries", f"elapsed   {elapsed}s"],
            limit=2,
        )
    return {"updated": updated, "elapsed_sec": elapsed}


async def geo_backfill_loop(
    interval_sec: Optional[float] = None,
    store: Optional[ChannelStore] = None,
) -> None:
    interval = float(interval_sec if interval_sec is not None else GEO_BACKFILL_INTERVAL_SEC)
    while True:
        try:
            await run_geo_backfill_batch(store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("geo_backfill_loop: batch failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# EPG refresh (stub — placeholder for future work)
# ---------------------------------------------------------------------------

async def run_epg_refresh(store: Optional[ChannelStore] = None) -> dict:
    """Fetch optional XMLTV feed and update ``playing_now`` by tvg_id / name."""
    from config import IPTV_XMLTV_URL

    store = store or get_default_store()
    url = (IPTV_XMLTV_URL or "").strip()
    if not url:
        task_queue.log_worker_batch("epg-refresh", ["No IPTV_XMLTV_URL configured"], limit=1)
        return {"status": "skipped", "updated": 0}

    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(
            None,
            lambda: requests.get(url, headers=HEADERS, timeout=60).text,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("epg-refresh fetch failed: %s", exc)
        return {"status": "error", "error": str(exc)}

    # Minimal XMLTV: <programme channel="id" ...><title>...</title>
    import re as _re
    now_titles: dict[str, str] = {}
    for m in _re.finditer(
        r'<programme[^>]*channel="([^"]+)"[^>]*>\s*<title[^>]*>([^<]+)</title>',
        text,
        flags=_re.I,
    ):
        cid, title = m.group(1).strip(), m.group(2).strip()
        if cid and title and cid not in now_titles:
            now_titles[cid] = title

    updated = 0
    results = []
    channels = store.get_valid_channels(include_test=True)
    for ch in channels:
        tid = (ch.get("tvg_id") or "").strip()
        title = now_titles.get(tid)
        if not title:
            # fuzzy: match channel name in programme channel attr
            name = (ch.get("name") or "").lower()
            for cid, t in now_titles.items():
                if name and name in cid.lower():
                    title = t
                    break
        if title and title != ch.get("playing_now"):
            results.append({
                "url": ch.get("url"),
                "status": ch.get("status") or "online",
                "playing_now": title,
                "fail_reason": ch.get("fail_reason"),
            })
    if results:
        store.update_channel_results(results)
        updated = len(results)

    task_queue.log_worker_batch(
        "epg-refresh",
        [f"Updated playing_now for {updated} channels ({len(now_titles)} programmes)"],
        limit=1,
    )
    return {"status": "ok", "updated": updated, "programmes": len(now_titles)}


async def epg_refresh_loop(
    interval_sec: Optional[float] = None,
    store: Optional[ChannelStore] = None,
) -> None:
    interval = float(interval_sec if interval_sec is not None else EPG_REFRESH_INTERVAL_SEC)
    while True:
        try:
            await run_epg_refresh(store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("epg_refresh_loop: cycle failed")
        await asyncio.sleep(interval)


__all__ = [
    "ACTIVE_STATUSES",
    "DEAD_STATUSES",
    "ACTIVE_HEALTH_INTERVAL_SEC",
    "DEAD_REVIVAL_INTERVAL_SEC",
    "INGEST_INTERVAL_SEC",
    "ICON_PREFETCH_INTERVAL_SEC",
    "EPG_REFRESH_INTERVAL_SEC",
    "GEO_BACKFILL_INTERVAL_SEC",
    "run_geo_backfill_batch",
    "geo_backfill_loop",
    "classify_fail_reason",
    "run_active_health_batch",
    "active_health_loop",
    "run_dead_revival_batch",
    "dead_revival_loop",
    "run_ingest_sources",
    "ingest_sources_loop",
    "run_discover_playlist",
    "run_icon_prefetch_batch",
    "icon_prefetch_loop",
    "run_epg_refresh",
    "epg_refresh_loop",
]
