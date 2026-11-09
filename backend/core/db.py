"""Database: Neon (or any) Postgres when DATABASE_URL is set, otherwise a local SQLite file.

Two tables:
  users  accounts (auth.py): email, name, scrypt password hash
  runs   one row per finished run (api.py): owner, topic, status, timings, and `doc`, a JSON
         document with the run's events, final graph state and usage-meter calls. Everything the
         UI shows for an old run (report, sources, claims, metrics, contradiction map) is rebuilt
         from this row, so the server keeps no state on local disk that matters.

The same SQLAlchemy Core code runs on both databases; tables are created on first use, and columns added
since (runs.server, runs.heartbeat, runs.share_token) are added to an existing table, so older rows and older servers still work.
Neon gives URLs like postgresql://user:pass@host/db?sslmode=require; they are used as-is
(only the scheme is pointed at the psycopg 3 driver). The URL is read from .env, never logged.
"""

import threading

from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, Integer, MetaData, String, Table, Text, create_engine, inspect, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from core import config

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String(254), unique=True, nullable=False),
    Column("name", String(80), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)

runs = Table(
    "runs", metadata,
    Column("id", String(64), primary_key=True),
    Column("owner", String(64), index=True),  # users.id as text; NULL = saved before accounts existed
    Column("topic", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("max_rounds", Integer, nullable=False),
    Column("hitl", Boolean, nullable=False),
    Column("created_at", BigInteger, nullable=False, index=True),
    Column("finished_at", BigInteger),
    Column("doc", JSON().with_variant(JSONB(), "postgresql"), nullable=False),  # events, state, calls
    # Which server process runs it and when that process last confirmed it was alive (epoch ms). Several servers
    # can share one database (Render and a local server on Neon): a starting server marks a run interrupted only
    # when its own server has gone quiet, not merely because it is "running". NULL: saved by an older server.
    Column("server", String(64)),
    Column("heartbeat", BigInteger),
    # Public read-only link to the finished report (POST /api/runs/{id}/share); NULL = not shared.
    # Written only by the share endpoints, never by the run's own saves, so a save cannot undo a share.
    Column("share_token", String(64), unique=True),
)

# Columns added after the first release, as (name, SQL type): created on an existing table by engine().
_ADDED_COLUMNS = {"runs": [("server", "VARCHAR(64)"), ("heartbeat", "BIGINT"), ("share_token", "VARCHAR(64)")]}

_lock = threading.Lock()
_engine: Engine | None = None
_engine_url = ""


def database_url() -> str:
    raw = (config.DATABASE_URL or "").strip()
    if not raw:
        return f"sqlite:///{config.LOCAL_DB}"
    for prefix in ("postgres://", "postgresql://"):  # Neon / Heroku style -> psycopg 3 driver
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix):]
    return raw


def kind() -> str:
    """'postgres' or 'sqlite' (shown on the Security page; never the URL itself)."""
    return "postgres" if database_url().startswith("postgresql") else "sqlite"


def engine() -> Engine:
    """The shared engine, created (with its tables) on first use, or again if the URL changed."""
    global _engine, _engine_url
    url = database_url()
    with _lock:
        if _engine is None or url != _engine_url:
            if _engine is not None:
                _engine.dispose()
            if url.startswith("sqlite:///"):
                from pathlib import Path

                Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
                _engine = create_engine(url, connect_args={"check_same_thread": False})
            else:
                # Neon closes idle connections: check each pooled connection before use.
                _engine = create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5)
            metadata.create_all(_engine)
            _add_missing_columns(_engine)
            _engine_url = url
        return _engine


def _add_missing_columns(eng: Engine) -> None:
    """ALTER TABLE ... ADD COLUMN for columns a table created by an older version lacks (nullable, so safe
    for rows and servers that do not know them)."""
    existing = {t: {c["name"] for c in inspect(eng).get_columns(t)} for t in _ADDED_COLUMNS}
    with eng.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            for name, sql_type in cols:
                if name not in existing[table]:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
