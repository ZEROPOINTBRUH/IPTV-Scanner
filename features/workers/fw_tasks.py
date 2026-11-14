"""FastWorker task registry for the IPTV Scanner.

Every task defined here is a thin adapter that delegates to a runner in
:mod:`health_workers`. Keeping the registry separate from the worker
implementations means:

* The FastWorker GUI (or the embedded fallback in :mod:`task_queue`) sees a
  clean list of named entry points.
* The runners in :mod:`health_workers` stay directly callable from tests
  and REPL sessions without going through the queue.
* Priorities and cadences live in one place, making it easy to see what
  the scheduler is going to do.

Bootstrap
---------
Callers typically do::

    import fw_tasks   # side-effect: registers every @task
    client = fw_tasks.build_client()
    await client.start()
    await fw_tasks.schedule_defaults(client)

``schedule_defaults`` wires the standard cadences from
:mod:`health_workers`. Individual tasks can also be enqueued on demand via
``client.delay("active_health_batch")`` etc.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from . import health_workers
from . import task_queue
from features.storage.db import get_default_store
from .task_queue import Client, TaskPriority, task


log = logging.getLogger("fw_tasks")

# Set by ``main`` / admin purge so the first ingest starts quickly after a wipe.
REQUEST_FRESH_INGEST = False


# ---------------------------------------------------------------------------
# Task definitions — each is a named entry point in the queue
# ---------------------------------------------------------------------------

@task("active_health_batch", priority=TaskPriority.HIGH)
async def active_health_batch(limit: Optional[int] = None) -> dict:
    """Run one active-health batch against ``get_default_store()``."""
    return await health_workers.run_active_health_batch(
        get_default_store(), limit=limit,
    )


@task("dead_revival_batch", priority=TaskPriority.NORMAL)
async def dead_revival_batch(limit: Optional[int] = None) -> dict:
    """Retry a batch of dead channels; survivors are promoted to online."""
    return await health_workers.run_dead_revival_batch(
        get_default_store(), limit=limit,
    )


@task("ingest_sources", priority=TaskPriority.NORMAL)
async def ingest_sources() -> dict:
    """Full scrape of every configured source, upserted into the store."""
    return await health_workers.run_ingest_sources(get_default_store())


@task("discover_playlist", priority=TaskPriority.LOW)
async def discover_playlist(seed_url: str) -> dict:
    """Depth-limited discovery walk starting from ``seed_url``."""
    return await health_workers.run_discover_playlist(
        seed_url, get_default_store(),
    )


@task("geo_backfill_batch", priority=TaskPriority.LOW)
async def geo_backfill_batch(limit: Optional[int] = None) -> dict:
    """Resolve host country for channels still marked GLOBAL / blank."""
    return await health_workers.run_geo_backfill_batch(
        get_default_store(), limit=limit,
    )


@task("epg_refresh", priority=TaskPriority.LOW)
async def epg_refresh() -> dict:
    """Placeholder EPG refresh — reserved for the Phase 3 EPG pipeline."""
    return await health_workers.run_epg_refresh(get_default_store())


@task("verify_channel", priority=TaskPriority.HIGH)
async def verify_channel(url: str, reason: str = "") -> dict:
    """Validate one channel now (crowd reports: alive / no_playback / slow_load)."""
    return await health_workers.run_verify_channel(
        url, get_default_store(), reason=reason,
    )


@task("icon_prefetch_batch", priority=TaskPriority.LOW)
async def icon_prefetch_batch(limit: Optional[int] = None) -> dict:
    """Fill in missing on-disk logos for up to ``limit`` channels."""
    return await health_workers.run_icon_prefetch_batch(
        get_default_store(), limit=limit,
    )


# ---------------------------------------------------------------------------
# Client bootstrap helpers
# ---------------------------------------------------------------------------

def build_client(*, worker_count: int = 4, name: str = "iptv-scanner") -> Client:
    """Return a fresh :class:`task_queue.Client`.

    Kept as a factory (rather than a module singleton) so tests can spin up
    their own client with a smaller pool without touching global state.
    """
    return Client(worker_count=worker_count, name=name)


async def schedule_defaults(client: Client) -> list[asyncio.Task]:
    """Attach the standard repeating cadences to ``client``.

    Real ``fastworker`` exposes a similar ``schedule_repeat`` shape; if a
    future backend does not, this helper is the single place to swap in the
    equivalent primitive.
    """
    scheduled: list[asyncio.Task] = []
    fresh = bool(REQUEST_FRESH_INGEST)
    # After a purge, pull sources almost immediately, then health-check.
    ingest_delay = 2.0 if fresh else 8.0
    health_delay = 6.0 if fresh else 5.0
    scheduled.append(
        client.schedule_repeat(
            "active_health_batch",
            health_workers.ACTIVE_HEALTH_INTERVAL_SEC,
            priority=TaskPriority.HIGH,
            initial_delay=health_delay,
        )
    )
    scheduled.append(
        client.schedule_repeat(
            "dead_revival_batch",
            health_workers.DEAD_REVIVAL_INTERVAL_SEC,
            priority=TaskPriority.NORMAL,
            initial_delay=30.0,
        )
    )
    scheduled.append(
        client.schedule_repeat(
            "ingest_sources",
            health_workers.INGEST_INTERVAL_SEC,
            priority=TaskPriority.NORMAL,
            # Let Waitress bind and serve the first page before the big upsert
            # (shortened when REQUEST_FRESH_INGEST is set after a purge).
            initial_delay=ingest_delay,
        )
    )
    scheduled.append(
        client.schedule_repeat(
            "icon_prefetch_batch",
            health_workers.ICON_PREFETCH_INTERVAL_SEC,
            priority=TaskPriority.LOW,
            initial_delay=20.0,
        )
    )
    scheduled.append(
        client.schedule_repeat(
            "geo_backfill_batch",
            health_workers.GEO_BACKFILL_INTERVAL_SEC,
            priority=TaskPriority.LOW,
            initial_delay=45.0,
        )
    )
    scheduled.append(
        client.schedule_repeat(
            "epg_refresh",
            health_workers.EPG_REFRESH_INTERVAL_SEC,
            priority=TaskPriority.LOW,
            initial_delay=120.0,
        )
    )
    log.info("fw_tasks: scheduled %d repeating cadences", len(scheduled))
    return scheduled


# ---------------------------------------------------------------------------
# Enqueue from non-async callers (Flask request threads)
# ---------------------------------------------------------------------------

_active_client: Optional[Client] = None
_active_loop: Optional[asyncio.AbstractEventLoop] = None
_verify_recent: dict[str, float] = {}
_verify_lock = threading.Lock()
VERIFY_DEDUPE_SEC = 30.0


def register_client(client: Client, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Remember the running client + its loop so :func:`enqueue` can reach it."""
    global _active_client, _active_loop
    _active_client = client
    _active_loop = loop or asyncio.get_running_loop()


def enqueue(
    name: str,
    *args,
    priority: TaskPriority = TaskPriority.HIGH,
    **kwargs,
) -> bool:
    """Thread-safe ``client.delay`` for Flask threads; ``False`` when no queue is running."""
    client, loop = _active_client, _active_loop
    if client is None or loop is None or loop.is_closed() or not loop.is_running():
        return False

    async def _put() -> None:
        try:
            await client.delay(name, *args, priority=priority, **kwargs)
        except TypeError:  # backend without a priority kwarg
            await client.delay(name, *args, **kwargs)

    try:
        asyncio.run_coroutine_threadsafe(_put(), loop).result(timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        log.warning("enqueue(%s) failed", name, exc_info=True)
        return False


def _verify_in_thread(url: str, reason: str) -> None:
    """Fallback when no queue is running: private event loop in a plain thread."""

    def _run() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                health_workers.run_verify_channel(url, get_default_store(), reason=reason)
            )
        except Exception:  # noqa: BLE001
            log.debug("verify thread failed for %s", url, exc_info=True)
        finally:
            loop.close()

    threading.Thread(target=_run, name="verify-channel", daemon=True).start()


def enqueue_verify(url: str, reason: str = "") -> str:
    """Queue a ``verify_channel`` task. Returns ``queue`` | ``thread`` | ``deduped``."""
    now = time.monotonic()
    with _verify_lock:
        if len(_verify_recent) > 2048:
            for k in [k for k, t in _verify_recent.items() if now - t > VERIFY_DEDUPE_SEC]:
                _verify_recent.pop(k, None)
        last = _verify_recent.get(url)
        if last is not None and now - last < VERIFY_DEDUPE_SEC:
            return "deduped"
        _verify_recent[url] = now
    if enqueue("verify_channel", url, reason=reason):
        return "queue"
    _verify_in_thread(url, reason)
    return "thread"


async def bootstrap(
    *,
    bind_host: str = "127.0.0.1",
    port: int = 40006,
    public_base: Optional[str] = None,
    worker_count: int = 4,
) -> Client:
    """One-call bootstrap for :mod:`main`.

    Starts a client, wires the default cadences, prints the access banner,
    and returns the running client so the caller can ``await client.stop()``
    on shutdown.
    """
    client = build_client(worker_count=worker_count)
    await client.start()
    register_client(client, asyncio.get_running_loop())
    await schedule_defaults(client)
    task_queue.print_access_banner(bind_host, port, public_base=public_base)
    return client


__all__ = [
    "active_health_batch",
    "dead_revival_batch",
    "ingest_sources",
    "discover_playlist",
    "epg_refresh",
    "icon_prefetch_batch",
    "geo_backfill_batch",
    "verify_channel",
    "enqueue",
    "enqueue_verify",
    "register_client",
    "build_client",
    "schedule_defaults",
    "bootstrap",
]
