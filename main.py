"""IPTV Scanner entry point.

Thin bootstrap: ensure SQLite + directories, configure logging, build the Flask
app, start the embedded FastWorker-compatible task queue, and serve HTTP.

Startup options
---------------
Interactive (TTY)::

    python main.py
    # menu waits 30s then auto-selects [1] Continue (avoids stall on auto-restart)

Non-interactive::

    python main.py --fresh          # purge catalog, then ingest
    python main.py --fresh --yes    # no confirm prompt
    IPTV_STARTUP_PURGE=1 python main.py
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import os
import signal
import sys
import time
from threading import Thread

from app_factory import create_app
from config import (
    DIRECTORIES,
    FILES,
    IPTV_BIND_HOST,
    IPTV_PORT,
    IPTV_PROXY_MAX_CONCURRENT,
    IPTV_PUBLIC_BASE_URL,
    IPTV_WAITRESS_THREADS,
)
from features.storage.db import ensure_db
from features.workers import fw_tasks


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("main")


class _QuietAccessFilter(logging.Filter):
    """Drop high-frequency HTTP access lines (HLS segments, status polls, etc.)."""

    _SKIP_SUBSTR = (
        "/proxy/stream",
        "/proxy/image",
        "/status",
        "/api/events",
        "/sw.js",
        "/manifest.webmanifest",
        "/icons/",
        "/css/",
        "/js/",
        "favicon",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in self._SKIP_SUBSTR)


def _configure_quiet_logging() -> None:
    """Keep occasional worker reports; silence grab/proxy spam."""
    logging.getLogger("werkzeug").addFilter(_QuietAccessFilter())
    for name in (
        "urllib3",
        "charset_normalizer",
        "streamlink",
        "streamlink.session",
        "streamlink.stream",
        "streamlink.stream.ffmpegmux",
        "streamlink.stream.hls",
        "streamlink.stream.http",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)


_configure_quiet_logging()

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="iptv-pool",
)


def _ensure_directories():
    for directory in DIRECTORIES:
        os.makedirs(directory, exist_ok=True)
    os.makedirs("static/icons", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    os.makedirs("jsons", exist_ok=True)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IPTV Scanner — live guide, SQLite catalog, background health checks.",
    )
    p.add_argument(
        "--fresh",
        "--purge",
        dest="fresh",
        action="store_true",
        help="Purge the channel catalog, then fetch sources from scratch.",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt when purging.",
    )
    p.add_argument(
        "--no-menu",
        action="store_true",
        help="Skip the interactive startup menu (use existing data unless --fresh).",
    )
    p.add_argument(
        "--keep-geo",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,  # default keep host geo
    )
    p.add_argument(
        "--clear-geo",
        action="store_true",
        help="Also clear the host→country geo cache when purging.",
    )
    return p.parse_args(argv)


def _clear_legacy_json_caches() -> None:
    """Best-effort empty the legacy jsons/*.json mirrors after a catalog purge."""
    for key in ("streams", "dead", "invalid", "master"):
        path = FILES.get(key)
        if not path:
            continue
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[]\n")
        except OSError as exc:
            log.debug("legacy json clear skipped for %s: %s", path, exc)
    slug_path = os.path.join("jsons", "SEO_SLUG_MANIFEST.json")
    try:
        with open(slug_path, "w", encoding="utf-8") as fh:
            fh.write('{"slug_order":[],"slugs":{}}\n')
    except OSError as exc:
        log.debug("SEO manifest clear skipped: %s", exc)


def purge_and_prepare(store, *, clear_geo: bool = False) -> dict:
    """Wipe catalog tables, legacy JSON mirrors, and mark a fresh ingest."""
    before = store.count_channels(exclude_test=False)
    summary = store.purge_catalog(keep_host_geo=not clear_geo)
    _clear_legacy_json_caches()
    try:
        from features.seo import seo as seo_mod

        seo_mod._seo_slug_to_channel = {}
        seo_mod._seo_slugs_sorted = []
        seo_mod._seo_slug_revision = -1
    except Exception:  # noqa: BLE001
        pass
    fw_tasks.REQUEST_FRESH_INGEST = True
    log.info(
        "Purged catalog (%d channels removed). Fresh ingest will run shortly after start.",
        summary.get("channels", before),
    )
    return summary


def _confirm_purge(total: int) -> bool:
    prompt = (
        f"This deletes {total} channel(s) from the local database and re-fetches "
        f"playlists. Continue? [y/N] "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _startup_menu_timeout_sec() -> float:
    raw = os.environ.get("IPTV_STARTUP_MENU_TIMEOUT_SEC", "30").strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def _read_choice_with_timeout(timeout_sec: float, default: str = "1") -> str:
    """Wait for a menu key; after ``timeout_sec`` return ``default`` (option 1).

    Gives auto-restarts a short cool-down before Continue kicks in, while still
    letting an operator pick purge/quit immediately.
    """
    if timeout_sec <= 0:
        try:
            return input("Choose [1/2/3] (default 1): ").strip() or default
        except EOFError:
            return default

    prompt = f"Choose [1/2/3] — auto [1] Continue in {int(timeout_sec)}s: "
    sys.stdout.write(prompt)
    sys.stdout.flush()

    # Windows: msvcrt so we can countdown without blocking the whole process.
    if sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:
            msvcrt = None
        if msvcrt is not None:
            buf: list[str] = []
            deadline = time.monotonic() + timeout_sec
            last_shown = -1
            while True:
                left = int(max(0.0, deadline - time.monotonic()))
                if left != last_shown:
                    last_shown = left
                    sys.stdout.write(
                        f"\rChoose [1/2/3] — auto [1] Continue in {left:2d}s: {''.join(buf)}"
                    )
                    sys.stdout.flush()
                if time.monotonic() >= deadline:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    print(f"No input — continuing with option [1] after {int(timeout_sec)}s.")
                    return default
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\r", "\n"):
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return "".join(buf).strip() or default
                    if ch in ("\x08", "\x7f"):  # backspace
                        if buf:
                            buf.pop()
                        continue
                    if ch == "\x03":
                        raise KeyboardInterrupt
                    # Ignore special keys (arrows etc. arrive as \x00 / \xe0 + next)
                    if ch in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        continue
                    if ch.isprintable():
                        buf.append(ch)
                else:
                    time.sleep(0.05)

    # POSIX: select() on stdin with a live countdown.
    try:
        import select
    except ImportError:
        select = None
    if select is not None and hasattr(sys.stdin, "fileno"):
        deadline = time.monotonic() + timeout_sec
        last_shown = -1
        while True:
            left = int(max(0.0, deadline - time.monotonic()))
            if left != last_shown:
                last_shown = left
                sys.stdout.write(
                    f"\rChoose [1/2/3] — auto [1] Continue in {left:2d}s: "
                )
                sys.stdout.flush()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                sys.stdout.write("\n")
                sys.stdout.flush()
                print(f"No input — continuing with option [1] after {int(timeout_sec)}s.")
                return default
            ready, _, _ = select.select([sys.stdin], [], [], min(0.25, remaining))
            if ready:
                line = sys.stdin.readline()
                sys.stdout.write("\n")
                sys.stdout.flush()
                return (line or "").strip() or default

    # Last resort: blocking input (no timeout available).
    try:
        return input("").strip() or default
    except EOFError:
        return default


def _interactive_startup_menu(store) -> str:
    """Return ``continue``, ``fresh``, or ``quit``."""
    total = store.count_channels(exclude_test=False)
    timeout_sec = _startup_menu_timeout_sec()
    print()
    print("┌──────────────────────────────────────────────┐")
    print("│           IPTV Scanner — startup             │")
    print("├──────────────────────────────────────────────┤")
    print(f"│  Database : {store.path}")
    print(f"│  Channels : {total}")
    print("├──────────────────────────────────────────────┤")
    print("│  [1] Continue with existing data             │")
    print("│  [2] Purge old data and fetch fresh sources  │")
    print("│  [3] Quit                                    │")
    print("└──────────────────────────────────────────────┘")
    if timeout_sec > 0:
        print(
            f"Waiting {int(timeout_sec)}s before [1] Continue "
            "(press 2 or 3 to override — eases auto-restart load)."
        )
    try:
        choice = _read_choice_with_timeout(timeout_sec, default="1")
    except EOFError:
        return "continue"
    if choice in ("2", "p", "purge", "fresh"):
        return "fresh"
    if choice in ("3", "q", "quit", "exit"):
        return "quit"
    return "continue"


_ensure_directories()
store = ensure_db()
log.info("SQLite ready at %s (%d channels)", store.path, store.count_channels(exclude_test=False))

app = create_app()


def run_flask():
    """Serve with Waitress (IPTV_WAITRESS_THREADS); fall back to Flask's dev server."""
    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress not installed (pip install waitress); using the Flask dev server")
        app.run(host=IPTV_BIND_HOST, port=IPTV_PORT, use_reloader=False, threaded=True)
        return

    kwargs = {
        "host": IPTV_BIND_HOST,
        "port": IPTV_PORT,
        "threads": IPTV_WAITRESS_THREADS,
        "ident": "iptv-scanner",
    }
    try:
        from waitress.adjustments import Adjustments

        if "clear_untrusted_proxy_headers" in {p[0] for p in Adjustments._params}:
            kwargs["clear_untrusted_proxy_headers"] = False
    except Exception:
        pass

    if IPTV_WAITRESS_THREADS <= IPTV_PROXY_MAX_CONCURRENT:
        log.warning(
            "IPTV_WAITRESS_THREADS=%d <= IPTV_PROXY_MAX_CONCURRENT=%d: proxy streams can occupy "
            "every worker thread; raise IPTV_WAITRESS_THREADS (suggest >= %d)",
            IPTV_WAITRESS_THREADS, IPTV_PROXY_MAX_CONCURRENT, IPTV_PROXY_MAX_CONCURRENT + 8,
        )
    log.info("Serving with waitress on %s:%s (%d threads)", IPTV_BIND_HOST, IPTV_PORT, IPTV_WAITRESS_THREADS)
    serve(app, **kwargs)


async def _async_main():
    # Repeating cadences (including ingest) are started inside bootstrap /
    # schedule_defaults — do not also delay("ingest_sources") here.
    client = await fw_tasks.bootstrap(
        bind_host=IPTV_BIND_HOST,
        port=IPTV_PORT,
        public_base=IPTV_PUBLIC_BASE_URL or None,
        worker_count=4,
    )
    return client


def _shutdown(loop: asyncio.AbstractEventLoop, client) -> None:
    log.info("Shutting down :3 XD, bye bye")  # just a joke, don't take it seriously :3 XD
    if client is not None:
        try:
            loop.run_until_complete(asyncio.wait_for(client.stop(), timeout=2.5))
        except Exception:
            pass
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=1.0,
                )
            )
    except Exception:
        pass
    try:
        _EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        try:
            _EXECUTOR.shutdown(wait=False)
        except Exception:
            pass
    except Exception:
        pass
    try:
        loop.close()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    args = _parse_args()
    want_fresh = bool(args.fresh) or _env_truthy("IPTV_STARTUP_PURGE")
    interactive = (
        not args.no_menu
        and not want_fresh
        and sys.stdin is not None
        and sys.stdin.isatty()
    )

    if interactive:
        decision = _interactive_startup_menu(store)
        if decision == "quit":
            log.info("Startup cancelled.")
            raise SystemExit(0)
        if decision == "fresh":
            want_fresh = True

    if want_fresh:
        total = store.count_channels(exclude_test=False)
        if not args.yes and not _env_truthy("IPTV_STARTUP_PURGE"):
            # Menu path already chose fresh; still confirm unless -y / env.
            if interactive or sys.stdin.isatty():
                if not _confirm_purge(total):
                    log.info("Purge cancelled; continuing with existing data.")
                    want_fresh = False
            elif total > 0:
                log.error("Refusing non-interactive purge without --yes (or IPTV_STARTUP_PURGE=1).")
                raise SystemExit(2)
        if want_fresh:
            purge_and_prepare(store, clear_geo=bool(args.clear_geo))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_default_executor(_EXECUTOR)

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    client = None
    stop_state = {"requested": False}

    def _request_stop(*_args):
        if stop_state["requested"]:
            os._exit(0)
        stop_state["requested"] = True
        loop.call_soon_threadsafe(loop.stop)

    try:
        signal.signal(signal.SIGINT, _request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _request_stop)
    except Exception:
        pass

    try:
        client = loop.run_until_complete(_async_main())
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown(loop, client)
