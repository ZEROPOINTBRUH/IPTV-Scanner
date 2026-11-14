"""Smoke tests for universal /api/v1/channels builder."""

from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    db_path = str(tmp_path_factory.mktemp("db") / "api.db")
    os.environ["IPTV_DB_PATH"] = db_path
    os.environ["IPTV_DB_DRIVER"] = "sqlite3"
    os.environ["IPTV_TRUST_PROXY"] = "0"

    import features.storage.db as dbmod

    dbmod._default_store = None
    dbmod.DEFAULT_DB_PATH = db_path

    store = dbmod.ensure_db(db_path)
    now = time.time()
    store._execute(
        "INSERT INTO channels (url, url_norm, name, status, group_title, country,"
        " media_type, updated_at, last_checked_at, trend_score)"
        " VALUES (?, ?, ?, 'online', 'News', 'US', 'live', ?, ?, 1.0)",
        (
            "https://example.com/live.m3u8",
            "https://example.com/live.m3u8",
            "Example News",
            now,
            now,
        ),
    )
    store._conn.commit()

    from app_factory import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    yield client, store
    store.close()
    dbmod._default_store = None


def test_api_v1_channels_slim_envelope(app_client):
    client, _store = app_client
    resp = client.get("/api/v1/channels?page=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("ok") is True
    assert "channels" in data
    assert "support" not in data
    assert "works_with" not in data
    assert isinstance(data["channels"], list)
    if data["channels"]:
        ch = data["channels"][0]
        assert "server" in ch or "country" in ch
        assert "url" in ch
        assert "name" in ch


def test_streams_json_alias_same_builder(app_client):
    client, _store = app_client
    a = client.get("/api/v1/channels").get_json()
    b = client.get("/streams.json")
    assert b.status_code == 200
    body = b.get_json()
    legacy = body.get("channels") or body.get("streams") or []
    assert len(legacy) == len(a.get("channels") or [])
