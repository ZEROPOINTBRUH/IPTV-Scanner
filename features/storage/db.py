"""SQLite-backed storage for IPTV Scanner Phase 2.

The legacy pipeline persisted every scan into a handful of large JSON files
under ``jsons/``. That worked but forced the whole channel list to be rewritten
on every mutation, and cross-worker coordination required a global write lock.

Phase 2 replaces those files with a WAL-mode SQLite database managed by the
:class:`ChannelStore` here. All read and write paths go through this store so
the checker workers, ingest task and Flask routes see a consistent view.

The module exposes:

* :func:`ensure_db` — idempotent factory that creates the schema, applies
  indexes, runs a one-shot JSON import if the ``channels`` table is empty, and
  returns a ready-to-use :class:`ChannelStore` bound to the target file.
* :class:`ChannelStore` — thread-safe wrapper around an APSW connection (when
  ``apsw`` is installed) or ``sqlite3.Connection`` with high-level helpers
  (``list_channels``, ``upsert_from_ingest``, etc.).
* Small CLI (``python -m features.storage.db`` / ``python -m features.storage.db backup``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from typing import Any, Iterable, Optional, Sequence

from .normalize import (
    classify_media_type,
    coerce_dict,
    is_test_channel,
    normalize_channel,
    normalize_group_title,
    normalize_url,
)

try:  # Optional faster / more robust SQLite driver.
    import apsw  # type: ignore
except Exception:  # ImportError or a broken binary wheel
    apsw = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

# Set IPTV_DB_DRIVER=sqlite3 to force the stdlib driver even when apsw is installed.
_APSW_ENABLED = apsw is not None and (
    os.environ.get("IPTV_DB_DRIVER", "").strip().lower() != "sqlite3"
)

try:
    from config import IPTV_CHECK_LEASE_SEC as _CONFIG_CHECK_LEASE_SEC
except Exception:  # pragma: no cover - config unavailable (odd cwd)
    try:
        _CONFIG_CHECK_LEASE_SEC = float(os.environ.get("IPTV_CHECK_LEASE_SEC", "300"))
    except (TypeError, ValueError):
        _CONFIG_CHECK_LEASE_SEC = 300.0
DEFAULT_CHECK_LEASE_SEC = float(_CONFIG_CHECK_LEASE_SEC)


class _ApswRow:
    """Row that behaves like ``sqlite3.Row`` (index, name lookup, ``keys()``)."""

    __slots__ = ("_values", "_names", "_index")

    def __init__(self, names: tuple, index: dict, values: tuple) -> None:
        self._names = names
        self._index = index
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list:
        return list(self._names)

    def __repr__(self) -> str:
        return f"_ApswRow({dict(zip(self._names, self._values))!r})"


class _ApswCursor:
    """Eagerly-fetched result set exposing the small DB-API subset we use."""

    def __init__(self, rows: list, rowcount: int) -> None:
        self._rows = rows
        self._pos = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def fetchmany(self, size: int = 1) -> list:
        rows = self._rows[self._pos:self._pos + max(1, int(size))]
        self._pos += len(rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class _ApswConnection:
    """Minimal ``sqlite3.Connection``-shaped adapter over ``apsw.Connection``.

    APSW runs in autocommit mode; :meth:`begin_immediate` / :meth:`commit` /
    :meth:`rollback` give :meth:`ChannelStore.transaction` explicit control.
    """

    def __init__(self, path: str, busy_timeout_ms: int = 30000) -> None:
        self._db = apsw.Connection(path)
        self._db.setbusytimeout(int(busy_timeout_ms))

    def _total_changes(self) -> int:
        getter = getattr(self._db, "total_changes", None) or self._db.totalchanges
        return int(getter())

    def _autocommit(self) -> bool:
        getter = getattr(self._db, "get_autocommit", None) or self._db.getautocommit
        return bool(getter())

    @staticmethod
    def _column_names(cursor) -> tuple:
        getter = getattr(cursor, "get_description", None) or getattr(
            cursor, "getdescription", None
        )
        if getter is None:
            return ()
        try:
            return tuple(d[0] for d in getter())
        except Exception:  # statement produced no result columns
            return ()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _ApswCursor:
        cursor = self._db.cursor()
        before = self._total_changes()
        cursor.execute(sql, tuple(params) if params else None)
        names = self._column_names(cursor)
        raw_rows = cursor.fetchall() if names else []
        if raw_rows and len(raw_rows[0]) != len(names):
            names = tuple(f"col{i}" for i in range(len(raw_rows[0])))
        index = {n: i for i, n in enumerate(names)}
        rows = [_ApswRow(names, index, tuple(r)) for r in raw_rows]
        return _ApswCursor(rows, self._total_changes() - before)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> _ApswCursor:
        cursor = self._db.cursor()
        before = self._total_changes()
        cursor.executemany(sql, (tuple(p) for p in seq))
        return _ApswCursor([], self._total_changes() - before)

    def begin_immediate(self) -> None:
        if self._autocommit():
            self._db.cursor().execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        if not self._autocommit():
            self._db.cursor().execute("COMMIT")

    def rollback(self) -> None:
        if not self._autocommit():
            self._db.cursor().execute("ROLLBACK")

    def backup_to(self, dest: str) -> None:
        target = apsw.Connection(dest)
        try:
            with target.backup("main", self._db, "main") as job:
                job.step()
        finally:
            target.close()

    def close(self) -> None:
        self._db.close()


DEFAULT_DB_PATH = os.environ.get("IPTV_DB_PATH", os.path.join("data", "iptv.db"))
LEGACY_JSON_DIR = "jsons"
LEGACY_JSON_FILES = {
    "streams": os.path.join(LEGACY_JSON_DIR, "IPTV_STREAMS_FILE.json"),
    "dead": os.path.join(LEGACY_JSON_DIR, "DEAD_STREAMS_FILE.json"),
    "invalid": os.path.join(LEGACY_JSON_DIR, "INVALID_LINKS_FILE.json"),
    "master": os.path.join(LEGACY_JSON_DIR, "MASTER_CACHE_FILE.json"),
    "slugs": os.path.join(LEGACY_JSON_DIR, "SEO_SLUG_MANIFEST.json"),
}

VALID_STATUSES = frozenset({"online", "offline", "pending", "unknown", "error"})
VALID_MEDIA_TYPES = frozenset({"live", "vod", "unknown"})

_SORT_COLUMNS = {
    "name": "name COLLATE NOCASE",
    "group": "group_title COLLATE NOCASE",
    "group_title": "group_title COLLATE NOCASE",
    "country": "country COLLATE NOCASE",
    # "Servers" view: country now reflects where the stream host is located.
    "server": "country COLLATE NOCASE",
    "status": "status",
    "media_type": "media_type",
    "last_checked_at": "last_checked_at",
    "last_checked": "last_checked_at",
    "scanned": "last_checked_at",
    "recently_scanned": "last_checked_at",
    "updated_at": "updated_at",
    "url": "url",
    "fail_count": "fail_count",
    "trending": "trend_score",
    "popular": "trend_score",
    "trend_score": "trend_score",
    "watch_count": "watch_count",
    "last_watched_at": "last_watched_at",
    "variant_quality": "variant_quality COLLATE NOCASE",
    "variant_bandwidth": "variant_bandwidth",
}

# Exponential half-life for trending popularity (hours).
TREND_HALF_LIFE_HOURS = 6.0
# Ignore repeat plays of the same URL within this window.
WATCH_MIN_INTERVAL_SEC = 120.0
# Drop raw watch events older than this.
WATCH_PRUNE_DAYS = 30
# A viewer heartbeat counts as "watching now" for this long.
VIEWER_ACTIVE_SEC = 45.0
# Heartbeat rows older than this are deleted (opportunistically + via prune_viewers).
VIEWER_PRUNE_SEC = 600.0
# Persisted host→country lookups are re-resolved after this many days.
HOST_GEO_TTL_DAYS = 30
# Priority assigned by mark_priority_check (higher = checked sooner).
DEFAULT_CHECK_PRIORITY = 100

_PLACEHOLDER_COUNTRIES = frozenset({"", "GLOBAL", "XX", "ZZ", "UNKNOWN", "UNDEFINED"})


SCHEMA_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS channels (
        url               TEXT PRIMARY KEY,
        url_norm          TEXT UNIQUE,
        name              TEXT,
        tvg_id            TEXT,
        tvg_logo          TEXT,
        group_title       TEXT,
        playing_now       TEXT,
        status            TEXT DEFAULT 'unknown',
        country           TEXT,
        icon_url          TEXT,
        audio_language    TEXT,
        variant_of        TEXT,
        media_type        TEXT DEFAULT 'unknown',
        fail_reason       TEXT,
        fail_count        INTEGER DEFAULT 0,
        last_checked_at   REAL,
        updated_at        REAL,
        variant_quality   TEXT,
        variant_bandwidth INTEGER,
        trend_score       REAL DEFAULT 0,
        watch_count       INTEGER DEFAULT 0,
        last_watched_at   REAL,
        trend_updated_at  REAL,
        check_priority    INTEGER DEFAULT 0,
        check_leased_until REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channel_variants (
        variant_url    TEXT PRIMARY KEY,
        channel_url    TEXT NOT NULL,
        resolution     TEXT,
        bandwidth      INTEGER,
        codecs         TEXT,
        audio_language TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invalid_links (
        url        TEXT PRIMARY KEY,
        created_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seo_slugs (
        slug         TEXT PRIMARY KEY,
        channel_url  TEXT,
        channel_name TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_queue (
        url        TEXT PRIMARY KEY,
        depth      INTEGER DEFAULT 0,
        created_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channel_watches (
        url         TEXT NOT NULL,
        watched_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channel_viewers (
        session_id TEXT PRIMARY KEY,
        url        TEXT NOT NULL,
        last_seen  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS host_geo (
        hostname    TEXT PRIMARY KEY,
        country     TEXT,
        resolved_at REAL
    )
    """,
)

INDEX_STATEMENTS: Sequence[str] = (
    "CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status)",
    "CREATE INDEX IF NOT EXISTS idx_channels_group_title ON channels(group_title)",
    "CREATE INDEX IF NOT EXISTS idx_channels_country ON channels(country)",
    "CREATE INDEX IF NOT EXISTS idx_channels_media_type ON channels(media_type)",
    "CREATE INDEX IF NOT EXISTS idx_channels_last_checked_at ON channels(last_checked_at)",
    "CREATE INDEX IF NOT EXISTS idx_channels_url_norm ON channels(url_norm)",
    "CREATE INDEX IF NOT EXISTS idx_channels_variant_of ON channels(variant_of)",
    "CREATE INDEX IF NOT EXISTS idx_channels_trend_score ON channels(trend_score)",
    "CREATE INDEX IF NOT EXISTS idx_variants_channel_url ON channel_variants(channel_url)",
    "CREATE INDEX IF NOT EXISTS idx_seo_slugs_channel_url ON seo_slugs(channel_url)",
    "CREATE INDEX IF NOT EXISTS idx_playlist_queue_depth ON playlist_queue(depth)",
    "CREATE INDEX IF NOT EXISTS idx_channel_watches_watched_at ON channel_watches(watched_at)",
    "CREATE INDEX IF NOT EXISTS idx_channel_watches_url ON channel_watches(url)",
    "CREATE INDEX IF NOT EXISTS idx_channels_check_priority ON channels(check_priority)",
    "CREATE INDEX IF NOT EXISTS idx_channel_viewers_url ON channel_viewers(url)",
    "CREATE INDEX IF NOT EXISTS idx_channel_viewers_last_seen ON channel_viewers(last_seen)",
)

# Columns added to existing DBs via ALTER (CREATE TABLE IF NOT EXISTS won't add them).
_CHANNEL_COLUMN_MIGRATIONS: Sequence[tuple[str, str]] = (
    ("trend_score", "REAL DEFAULT 0"),
    ("watch_count", "INTEGER DEFAULT 0"),
    ("last_watched_at", "REAL"),
    ("trend_updated_at", "REAL"),
    ("check_priority", "INTEGER DEFAULT 0"),
    ("check_leased_until", "REAL"),
    ("http_user_agent", "TEXT"),
    ("http_referrer", "TEXT"),
)


CHANNEL_COLUMNS: tuple = (
    "url",
    "url_norm",
    "name",
    "tvg_id",
    "tvg_logo",
    "group_title",
    "playing_now",
    "status",
    "country",
    "icon_url",
    "audio_language",
    "variant_of",
    "media_type",
    "fail_reason",
    "fail_count",
    "last_checked_at",
    "updated_at",
    "variant_quality",
    "variant_bandwidth",
    "trend_score",
    "watch_count",
    "last_watched_at",
    "trend_updated_at",
    "check_priority",
    "check_leased_until",
    "http_user_agent",
    "http_referrer",
)


ACTIVE_STATUSES = ("online", "pending", "unknown")
DEAD_STATUSES = ("offline", "error")

_default_store: Optional["ChannelStore"] = None
_default_store_lock = threading.Lock()


def ensure_db(path: Optional[str] = None) -> "ChannelStore":
    """Return a ready :class:`ChannelStore` at ``path`` (env / default).

    The parent directory is created, the schema is applied idempotently, WAL
    mode is enabled, and a legacy JSON import is attempted whenever the
    ``channels`` table is empty.
    """
    global _default_store
    db_path = path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    store = ChannelStore(db_path)
    store._init_schema()

    if store.count_channels(exclude_test=False) == 0:
        try:
            imported = store.import_from_json_files()
            if imported:
                logger.info("Imported %d channels from legacy JSON files", imported)
        except Exception as exc:
            logger.warning("Legacy JSON import skipped: %s", exc)

    try:
        moved = store.separate_country_groups()
        if moved:
            logger.info("Separated %d country group-titles into country codes", moved)
    except Exception as exc:
        logger.warning("Country/category separation skipped: %s", exc)

    try:
        fixed = store.reclassify_media_types()
        if fixed:
            logger.info("Reclassified media_type on %d channels (HLS loops → live)", fixed)
    except Exception as exc:
        logger.warning("media_type reclassify skipped: %s", exc)

    with _default_store_lock:
        if path is None or os.path.abspath(db_path) == os.path.abspath(DEFAULT_DB_PATH):
            _default_store = store
    return store


def get_default_store() -> "ChannelStore":
    """Return the process-wide store, creating it on first use."""
    global _default_store
    with _default_store_lock:
        if _default_store is not None:
            return _default_store
    # Call ensure_db outside the lock — ensure_db also takes _default_store_lock
    # (non-reentrant), so nesting would deadlock.
    return ensure_db()


class ChannelStore:
    """Thread-safe wrapper around a WAL-mode SQLite connection.

    Uses APSW when importable (see :data:`_APSW_ENABLED`), otherwise stdlib
    ``sqlite3``. Either way rows support ``row[0]``, ``row["col"]`` and
    ``row.keys()``, and cursors support ``fetchone`` / ``fetchall`` / ``rowcount``.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._read_lock = threading.RLock()
        self._tx_depth = 0
        self._last_viewer_prune = 0.0
        self._apsw = False
        self._conn: Any = None
        self._read_conn: Any = None

        if _APSW_ENABLED:
            try:
                conn = _ApswConnection(path, 30000)
                with self._lock:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA foreign_keys=ON")
                self._conn = conn
                self._apsw = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("APSW unavailable for %s (%s); using sqlite3", path, exc)
                self._conn = None

        if self._conn is None:
            self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.execute("PRAGMA busy_timeout=30000")

        # Separate read connection so /channels can answer while ingest/health
        # hold the write lock (WAL allows concurrent readers).
        try:
            if self._apsw:
                rconn = _ApswConnection(path, 30000)
                rconn.execute("PRAGMA query_only=ON")
            else:
                rconn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
                rconn.row_factory = sqlite3.Row
                rconn.execute("PRAGMA busy_timeout=30000")
                try:
                    rconn.execute("PRAGMA query_only=ON")
                except sqlite3.Error:
                    pass
            self._read_conn = rconn
        except Exception as exc:  # noqa: BLE001
            logger.warning("read connection unavailable (%s); reads share write conn", exc)
            self._read_conn = None

    @property
    def driver(self) -> str:
        """Active SQLite driver name: ``"apsw"`` or ``"sqlite3"``."""
        return "apsw" if self._apsw else "sqlite3"

    # ------------------------------------------------------------------ core

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.close()
        with self._read_lock:
            if self._read_conn is not None:
                with contextlib.suppress(Exception):
                    self._read_conn.close()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            return self._conn.execute(sql, params)

    def _execute_read(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run a read-only query without waiting on the write lock when possible."""
        if self._read_conn is not None:
            with self._read_lock:
                return self._read_conn.execute(sql, params)
        return self._execute(sql, params)

    def _executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> Any:
        with self._lock:
            return self._conn.executemany(sql, seq)

    @contextlib.contextmanager
    def transaction(self, immediate: bool = False):
        """Yield a connection guarded by the write lock, committing on exit.

        ``immediate=True`` takes the SQLite write lock up front (``BEGIN
        IMMEDIATE``) so a read-then-write sequence is atomic across processes.
        APSW always begins immediately. Nested calls join the outer transaction.
        """
        with self._lock:
            if self._tx_depth > 0:
                self._tx_depth += 1
                try:
                    yield self._conn
                finally:
                    self._tx_depth -= 1
                return

            if self._apsw:
                self._conn.begin_immediate()
            elif immediate:
                if self._conn.in_transaction:
                    self._conn.commit()
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._tx_depth = 0

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            # Existing DBs created before trending columns need ALTER TABLE.
            existing_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(channels)").fetchall()
            }
            for col_name, col_type in _CHANNEL_COLUMN_MIGRATIONS:
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE channels ADD COLUMN {col_name} {col_type}"
                    )
            for statement in INDEX_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('revision', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
            )

    # ---------------------------------------------------------------- meta

    def bump_revision(self) -> int:
        """Increment the shared revision counter and return the new value."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'revision'"
            ).fetchone()
            current = int(row[0]) if row and row[0] is not None else 0
            new_value = current + 1
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('revision', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(new_value),),
            )
        return new_value

    def get_revision(self) -> int:
        row = self._execute_read(
            "SELECT value FROM meta WHERE key = 'revision'"
        ).fetchone()
        try:
            return int(row[0]) if row and row[0] is not None else 0
        except (TypeError, ValueError):
            return 0

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._execute_read(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row and row[0] is not None else default

    def set_meta(self, key: str, value: Optional[str]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value if value is None else str(value)),
            )

    # ------------------------------------------------------------- reading

    def count_channels(
        self,
        status: Optional[str] = None,
        media_type: Optional[str] = None,
        exclude_test: bool = True,
    ) -> int:
        clauses = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)
        if exclude_test:
            clauses.append("(group_title IS NULL OR group_title != 'Test')")
        sql = "SELECT COUNT(*) FROM channels"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = self._execute_read(sql, params).fetchone()
        return int(row[0]) if row else 0

    def status_counts(self, *, exclude_test: bool = True) -> dict:
        """One-query ``{status: count}`` for every valid status (0 when absent)."""
        sql = "SELECT status, COUNT(*) FROM channels"
        if exclude_test:
            sql += " WHERE (group_title IS NULL OR group_title != 'Test')"
        sql += " GROUP BY status"
        counts = {s: 0 for s in ("online", "pending", "unknown", "offline", "error")}
        for row in self._execute_read(sql).fetchall():
            key = (row[0] or "unknown").lower()
            if key not in counts:
                key = "unknown"
            counts[key] += int(row[1])
        return counts

    def count_check_queue(
        self,
        statuses: Sequence[str],
        *,
        exclude_test: bool = True,
    ) -> int:
        """How many channels in ``statuses`` still sit in the check rotation."""
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        clauses = [f"status IN ({placeholders})"]
        params: list = list(statuses)
        if exclude_test:
            clauses.append("(group_title IS NULL OR group_title != 'Test')")
        sql = "SELECT COUNT(*) FROM channels WHERE " + " AND ".join(clauses)
        row = self._execute_read(sql, params).fetchone()
        return int(row[0]) if row else 0

    def count_priority_checks(
        self,
        statuses: Optional[Sequence[str]] = None,
        *,
        exclude_test: bool = True,
    ) -> int:
        """Never-checked channels (``last_checked_at IS NULL``) — scan first."""
        statuses = tuple(statuses or ACTIVE_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        clauses = [
            f"status IN ({placeholders})",
            "last_checked_at IS NULL",
        ]
        params: list = list(statuses)
        if exclude_test:
            clauses.append("(group_title IS NULL OR group_title != 'Test')")
        sql = "SELECT COUNT(*) FROM channels WHERE " + " AND ".join(clauses)
        row = self._execute_read(sql, params).fetchone()
        return int(row[0]) if row else 0

    def list_channels(
        self,
        page: int = 1,
        limit: int = 50,
        q: Optional[str] = None,
        group: Optional[str] = None,
        country: Optional[str] = None,
        online_only: bool = False,
        include_test: bool = False,
        media_type: Optional[str] = None,
        status: Optional[str] = None,
        status_in: Optional[Sequence[str]] = None,
        sort: Optional[str] = None,
        sort_dir: str = "asc",
    ) -> dict:
        """Paginated channel listing plus facet counts and the current revision."""
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 50

        def _split_filter_values(value) -> list[str]:
            """One value, comma-list, or sequence → de-duplicated strings."""
            if value is None:
                return []
            if isinstance(value, (list, tuple, set)):
                raw_items = list(value)
            else:
                raw_items = [value]
            out: list[str] = []
            seen: set[str] = set()
            for item in raw_items:
                for part in str(item).split(","):
                    s = part.strip()
                    if not s:
                        continue
                    key = s.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(s)
            return out

        clauses: list = []
        params: list = []
        if q:
            like = f"%{q.strip().lower()}%"
            clauses.append(
                "(LOWER(name) LIKE ? OR LOWER(group_title) LIKE ?"
                " OR LOWER(country) LIKE ? OR LOWER(url) LIKE ?)"
            )
            params.extend([like, like, like, like])
        group_list = _split_filter_values(group)
        if len(group_list) == 1:
            clauses.append("group_title = ?")
            params.append(group_list[0])
        elif group_list:
            placeholders = ",".join("?" for _ in group_list)
            clauses.append(f"group_title IN ({placeholders})")
            params.extend(group_list)
        country_list = _split_filter_values(country)
        if country_list:
            from .geo import country_code_to_name, country_name_to_code

            country_parts: list[str] = []
            for raw in country_list:
                # Accept ISO code or English name from the UI.
                code = country_name_to_code(raw) or (
                    raw.strip().upper() if len(raw.strip()) == 2 else raw.strip().upper()
                )
                name = country_code_to_name(code) or raw.strip()
                # Many playlists put the country in group-title and leave country blank/GLOBAL.
                country_parts.append(
                    "("
                    " UPPER(COALESCE(country, '')) = ?"
                    " OR LOWER(COALESCE(group_title, '')) = LOWER(?)"
                    ")"
                )
                params.extend([code, name])
            if len(country_parts) == 1:
                clauses.append(country_parts[0])
            else:
                clauses.append("(" + " OR ".join(country_parts) + ")")
        if online_only:
            clauses.append("status = 'online'")
        elif status_in:
            cleaned = [s for s in status_in if s]
            if cleaned:
                placeholders = ",".join("?" for _ in cleaned)
                clauses.append(f"status IN ({placeholders})")
                params.extend(cleaned)
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if media_type:
            if media_type == "live":
                clauses.append("(media_type = 'live' OR media_type = 'unknown' OR media_type IS NULL OR media_type = '')")
            else:
                clauses.append("media_type = ?")
                params.append(media_type)
        if not include_test:
            clauses.append("(group_title IS NULL OR group_title != 'Test')")

        where = ""
        if clauses:
            where = " WHERE " + " AND ".join(clauses)

        total_row = self._execute_read(
            f"SELECT COUNT(*) FROM channels{where}", params
        ).fetchone()
        total = int(total_row[0]) if total_row else 0

        sort_key = (sort or "name").lower()
        order_col = _SORT_COLUMNS.get(sort_key, _SORT_COLUMNS["name"])
        direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        if sort_key in ("trending", "popular", "trend_score"):
            # Hot channels first; recent watches break ties.
            order_clause = (
                f" ORDER BY COALESCE(trend_score, 0) {direction},"
                f" COALESCE(last_watched_at, 0) DESC,"
                f" name COLLATE NOCASE ASC, url ASC"
            )
        elif sort_key in ("last_checked_at", "last_checked", "scanned", "recently_scanned"):
            # Never-checked rows (NULL) always last when sorting by scan time.
            order_clause = (
                f" ORDER BY (last_checked_at IS NULL) ASC,"
                f" last_checked_at {direction}, name COLLATE NOCASE ASC, url ASC"
            )
        else:
            order_clause = f" ORDER BY {order_col} {direction}, url ASC"

        offset = (page - 1) * limit
        query_sql = (
            f"SELECT * FROM channels{where}{order_clause} LIMIT ? OFFSET ?"
        )
        rows = self._execute_read(query_sql, (*params, limit, offset)).fetchall()
        channels = [coerce_dict(r) for r in rows]

        groups_rows = self._execute_read(
            "SELECT group_title, COUNT(*) FROM channels"
            " WHERE group_title IS NOT NULL AND group_title != ''"
            " GROUP BY group_title ORDER BY group_title COLLATE NOCASE"
        ).fetchall()
        from .geo import country_code_to_name, is_country_like_group

        # Category menu: drop country names (those belong in Countries).
        groups = [
            {"name": r[0], "count": int(r[1])}
            for r in groups_rows
            if not is_country_like_group(r[0])
        ]

        countries_rows = self._execute_read(
            "SELECT country, COUNT(*) FROM channels"
            " WHERE country IS NOT NULL AND country != ''"
            " AND UPPER(country) NOT IN ('GLOBAL', 'XX', 'ZZ', 'UNKNOWN', 'UNDEFINED')"
            " GROUP BY country ORDER BY country COLLATE NOCASE"
        ).fetchall()
        countries = []
        seen_codes: set[str] = set()
        for r in countries_rows:
            code = (r[0] or "").strip().upper()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            countries.append(
                {
                    "code": code,
                    "name": country_code_to_name(code) or code,
                    "count": int(r[1]),
                }
            )

        # Also surface countries that only appear as group-title labels.
        for r in groups_rows:
            label = r[0]
            if not is_country_like_group(label):
                continue
            from .geo import country_name_to_code

            code = country_name_to_code(label)
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            countries.append(
                {
                    "code": code,
                    "name": country_code_to_name(code) or label,
                    "count": int(r[1]),
                }
            )
        countries.sort(key=lambda c: (c.get("name") or c.get("code") or "").lower())

        total_pages = (total + limit - 1) // limit if limit > 0 else 1
        return {
            "channels": channels,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "has_more": page < total_pages,
            "countries": countries,
            "groups": groups,
            "revision": self.get_revision(),
        }

    def get_valid_channels(self, include_test: bool = False) -> list:
        """Return online channels — the primary feed for the public UI."""
        clauses = ["status = 'online'"]
        if not include_test:
            clauses.append("(group_title IS NULL OR group_title != 'Test')")
        sql = (
            "SELECT * FROM channels WHERE "
            + " AND ".join(clauses)
            + " ORDER BY name COLLATE NOCASE"
        )
        rows = self._execute_read(sql).fetchall()
        return [coerce_dict(r) for r in rows]

    def get_export_channels(self) -> list:
        """Return every online channel, including Test entries — for M3U export."""
        rows = self._execute_read(
            "SELECT * FROM channels WHERE status = 'online'"
            " ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [coerce_dict(r) for r in rows]

    def get_channel(self, url: str) -> Optional[dict]:
        if not url:
            return None
        row = self._execute_read(
            "SELECT * FROM channels WHERE url = ?", (url,)
        ).fetchone()
        return coerce_dict(row) if row else None

    def get_master_map(self) -> dict:
        """Return ``{url: channel_dict}`` used to merge with the legacy master cache."""
        rows = self._execute_read("SELECT * FROM channels").fetchall()
        return {r["url"]: coerce_dict(r) for r in rows}

    # -------------------------------------------------------------- checker

    def claim_check_batch(
        self,
        statuses: Sequence[str],
        limit: int,
        lease_sec: Optional[float] = None,
    ) -> list:
        """Atomically lease and return channels ready to be re-checked.

        Order: highest ``check_priority`` first, then never-checked rows, then
        oldest ``last_checked_at``. Rows whose ``check_leased_until`` is still in
        the future are skipped; every returned row is leased for ``lease_sec``
        (default :data:`DEFAULT_CHECK_LEASE_SEC`) so concurrent workers never get
        the same row. The lease is cleared by :meth:`update_channel_results`, or
        simply expires if the worker dies.

        Implemented as SELECT + UPDATE inside one ``BEGIN IMMEDIATE`` transaction
        (works on any SQLite version, no ``RETURNING`` needed).
        """
        if not statuses:
            return []
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 50
        try:
            lease = float(DEFAULT_CHECK_LEASE_SEC if lease_sec is None else lease_sec)
        except (TypeError, ValueError):
            lease = DEFAULT_CHECK_LEASE_SEC

        placeholders = ",".join("?" for _ in statuses)
        sql = (
            f"SELECT * FROM channels WHERE status IN ({placeholders})"
            " AND (check_leased_until IS NULL OR check_leased_until <= ?)"
            " ORDER BY COALESCE(check_priority, 0) DESC,"
            " (last_checked_at IS NULL) DESC, last_checked_at ASC, url ASC"
            " LIMIT ?"
        )
        now = time.time()
        lease_until = now + lease
        with self.transaction(immediate=True) as conn:
            rows = conn.execute(sql, (*statuses, now, limit)).fetchall()
            claimed = [coerce_dict(r) for r in rows]
            if claimed and lease > 0:
                conn.executemany(
                    "UPDATE channels SET check_leased_until = ? WHERE url = ?",
                    [(lease_until, ch["url"]) for ch in claimed],
                )
                for ch in claimed:
                    ch["check_leased_until"] = lease_until
        return claimed

    def claim_active_batch(
        self,
        limit: int,
        statuses: Optional[Sequence[str]] = None,
    ) -> list:
        """Claim online/pending/unknown channels for the active-health worker."""
        return self.claim_check_batch(statuses or ACTIVE_STATUSES, limit)

    def claim_dead_batch(
        self,
        limit: int,
        statuses: Optional[Sequence[str]] = None,
    ) -> list:
        """Claim offline/error channels for the dead-revival worker."""
        return self.claim_check_batch(statuses or DEAD_STATUSES, limit)

    def mark_priority_check(
        self,
        url: str,
        priority: int = DEFAULT_CHECK_PRIORITY,
    ) -> bool:
        """Force ``url`` to the front of the health queue.

        Sets ``check_priority`` (claimed highest-first) and clears
        ``last_checked_at`` so :meth:`claim_check_batch` picks it up before
        already-scanned rows. Used when a client reports a stream from the
        unchecked/dead directory actually played. Priority is reset to 0 by
        :meth:`update_channel_results` once the check lands.
        """
        url = (url or "").strip()
        if not url:
            return False
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = DEFAULT_CHECK_PRIORITY
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE channels SET check_priority = ?, last_checked_at = NULL,"
                " updated_at = ? WHERE url = ?",
                (priority, now, url),
            )
            changed = cur.rowcount
        if changed:
            self.bump_revision()
            return True
        return False

    def update_channel_results(self, results: Sequence[dict]) -> int:
        """Batch-apply check results.

        Only the status-related columns are touched here so metadata written by
        ingest (name, group_title, tvg_logo, ...) is never overwritten.
        """
        if not results:
            return 0

        rows_written = 0
        now = time.time()
        with self.transaction() as conn:
            for result in results:
                if not isinstance(result, dict):
                    continue
                url = result.get("url")
                if not url:
                    continue

                status = (result.get("status") or "unknown").lower()
                if status not in VALID_STATUSES:
                    status = "unknown"

                media_type = result.get("media_type")
                if media_type and media_type not in VALID_MEDIA_TYPES:
                    media_type = None

                playing_now = result.get("playing_now")
                fail_reason = result.get("fail_reason")
                fail_delta = result.get("fail_delta")
                explicit_fail_count = result.get("fail_count")

                assignments = [
                    "status = ?",
                    "updated_at = ?",
                    "last_checked_at = ?",
                    "check_priority = 0",
                    "check_leased_until = NULL",
                ]
                params: list = [status, now, now]

                if playing_now is not None:
                    assignments.append("playing_now = ?")
                    params.append(playing_now)
                if fail_reason is not None:
                    assignments.append("fail_reason = ?")
                    params.append(fail_reason)
                if media_type is not None:
                    assignments.append("media_type = ?")
                    params.append(media_type)

                if explicit_fail_count is not None:
                    assignments.append("fail_count = ?")
                    params.append(int(explicit_fail_count))
                elif status == "online":
                    assignments.append("fail_count = 0")
                elif fail_delta:
                    assignments.append(
                        "fail_count = COALESCE(fail_count, 0) + ?"
                    )
                    params.append(int(fail_delta))

                params.append(url)
                sql = f"UPDATE channels SET {', '.join(assignments)} WHERE url = ?"
                cur = conn.execute(sql, params)
                rows_written += cur.rowcount or 0

        if rows_written:
            self.bump_revision()
        return rows_written

    def reclassify_media_types(self, *, limit: int = 5000) -> int:
        """Fix false ``vod`` tags (e.g. ``.mov`` matching ``mov3.co`` hosts).

        HLS/DASH URLs and live YouTube/Twitch links become ``live``. Progressive
        files stay ``vod``. Idempotent.
        """
        from .normalize import classify_media_type

        rows = self._execute(
            "SELECT url, media_type FROM channels"
            " WHERE media_type IS NULL OR media_type IN ('', 'unknown', 'vod')"
            " LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        if not rows:
            return 0

        fixed = 0
        now = time.time()
        with self.transaction() as conn:
            for row in rows:
                url = row["url"] if hasattr(row, "keys") else row[0]
                prev = (row["media_type"] if hasattr(row, "keys") else row[1]) or ""
                new = classify_media_type(url)
                if new == "unknown" or new == (prev or "").lower():
                    continue
                cur = conn.execute(
                    "UPDATE channels SET media_type = ?, updated_at = ? WHERE url = ?",
                    (new, now, url),
                )
                fixed += int(cur.rowcount or 0)
        if fixed:
            self.bump_revision()
        return fixed

    def separate_country_groups(self) -> int:
        """Peel country/region names out of ``group_title`` into ``country``.

        Playlists often use Italy / UK / US as the M3U group. Those belong in
        the Countries filter, not Categories. Idempotent — safe to run on every
        startup.
        """
        from .geo import DEFAULT_CATEGORY_AFTER_COUNTRY_SPLIT, country_name_to_code

        rows = self._execute(
            "SELECT DISTINCT group_title FROM channels"
            " WHERE group_title IS NOT NULL AND TRIM(group_title) != ''"
        ).fetchall()
        if not rows:
            return 0

        moved = 0
        now = time.time()
        with self.transaction() as conn:
            for (label,) in rows:
                code = country_name_to_code(label)
                if not code:
                    continue
                cur = conn.execute(
                    "UPDATE channels SET"
                    " country = CASE"
                    "   WHEN country IS NULL OR TRIM(country) = ''"
                    "     OR UPPER(country) IN ('GLOBAL','XX','ZZ','UNKNOWN','UNDEFINED')"
                    "   THEN ? ELSE country END,"
                    " group_title = ?,"
                    " updated_at = ?"
                    " WHERE group_title = ?",
                    (code, DEFAULT_CATEGORY_AFTER_COUNTRY_SPLIT, now, label),
                )
                moved += int(cur.rowcount or 0)

        if moved:
            self.bump_revision()
        return moved

    # ------------------------------------------------------------- trending

    @staticmethod
    def _decay_factor(hours: float, half_life: float = TREND_HALF_LIFE_HOURS) -> float:
        if hours <= 0 or half_life <= 0:
            return 1.0
        return 0.5 ** (hours / half_life)

    def record_watch(
        self,
        url: str,
        *,
        min_interval_sec: float = WATCH_MIN_INTERVAL_SEC,
    ) -> Optional[dict]:
        """Record a channel play and bump its decaying trend score.

        Same URL is rate-limited to once per ``min_interval_sec``. Does not
        bump the catalog revision (avoids thrashing soft-refresh for everyone).
        """
        url = (url or "").strip()
        if not url:
            return None

        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT url, trend_score, watch_count, last_watched_at, trend_updated_at"
                " FROM channels WHERE url = ? OR url_norm = ? LIMIT 1",
                (url, normalize_url(url)),
            ).fetchone()
            if not row:
                return None

            target = row[0]
            score = float(row[1] or 0)
            watch_count = int(row[2] or 0)
            last_watched = row[3]
            trend_updated = row[4]

            if last_watched is not None and (now - float(last_watched)) < float(
                min_interval_sec
            ):
                return {
                    "ok": True,
                    "recorded": False,
                    "rate_limited": True,
                    "url": target,
                    "trend_score": score,
                    "watch_count": watch_count,
                }

            baseline = trend_updated if trend_updated is not None else last_watched
            if baseline is not None and score > 0:
                hours = max(0.0, (now - float(baseline)) / 3600.0)
                score *= self._decay_factor(hours)
            score += 1.0
            watch_count += 1

            conn.execute(
                "UPDATE channels SET trend_score = ?, watch_count = ?,"
                " last_watched_at = ?, trend_updated_at = ? WHERE url = ?",
                (score, watch_count, now, now, target),
            )
            conn.execute(
                "INSERT INTO channel_watches(url, watched_at) VALUES (?, ?)",
                (target, now),
            )

            # Prune occasionally (~1/40 of watches) to keep the event table small.
            if watch_count % 40 == 0:
                cutoff = now - (WATCH_PRUNE_DAYS * 86400.0)
                conn.execute(
                    "DELETE FROM channel_watches WHERE watched_at < ?",
                    (cutoff,),
                )

        return {
            "ok": True,
            "recorded": True,
            "rate_limited": False,
            "url": target,
            "trend_score": score,
            "watch_count": watch_count,
        }

    def decay_trend_scores(self) -> int:
        """Apply global exponential decay so inactive channels sink over time.

        Uses ``meta.trend_decay_at`` so repeated calls within a short window are
        no-ops. Updates ``trend_updated_at`` so the next watch does not
        double-decay the same period.
        """
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'trend_decay_at'"
            ).fetchone()
            try:
                last = float(row[0]) if row and row[0] is not None else None
            except (TypeError, ValueError):
                last = None

            if last is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('trend_decay_at', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(now),),
                )
                return 0

            hours = max(0.0, (now - last) / 3600.0)
            if hours < (1.0 / 60.0):  # < ~1 minute
                return 0

            factor = self._decay_factor(hours)
            if factor >= 0.9999:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('trend_decay_at', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(now),),
                )
                return 0

            cur = conn.execute(
                "UPDATE channels SET trend_score = trend_score * ?,"
                " trend_updated_at = ?"
                " WHERE trend_score IS NOT NULL AND trend_score > 0.0001",
                (factor, now),
            )
            conn.execute(
                "UPDATE channels SET trend_score = 0 WHERE trend_score IS NOT NULL"
                " AND trend_score <= 0.0001"
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('trend_decay_at', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(now),),
            )
            return int(cur.rowcount or 0)

    # -------------------------------------------------------------- viewers

    def heartbeat_viewer(self, session_id: str, url: str) -> bool:
        """Record that ``session_id`` is watching ``url`` right now.

        One row per session: switching channels simply moves the row. Stale
        rows are pruned opportunistically (about once a minute).
        """
        session_id = (session_id or "").strip()[:128]
        url = (url or "").strip()
        if not session_id or not url:
            return False
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO channel_viewers(session_id, url, last_seen) VALUES (?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " url = excluded.url, last_seen = excluded.last_seen",
                (session_id, url, now),
            )
            if now - self._last_viewer_prune >= 60.0:
                self._last_viewer_prune = now
                conn.execute(
                    "DELETE FROM channel_viewers WHERE last_seen < ?",
                    (now - VIEWER_PRUNE_SEC,),
                )
        return True

    def count_viewers(self, url: str, max_age: float = VIEWER_ACTIVE_SEC) -> int:
        """Sessions with a heartbeat for ``url`` within the last ``max_age`` seconds."""
        url = (url or "").strip()
        if not url:
            return 0
        row = self._execute(
            "SELECT COUNT(*) FROM channel_viewers WHERE url = ? AND last_seen >= ?",
            (url, time.time() - float(max_age)),
        ).fetchone()
        return int(row[0]) if row else 0

    def prune_viewers(self, max_age: float = VIEWER_PRUNE_SEC) -> int:
        """Delete heartbeats older than ``max_age`` seconds; returns rows removed."""
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM channel_viewers WHERE last_seen < ?",
                (time.time() - float(max_age),),
            )
            return int(cur.rowcount or 0)

    def viewer_counts_for_urls(
        self,
        urls: Iterable[str],
        max_age: float = VIEWER_ACTIVE_SEC,
    ) -> dict:
        """Return ``{url: active_viewers}`` for every url in ``urls`` (0 when none)."""
        wanted: list = []
        seen: set = set()
        for u in urls or ():
            if isinstance(u, str):
                u = u.strip()
                if u and u not in seen:
                    seen.add(u)
                    wanted.append(u)
        counts = {u: 0 for u in wanted}
        if not wanted:
            return counts
        cutoff = time.time() - float(max_age)
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._execute(
                "SELECT url, COUNT(*) FROM channel_viewers"
                f" WHERE last_seen >= ? AND url IN ({placeholders}) GROUP BY url",
                (cutoff, *chunk),
            ).fetchall()
            for r in rows:
                counts[r[0]] = int(r[1])
        return counts

    # ------------------------------------------------------------- host geo

    def get_host_country(self, hostname: str) -> Optional[str]:
        """Cached country for ``hostname`` (``None`` if unknown or stale)."""
        host = (hostname or "").strip().lower()
        if not host:
            return None
        row = self._execute(
            "SELECT country, resolved_at FROM host_geo WHERE hostname = ?",
            (host,),
        ).fetchone()
        if not row or not row[0]:
            return None
        resolved_at = row[1]
        if resolved_at is not None and (
            time.time() - float(resolved_at) > HOST_GEO_TTL_DAYS * 86400.0
        ):
            return None
        return str(row[0])

    def set_host_country(self, hostname: str, country: str) -> None:
        host = (hostname or "").strip().lower()
        code = (country or "").strip().upper()
        if not host or not code:
            return
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO host_geo(hostname, country, resolved_at) VALUES (?, ?, ?)"
                " ON CONFLICT(hostname) DO UPDATE SET"
                " country = excluded.country, resolved_at = excluded.resolved_at",
                (host, code, time.time()),
            )

    def backfill_server_countries(self, limit: int = 25) -> int:
        """Geolocate stream hosts for channels whose country is blank / GLOBAL.

        Walks ``url`` order using a persisted cursor (``meta.geo_backfill_cursor``)
        so unresolvable rows do not starve the rest, wrapping around when done.
        Network lookups happen outside the DB lock and honour the geo rate limit
        (this call may therefore block for a while). Returns rows updated.
        """
        from .geo import is_placeholder_country, resolve_server_country_from_url

        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 25

        cursor_url = self.get_meta("geo_backfill_cursor", "") or ""
        sql = (
            "SELECT url FROM channels WHERE url > ?"
            " AND (country IS NULL OR TRIM(country) = ''"
            "   OR UPPER(country) IN ('GLOBAL','XX','ZZ','UNKNOWN','UNDEFINED'))"
            " ORDER BY url LIMIT ?"
        )
        rows = self._execute(sql, (cursor_url, limit)).fetchall()
        if not rows:
            if cursor_url:
                self.set_meta("geo_backfill_cursor", "")
            return 0

        updates: list = []
        for row in rows:
            url = row[0]
            try:
                code = resolve_server_country_from_url(url, block=True, store=self)
            except Exception:  # noqa: BLE001
                logger.debug("host geo failed for %s", url, exc_info=True)
                code = None
            if code and not is_placeholder_country(code):
                updates.append((code, url))

        updated = 0
        now = time.time()
        with self.transaction() as conn:
            for code, url in updates:
                cur = conn.execute(
                    "UPDATE channels SET country = ?, updated_at = ? WHERE url = ?"
                    " AND (country IS NULL OR TRIM(country) = ''"
                    "   OR UPPER(country) IN ('GLOBAL','XX','ZZ','UNKNOWN','UNDEFINED'))",
                    (code, now, url),
                )
                updated += int(cur.rowcount or 0)
        self.set_meta("geo_backfill_cursor", rows[-1][0])
        if updated:
            self.bump_revision()
        return updated

    # --------------------------------------------------------------- ingest

    def upsert_from_ingest(self, channels: Sequence[dict]) -> dict:
        """Insert or update channel metadata from an ingest pass.

        * New URLs (or URLs whose ``url_norm`` isn't already in the store) are
          inserted with ``status='pending'`` so the checker picks them up on the
          next batch — we never bulk-flag existing channels offline here.
        * Existing rows keep their status; only descriptive metadata is updated
          (name, tvg_id, tvg_logo, group_title, country, ...).
        * Normalization helpers are always applied before writing.
        * Commits in chunks so the UI / health workers can interleave reads and
          claims instead of waiting on one multi-minute write lock.

        Returns a summary dict: ``{new, updated, skipped}``.
        """
        summary = {"new": 0, "updated": 0, "skipped": 0}
        if not channels:
            return summary

        chunk_size = 200
        now = time.time()
        items = list(channels)

        for start in range(0, len(items), chunk_size):
            chunk = items[start : start + chunk_size]
            with self.transaction() as conn:
                for raw in chunk:
                    if not isinstance(raw, dict):
                        summary["skipped"] += 1
                        continue
                    ch = dict(raw)
                    url = (ch.get("url") or "").strip()
                    if not url:
                        summary["skipped"] += 1
                        continue
                    ch["url"] = url
                    normalize_channel(ch)
                    url_norm = ch.get("url_norm") or normalize_url(url)

                    existing = conn.execute(
                        "SELECT url FROM channels WHERE url = ?"
                        " UNION ALL SELECT url FROM channels WHERE url_norm = ?"
                        " LIMIT 1",
                        (url, url_norm),
                    ).fetchone()

                    if existing:
                        target_url = existing[0]
                        updates: list = []
                        params: list = []
                        for column in (
                            "name",
                            "tvg_id",
                            "tvg_logo",
                            "group_title",
                            "country",
                            "icon_url",
                            "audio_language",
                            "variant_of",
                            "variant_quality",
                            "variant_bandwidth",
                            "http_user_agent",
                            "http_referrer",
                        ):
                            val = ch.get(column)
                            if column == "country" and (
                                str(val or "").strip().upper() in _PLACEHOLDER_COUNTRIES
                            ):
                                # Never downgrade a known (host-geo) country to GLOBAL.
                                continue
                            if val not in (None, ""):
                                updates.append(f"{column} = ?")
                                params.append(val)

                        media_type = ch.get("media_type")
                        if media_type and media_type in VALID_MEDIA_TYPES and media_type != "unknown":
                            updates.append(
                                "media_type = COALESCE(NULLIF(media_type, 'unknown'), ?)"
                            )
                            params.append(media_type)

                        if url_norm:
                            updates.append("url_norm = COALESCE(url_norm, ?)")
                            params.append(url_norm)

                        updates.append("updated_at = ?")
                        params.append(now)

                        params.append(target_url)
                        conn.execute(
                            f"UPDATE channels SET {', '.join(updates)} WHERE url = ?",
                            params,
                        )
                        summary["updated"] += 1
                        continue

                    media_type = ch.get("media_type") or classify_media_type(url)
                    if media_type not in VALID_MEDIA_TYPES:
                        media_type = "unknown"

                    conn.execute(
                        "INSERT INTO channels ("
                        " url, url_norm, name, tvg_id, tvg_logo, group_title,"
                        " playing_now, status, country, icon_url, audio_language,"
                        " variant_of, media_type, fail_reason, fail_count,"
                        " last_checked_at, updated_at, variant_quality, variant_bandwidth,"
                        " http_user_agent, http_referrer"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            url,
                            url_norm,
                            ch.get("name"),
                            ch.get("tvg_id"),
                            ch.get("tvg_logo"),
                            ch.get("group_title"),
                            ch.get("playing_now"),
                            "pending",
                            ch.get("country"),
                            ch.get("icon_url"),
                            ch.get("audio_language"),
                            ch.get("variant_of"),
                            media_type,
                            None,
                            0,
                            None,
                            now,
                            ch.get("variant_quality"),
                            ch.get("variant_bandwidth"),
                            ch.get("http_user_agent"),
                            ch.get("http_referrer"),
                        ),
                    )
                    summary["new"] += 1
            # Yield the write lock between chunks so Waitress can serve /channels.
            time.sleep(0.01)

        if summary["new"] or summary["updated"]:
            self.bump_revision()
        return summary

    # ------------------------------------------------------------- variants

    def replace_variants(
        self,
        channel_url: str,
        variants: Sequence[dict],
    ) -> int:
        """Replace the variant set for ``channel_url`` atomically."""
        if not channel_url:
            return 0
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM channel_variants WHERE channel_url = ?",
                (channel_url,),
            )
            written = 0
            for variant in variants or ():
                if not isinstance(variant, dict):
                    continue
                v_url = (variant.get("url") or variant.get("variant_url") or "").strip()
                if not v_url:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO channel_variants ("
                    " variant_url, channel_url, resolution, bandwidth, codecs, audio_language"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        v_url,
                        channel_url,
                        variant.get("resolution") or variant.get("variant_quality"),
                        int(variant["bandwidth"])
                        if str(variant.get("bandwidth") or "").isdigit()
                        else variant.get("variant_bandwidth"),
                        variant.get("codecs"),
                        variant.get("audio_language"),
                    ),
                )
                written += 1
            return written

    def get_variants(self, channel_url: str) -> list:
        if not channel_url:
            return []
        rows = self._execute(
            "SELECT * FROM channel_variants WHERE channel_url = ?"
            " ORDER BY bandwidth DESC",
            (channel_url,),
        ).fetchall()
        return [coerce_dict(r) for r in rows]

    # -------------------------------------------------------- invalid links

    def get_invalid_links(self) -> list:
        rows = self._execute(
            "SELECT url FROM invalid_links ORDER BY created_at DESC, url"
        ).fetchall()
        return [r[0] for r in rows]

    def set_invalid_links(self, urls: Iterable[str]) -> int:
        now = time.time()
        with self.transaction() as conn:
            conn.execute("DELETE FROM invalid_links")
            payload = [
                (str(u).strip(), now)
                for u in (urls or ())
                if isinstance(u, str) and u.strip()
            ]
            if payload:
                conn.executemany(
                    "INSERT OR IGNORE INTO invalid_links(url, created_at) VALUES (?, ?)",
                    payload,
                )
            return len(payload)

    def add_invalid_links(self, urls: Iterable[str]) -> int:
        now = time.time()
        payload = [
            (str(u).strip(), now)
            for u in (urls or ())
            if isinstance(u, str) and u.strip()
        ]
        if not payload:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO invalid_links(url, created_at) VALUES (?, ?)",
                payload,
            )
        return len(payload)

    # ------------------------------------------------------------- SEO slugs

    def save_seo_slugs(self, slug_order: Sequence[str], slugs: dict) -> int:
        """Persist the slug manifest. ``slugs`` may map slug→name or slug→dict."""
        if not slug_order:
            with self.transaction() as conn:
                conn.execute("DELETE FROM seo_slugs")
            return 0

        rows: list = []
        for slug in slug_order:
            if not slug:
                continue
            entry = slugs.get(slug) if isinstance(slugs, dict) else None
            channel_name = None
            channel_url = None
            if isinstance(entry, dict):
                channel_name = entry.get("name") or entry.get("channel_name")
                channel_url = entry.get("url") or entry.get("channel_url")
            elif isinstance(entry, str):
                channel_name = entry
            rows.append((slug, channel_url, channel_name))

        with self.transaction() as conn:
            conn.execute("DELETE FROM seo_slugs")
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO seo_slugs("
                    " slug, channel_url, channel_name"
                    ") VALUES (?, ?, ?)",
                    rows,
                )
        return len(rows)

    def get_seo_slug(self, slug: str) -> Optional[dict]:
        if not slug:
            return None
        row = self._execute(
            "SELECT slug, channel_url, channel_name FROM seo_slugs WHERE slug = ?",
            (slug,),
        ).fetchone()
        return coerce_dict(row) if row else None

    def all_seo_slugs(self) -> list:
        rows = self._execute(
            "SELECT slug, channel_url, channel_name FROM seo_slugs"
            " ORDER BY slug"
        ).fetchall()
        return [coerce_dict(r) for r in rows]

    # ----------------------------------------------------------- playlist queue

    def enqueue_playlist(self, url: str, depth: int = 0) -> bool:
        if not url:
            return False
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO playlist_queue(url, depth, created_at)"
                " VALUES (?, ?, ?)",
                (url, int(depth), time.time()),
            )
            return (cur.rowcount or 0) > 0

    def pop_playlist_batch(self, limit: int = 10) -> list:
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 10
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT url, depth, created_at FROM playlist_queue"
                " ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                conn.executemany(
                    "DELETE FROM playlist_queue WHERE url = ?",
                    [(r[0],) for r in rows],
                )
        return [coerce_dict(r) for r in rows]

    # ---------------------------------------------------------------- purge

    def purge_catalog(
        self,
        *,
        keep_host_geo: bool = True,
        vacuum: bool = True,
    ) -> dict:
        """Wipe channel catalog and related tables so ingest can rebuild fresh.

        Clears channels, variants, watches, viewers, SEO slugs, playlist queue,
        and invalid-link denylist. Meta ``revision`` is reset to 0 and bumped.
        Host→country cache is kept by default (safe to reuse across rescans).

        Returns a summary of deleted row counts.
        """
        tables = (
            "channel_viewers",
            "channel_watches",
            "channel_variants",
            "seo_slugs",
            "playlist_queue",
            "invalid_links",
            "channels",
        )
        deleted: dict[str, int] = {}
        with self.transaction() as conn:
            for table in tables:
                before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                count_before = int(before[0]) if before else 0
                conn.execute(f"DELETE FROM {table}")
                deleted[table] = count_before
            if not keep_host_geo:
                before = conn.execute("SELECT COUNT(*) FROM host_geo").fetchone()
                deleted["host_geo"] = int(before[0]) if before else 0
                conn.execute("DELETE FROM host_geo")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('revision', '0')"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('purged_at', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(time.time()),),
            )
            # Drop ingest cursors so geo-backfill starts over cleanly.
            for key in ("geo_backfill_cursor",):
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))

        self.bump_revision()
        if vacuum:
            try:
                # VACUUM cannot run inside a transaction; best-effort reclaim.
                with self._lock:
                    self._conn.execute("VACUUM")
            except Exception as exc:  # noqa: BLE001
                logger.debug("VACUUM after purge skipped: %s", exc)
        deleted["revision"] = self.get_revision()
        return deleted

    # ---------------------------------------------------------------- backup

    def backup(self, dest: Optional[str] = None) -> str:
        """Copy the live database to ``data/backups/<timestamp>.db`` via SQLite."""
        if dest is None:
            backups_dir = os.path.join(
                os.path.dirname(os.path.abspath(self.path)) or ".",
                "backups",
            )
            os.makedirs(backups_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = os.path.join(backups_dir, f"iptv-{stamp}.db")
        else:
            parent = os.path.dirname(os.path.abspath(dest))
            if parent:
                os.makedirs(parent, exist_ok=True)

        with self._lock:
            if self._apsw:
                self._conn.backup_to(dest)
                return dest
            try:
                target = sqlite3.connect(dest)
                try:
                    self._conn.backup(target)
                finally:
                    target.close()
            except sqlite3.Error:
                # Fallback for very old sqlite builds that lack .backup().
                self._conn.commit()
                shutil.copyfile(self.path, dest)
        return dest

    # ---------------------------------------------------------- json import

    def import_from_json_files(self, jsons_dir: str = LEGACY_JSON_DIR) -> int:
        """One-shot bulk import from the legacy ``jsons/*.json`` files.

        Safe to call more than once — existing rows are merged in place and
        their status is left alone. The function returns the number of rows
        that were inserted or updated.
        """
        touched = 0
        streams_path = os.path.join(jsons_dir, "IPTV_STREAMS_FILE.json")
        dead_path = os.path.join(jsons_dir, "DEAD_STREAMS_FILE.json")
        master_path = os.path.join(jsons_dir, "MASTER_CACHE_FILE.json")
        invalid_path = os.path.join(jsons_dir, "INVALID_LINKS_FILE.json")
        slugs_path = os.path.join(jsons_dir, "SEO_SLUG_MANIFEST.json")

        online = _load_json_list(streams_path)
        dead = _load_json_list(dead_path)
        master = _load_json_list(master_path)

        touched += self._import_channel_list(online, default_status="online")
        touched += self._import_channel_list(dead, default_status="offline")
        touched += self._import_channel_list(master, default_status=None)

        invalid = _load_json_list(invalid_path)
        if isinstance(invalid, list):
            urls = [u for u in invalid if isinstance(u, str) and u.strip()]
            if urls:
                self.add_invalid_links(urls)

        slug_data = _load_json_file(slugs_path)
        if isinstance(slug_data, dict):
            order = slug_data.get("slug_order") or []
            slugs = slug_data.get("slugs") or {}
            if order:
                try:
                    self.save_seo_slugs(order, slugs)
                except Exception as exc:
                    logger.debug("Legacy slug import failed: %s", exc)

        if touched:
            self.set_meta("legacy_import_at", str(time.time()))
        return touched

    def _import_channel_list(
        self,
        entries: Iterable[dict],
        default_status: Optional[str],
    ) -> int:
        if not entries:
            return 0

        touched = 0
        now = time.time()
        with self.transaction() as conn:
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                ch = dict(raw)
                url = (ch.get("url") or "").strip()
                if not url:
                    continue
                ch["url"] = url
                normalize_channel(ch)
                url_norm = ch.get("url_norm") or normalize_url(url)

                status = (ch.get("status") or default_status or "unknown").lower()
                if status not in VALID_STATUSES:
                    status = "unknown"
                if is_test_channel(ch) and ch.get("group_title") != "Test":
                    ch["group_title"] = "Test"

                media_type = ch.get("media_type") or classify_media_type(url)
                if media_type not in VALID_MEDIA_TYPES:
                    media_type = "unknown"

                existing = conn.execute(
                    "SELECT url FROM channels WHERE url = ?"
                    " UNION ALL SELECT url FROM channels WHERE url_norm = ?"
                    " LIMIT 1",
                    (url, url_norm),
                ).fetchone()

                if existing:
                    target_url = existing[0]
                    conn.execute(
                        "UPDATE channels SET"
                        "  name           = COALESCE(NULLIF(?, ''), name),"
                        "  tvg_id         = COALESCE(NULLIF(?, ''), tvg_id),"
                        "  tvg_logo       = COALESCE(NULLIF(?, ''), tvg_logo),"
                        "  group_title    = COALESCE(NULLIF(?, ''), group_title),"
                        "  playing_now    = COALESCE(NULLIF(?, ''), playing_now),"
                        "  country        = COALESCE(NULLIF(?, ''), country),"
                        "  icon_url       = COALESCE(NULLIF(?, ''), icon_url),"
                        "  audio_language = COALESCE(NULLIF(?, ''), audio_language),"
                        "  variant_of     = COALESCE(NULLIF(?, ''), variant_of),"
                        "  variant_quality= COALESCE(NULLIF(?, ''), variant_quality),"
                        "  variant_bandwidth = COALESCE(?, variant_bandwidth),"
                        "  media_type     = CASE WHEN media_type IN ('unknown', '') OR media_type IS NULL"
                        "                        THEN ? ELSE media_type END,"
                        "  url_norm       = COALESCE(url_norm, ?),"
                        "  updated_at     = ?"
                        " WHERE url = ?",
                        (
                            ch.get("name"),
                            ch.get("tvg_id"),
                            ch.get("tvg_logo"),
                            ch.get("group_title"),
                            ch.get("playing_now"),
                            ch.get("country"),
                            ch.get("icon_url"),
                            ch.get("audio_language"),
                            ch.get("variant_of"),
                            ch.get("variant_quality"),
                            ch.get("variant_bandwidth"),
                            media_type,
                            url_norm,
                            now,
                            target_url,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO channels ("
                        " url, url_norm, name, tvg_id, tvg_logo, group_title,"
                        " playing_now, status, country, icon_url, audio_language,"
                        " variant_of, media_type, fail_reason, fail_count,"
                        " last_checked_at, updated_at, variant_quality, variant_bandwidth"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            url,
                            url_norm,
                            ch.get("name"),
                            ch.get("tvg_id"),
                            ch.get("tvg_logo"),
                            ch.get("group_title"),
                            ch.get("playing_now"),
                            status,
                            ch.get("country"),
                            ch.get("icon_url"),
                            ch.get("audio_language"),
                            ch.get("variant_of"),
                            media_type,
                            ch.get("fail_reason"),
                            int(ch.get("fail_count") or 0),
                            ch.get("last_checked_at"),
                            now,
                            ch.get("variant_quality"),
                            ch.get("variant_bandwidth"),
                        ),
                    )
                touched += 1
        return touched


# --------------------------------------------------------------- helpers

def _load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("Failed to read %s: %s", path, exc)
        return None


def _load_json_list(path: str) -> list:
    data = _load_json_file(path)
    if isinstance(data, list):
        return data
    return []


# ------------------------------------------------------------------- CLI

def _cli(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = list(argv[1:])
    command = args[0].lower() if args else "ensure"

    if command in ("ensure", "init", "migrate"):
        store = ensure_db()
        try:
            total = store.count_channels(exclude_test=False)
            print(f"Database ready at {store.path} ({total} channels, revision {store.get_revision()})")
        finally:
            store.close()
        return 0

    if command == "backup":
        store = ensure_db()
        try:
            dest = store.backup()
            print(f"Backup written to {dest}")
        finally:
            store.close()
        return 0

    if command in ("purge", "reset"):
        store = ensure_db()
        try:
            before = store.count_channels(exclude_test=False)
            summary = store.purge_catalog()
            print(f"Purged catalog at {store.path}")
            print(f"  channels removed : {summary.get('channels', before)}")
            print(f"  revision         : {summary.get('revision')}")
        finally:
            store.close()
        return 0

    if command in ("stats", "info"):
        store = ensure_db()
        try:
            total = store.count_channels(exclude_test=False)
            online = store.count_channels(status="online", exclude_test=False)
            offline = store.count_channels(status="offline", exclude_test=False)
            pending = store.count_channels(status="pending", exclude_test=False)
            print(f"Path        : {store.path}")
            print(f"Revision    : {store.get_revision()}")
            print(f"Channels    : {total}")
            print(f"  online    : {online}")
            print(f"  offline   : {offline}")
            print(f"  pending   : {pending}")
        finally:
            store.close()
        return 0

    print(
        f"Unknown command: {command}. Use 'ensure', 'backup', 'purge', or 'stats'.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
