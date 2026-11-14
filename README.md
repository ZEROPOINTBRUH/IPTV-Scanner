# IPTV Scanner

Live TV guide and channel scanner. Scrapes public IPTV playlists, stores them in **SQLite**, health-checks streams in the background, and serves a Roku-style web guide plus a public channels API.

Live demo: https://iptv.bbyte.app/

## Requirements

- Python 3.10+ (3.12+ recommended for optional FastWorker)
- pip

## Quick start

```bash
git clone https://github.com/ZEROPOINTBRUH/iptv-scanner.git
cd iptv-scanner
pip install -r requirements.txt
python main.py
```

On a TTY, startup asks whether to **continue** with the existing catalog or **purge and fetch fresh** sources. It waits **30s** then auto-selects **[1] Continue** (set `IPTV_STARTUP_MENU_TIMEOUT_SEC=0` to disable). Non-interactive options:

```bash
python main.py --fresh --yes          # wipe catalog, then ingest
IPTV_STARTUP_PURGE=1 python main.py   # same via env
python -m features.storage.db purge   # wipe only (no server)
```

While the app is running: **More → Purge** requires `IPTV_ADMIN_PASSWORD` (falls back to `IPTV_PLAYLIST_SECRET`). Example:

```bash
curl -sS -X POST http://127.0.0.1:40006/admin/purge \
  -H "Content-Type: application/json" \
  -d '{"confirm":"purge","password":"YOUR_PASSWORD"}'
```

HTTP purge is **disabled** until a password is set. Attempts are rate-limited; a successful wipe starts a cooldown.

Open http://127.0.0.1:40006

The app serves with **Waitress** when installed (falls back to Flask’s dev server). Catalog data lives in `data/iptv.db` (override with `IPTV_DB_PATH`).

## Architecture

```
Sources (iptv-org / Free-TV / regional M3Us / playlists.txt)
        → ingest (pending rows, host-geo “Servers”)
        → active health + dead revival (+ client priority signals)
        → SQLite ChannelStore
        → Flask + Waitress → guide UI / M3U / JSON API / SEO
```

## Public API (no key)

Canonical catalog (reads SQLite live — not a static fake JSON file):

| Style | URL |
|-------|-----|
| REST | `GET /api/v1/channels` |
| Paged M3U | `GET /api/v1/channels.m3u` |
| Full active playlist | `GET /api/v1/playlist.m3u` |
| File-style JSON alias | `GET /streams.json` (same builder as REST) |

Useful query params: `page`, `limit`, `status` (default `online`), `server`, `group`, `q`, `sort` (`trending` \| `name` \| `server` \| `group`), `include=facets`.

Docs: [/api](http://127.0.0.1:40006/api) · Playground: [/api/playground](http://127.0.0.1:40006/api/playground) · OpenAPI: [/openapi.yaml](http://127.0.0.1:40006/openapi.yaml) · Discovery: `/api/v1`

Access is **public** (no API key). Catalog endpoints are lightly rate-limited; respect `wait_seconds` / HTTP 429.

## Web guide

- Desktop: left nav (Search, Home, Categories, Servers, My list, …)
- Mobile/tablet: bottom tabs
- Horizontal channel rails, custom player, prev/next, HTTP insecure warning
- Client playback signals help promote/demote streams; viewer heartbeats via `/api/watching`

## Important environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `IPTV_DB_PATH` | `data/iptv.db` | SQLite path |
| `IPTV_BIND_HOST` | `0.0.0.0` | Bind address |
| `IPTV_PORT` | `40006` | HTTP port |
| `IPTV_PUBLIC_BASE_URL` | _(empty)_ | Absolute URL base for API/M3U links |
| `IPTV_PLAYLIST_SECRET` | _(empty)_ | Optional `?token=` for full playlist / backup |
| `IPTV_ADMIN_PASSWORD` | _(falls back to playlist secret)_ | **Required** for HTTP catalog purge |
| `IPTV_ADMIN_PURGE_BURST` | `3` | Purge attempt burst before rate limit |
| `IPTV_ADMIN_PURGE_MIN_INTERVAL_SEC` | `60` | Seconds between purge attempt tokens |
| `IPTV_ADMIN_PURGE_COOLDOWN_SEC` | `600` | Cooldown after a successful purge |
| `IPTV_STARTUP_PURGE` | off | If set, purge catalog on startup (use with care) |
| `IPTV_STARTUP_MENU_TIMEOUT_SEC` | `30` | TTY menu wait before auto **[1] Continue** (`0` = wait forever) |
| `IPTV_TRUST_PROXY` | off | Trust `X-Forwarded-For` only when set |
| `IPTV_PROXY_MAX_CONCURRENT` | `32` | Max concurrent `/proxy/stream` slots |
| `IPTV_WAITRESS_THREADS` | _(config)_ | Waitress worker threads |
| `IPTV_DB_DRIVER` | apsw→sqlite3 | Force `sqlite3` to skip APSW |
| `IPTV_API_MIN_INTERVAL_SEC` | `2` | Public API rate interval |
| `IPTV_API_BURST` | `3` | Public API burst |
| `IPTV_VALIDATE_CONCURRENCY` | `12` | Parallel stream probes per health batch |
| `IPTV_ACTIVE_HEALTH_BATCH` | `40` | Channels claimed per active-health pass |
| `IPTV_PROBE_TOTAL_SEC` | `8` | Per-request timeout during validation |

## Tests

```bash
pip install pytest
pytest -q
```

## Project layout

- `main.py` — bootstrap (DB, task queue, Waitress)
- `features/storage/` — SQLite store, host geo, normalize
- `features/workers/` — health / ingest task queue
- `features/public_api.py` — universal channels API
- `templates/` — Jinja pages
- `static/` — JS/CSS/icons/SW

Legacy JSON workers under `features/workers/workers.py` are **not** used by `main.py` (SQLite path only).
