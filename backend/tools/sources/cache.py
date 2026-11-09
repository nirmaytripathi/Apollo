"""Persistent cache of source-API results in SQLite (config.SOURCE_CACHE_DB).

Every successful API answer is stored; the cache is READ only when a domain's primary and fallback
both fail (router.fetch). An entry past its domain's TTL is kept and marked stale, never deleted
automatically, and never presented as live.

Thread safety: runs execute in background threads, so every call opens its own short-lived connection
(no connection is shared between threads), writes run in one short transaction, and SQLite's busy
timeout makes a second writer wait instead of failing. WAL mode lets readers proceed during a write.
`_lock` guards the only shared in-memory state: which database files already have their table.
No lock is ever held during an external API call: the router calls the cache only before or after one.
"""

import json
import os
import sqlite3
import threading
import time

from core import config

_SCHEMA = """CREATE TABLE IF NOT EXISTS source_cache (
    key TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    query TEXT NOT NULL,
    params TEXT NOT NULL,
    source TEXT NOT NULL,
    results TEXT NOT NULL,
    retrieved_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
)"""

_lock = threading.Lock()
_ready: set[str] = set()  # database paths whose table exists (guarded by _lock)


def _connect() -> sqlite3.Connection:
    path = config.SOURCE_CACHE_DB
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path, timeout=config.SOURCE_CACHE_BUSY_SECONDS)
    with _lock:
        if path not in _ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.commit()
            _ready.add(path)
    return conn


def ttl_seconds(domain: str) -> int:
    return int(config.SOURCE_CACHE_TTL_DAYS.get(domain, config.SOURCE_CACHE_TTL_DAYS["general"]) * 86400)


def put(key: str, domain: str, query: str, params: dict, source: str, results: list[dict],
        now: float | None = None) -> bool:
    """Store (or replace) one answer. Returns False instead of raising when the cache cannot be written."""
    now = int(now if now is not None else time.time())
    try:
        conn = _connect()
        try:
            with conn:  # one transaction: committed on success, rolled back on error
                conn.execute(
                    "INSERT OR REPLACE INTO source_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, domain, query, json.dumps(params, sort_keys=True), source,
                     json.dumps(results), now, now + ttl_seconds(domain)),
                )
        finally:
            conn.close()
        return True
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return False


def get(key: str, now: float | None = None) -> dict | None:
    """The stored answer for `key` with `is_stale`, or None (missing, unreadable, or cache off)."""
    now = int(now if now is not None else time.time())
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT domain, query, source, results, retrieved_at, expires_at FROM source_cache WHERE key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    if row is None:
        return None
    domain, query, source, results, retrieved_at, expires_at = row
    try:
        items = json.loads(results)
    except ValueError:
        return None
    return {"domain": domain, "query": query, "source": source, "results": items,
            "retrieved_at": retrieved_at, "expires_at": expires_at, "is_stale": now >= expires_at}
