"""LEGACY JSON-file workers — NOT used by ``main.py``.

The live runtime uses SQLite + ``features.workers.health_workers`` /
``fw_tasks``. This module remains only for historical reference and must not
be imported from hot paths. Prefer deleting call sites over extending it.
"""

import asyncio
import logging
import time
from functools import partial

import state
from features.storage.channels_io import load_json_file, save_json_atomic
from config import FILES, SWEEP_INTERVAL_SEC
from features.icons.icons import download_channel_icon
from features.ingest.ingest import channel_icon_safe_name, check_all_global_sources, find_local_icon_url
from features.validate.validate import process_channels


async def initial_scan():
    """Initial scan: parse global sources, validate new channels, persist merged list."""
    state.SCAN_ACTIVE = True
    try:
        logging.info("Starting global initial scan...")
        channels = await asyncio.to_thread(check_all_global_sources)

        existing_valid = load_json_file(FILES['master'], load_json_file(FILES['streams'], []))
        existing_map = {ch.get('url'): ch for ch in existing_valid if ch.get('url')}

        merged_cached = []
        pending_validation = []
        for channel in channels:
            cached = existing_map.get(channel.get('url'))
            if cached:
                cached.update({
                    'name': channel.get('name', cached.get('name')),
                    'tvg_logo': channel.get('tvg_logo', cached.get('tvg_logo', '')),
                    'group_title': channel.get('group_title', cached.get('group_title', 'Unknown')),
                    'country': channel.get('country', cached.get('country', 'GLOBAL'))
                })
                merged_cached.append(cached)
            else:
                pending_validation.append(channel)

        invalid_links = load_json_file(FILES['invalid'], [])
        valid_new = []
        dead_channels = []
        if pending_validation:
            valid_new, dead_channels = await process_channels(pending_validation, invalid_links)

        valid_channels = merged_cached + valid_new
        save_json_atomic(FILES['streams'], valid_channels)
        save_json_atomic(FILES['dead'], dead_channels)
        save_json_atomic(FILES['invalid'], invalid_links)
        save_json_atomic(FILES['master'], valid_channels)

        logging.info(f"Global initial scan complete: {len(valid_channels)} valid, {len(dead_channels)} dead.")
    except Exception as e:
        logging.error(f"Error during global initial scan: {e}")
    finally:
        state.SCAN_ACTIVE = False


async def sweep_channels_async():
    """Full sweep: re-parse all global sources and re-validate every channel."""
    state.SCAN_ACTIVE = True
    state.LAST_SWEEP_STARTED_AT = time.time()
    try:
        logging.info("Starting global channel sweep...")
        channels = await asyncio.to_thread(check_all_global_sources)
        invalid_links = load_json_file(FILES['invalid'], [])
        valid_channels, dead_channels = await process_channels(channels, invalid_links)
        save_json_atomic(FILES['streams'], valid_channels)
        save_json_atomic(FILES['dead'], dead_channels)
        save_json_atomic(FILES['master'], valid_channels)

        logging.info(f"Global channel sweep complete: {len(valid_channels)} valid, {len(dead_channels)} dead.")
        state.LAST_SWEEP_COUNTS = {"valid": len(valid_channels), "dead": len(dead_channels)}
        state.LAST_SWEEP_COMPLETED_AT = time.time()
    finally:
        state.SCAN_ACTIVE = False


async def background_icon_prefetch_loop():
    """Fill in missing logos in the background; uses local disk after first scrape."""
    await asyncio.sleep(20)
    executor_loop = asyncio.get_event_loop()
    while True:
        try:
            chs = load_json_file(FILES['streams'], [])
            batch = []
            for ch in chs:
                if find_local_icon_url(channel_icon_safe_name(ch.get('name', ''))):
                    continue
                batch.append(ch)
                if len(batch) >= 10:
                    break
            for ch in batch:
                await executor_loop.run_in_executor(
                    None,
                    partial(
                        download_channel_icon,
                        ch.get('name', ''),
                        ch.get('url', ''),
                        ch.get('tvg_logo', ''),
                    ),
                )
                await asyncio.sleep(0.2)
        except Exception as exc:
            logging.debug("Icon prefetch cycle: %s", exc)
        await asyncio.sleep(6)


async def start_periodic_sweep():
    """Periodic full sweeps (interval configurable via IPTV_SWEEP_INTERVAL_SEC)."""
    while True:
        await sweep_channels_async()
        await asyncio.sleep(SWEEP_INTERVAL_SEC)


async def bootstrap_background_tasks():
    """Run initial scrape first; then periodic sweep + icon prefetch.

    Previously initial_scan and start_periodic_sweep were scheduled together, so both
    could enter process_channels() and overwrite jsons mid-batch — often leaving
    IPTV_STREAMS_FILE.json empty.
    """
    try:
        await initial_scan()
    except Exception as e:
        logging.error("Bootstrap initial_scan failed: %s", e, exc_info=True)
    await asyncio.gather(
        start_periodic_sweep(),
        background_icon_prefetch_loop(),
    )
