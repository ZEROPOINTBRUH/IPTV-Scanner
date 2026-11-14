"""Regression: get_default_store must not deadlock when the singleton is empty."""

from __future__ import annotations

import os
import sys
import threading

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_get_default_store_no_deadlock(tmp_path, monkeypatch):
    db_path = str(tmp_path / "solo.db")
    monkeypatch.setenv("IPTV_DB_PATH", db_path)
    monkeypatch.setenv("IPTV_DB_DRIVER", "sqlite3")

    import features.storage.db as dbmod

    dbmod._default_store = None
    dbmod.DEFAULT_DB_PATH = db_path

    result = {}

    def _run():
        try:
            store = dbmod.get_default_store()
            result["ok"] = True
            result["path"] = store.path
            store.close()
        except Exception as exc:  # noqa: BLE001
            result["err"] = str(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "get_default_store deadlocked"
    assert result.get("ok") is True
    dbmod._default_store = None
