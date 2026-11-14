"""ChannelStore claim leases + priority ordering."""

from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from features.storage.db import ChannelStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    path = str(tmp_path / "test.db")
    s = ChannelStore(path)
    s._init_schema()
    yield s
    s.close()


def _insert(store: ChannelStore, url: str, status: str = "pending", **extra):
    now = time.time()
    cols = {
        "url": url,
        "url_norm": url,
        "name": url,
        "status": status,
        "group_title": "News",
        "country": "US",
        "media_type": "live",
        "updated_at": now,
        "last_checked_at": extra.get("last_checked_at", now - 100),
        "check_priority": extra.get("check_priority", 0),
        "check_leased_until": extra.get("check_leased_until"),
    }
    keys = ", ".join(cols.keys())
    placeholders = ", ".join("?" for _ in cols)
    store._execute(
        f"INSERT INTO channels ({keys}) VALUES ({placeholders})",
        tuple(cols.values()),
    )
    store._conn.commit()


def test_claim_skips_active_lease(store):
    _insert(store, "http://a.example/1.m3u8", check_leased_until=time.time() + 60)
    _insert(store, "http://b.example/2.m3u8", last_checked_at=None)
    claimed = store.claim_check_batch(["pending"], 10, lease_sec=30)
    urls = {c["url"] for c in claimed}
    assert "http://b.example/2.m3u8" in urls
    assert "http://a.example/1.m3u8" not in urls


def test_priority_claimed_first(store):
    _insert(store, "http://low.example/1.m3u8", check_priority=0, last_checked_at=None)
    _insert(store, "http://high.example/2.m3u8", check_priority=90, last_checked_at=None)
    claimed = store.claim_check_batch(["pending"], 1, lease_sec=30)
    assert len(claimed) == 1
    assert claimed[0]["url"] == "http://high.example/2.m3u8"


def test_mark_priority_and_clear_on_result(store):
    _insert(store, "http://x.example/1.m3u8")
    assert store.mark_priority_check("http://x.example/1.m3u8", priority=100)
    row = store.get_channel("http://x.example/1.m3u8")
    assert int(row.get("check_priority") or 0) == 100
    assert row.get("last_checked_at") is None
    store.update_channel_results(
        [{"url": "http://x.example/1.m3u8", "status": "online", "fail_count": 0}]
    )
    row = store.get_channel("http://x.example/1.m3u8")
    assert int(row.get("check_priority") or 0) == 0
    assert row.get("status") == "online"


def test_viewer_heartbeat(store):
    _insert(store, "http://v.example/1.m3u8", status="online")
    assert store.heartbeat_viewer("sess-1", "http://v.example/1.m3u8")
    assert store.count_viewers("http://v.example/1.m3u8") >= 1
