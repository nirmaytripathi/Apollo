"""Database layer: Neon URL handling, run persistence, one-time import of older file stores. Offline (SQLite)."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api
from core import auth, config, db


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'apollo-test.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    auth._failures.clear()
    api._runs.clear()
    return tmp_path


@pytest.mark.parametrize("given, expected", [
    ("postgresql://u:p@ep-x.neon.tech/apollo?sslmode=require", "postgresql+psycopg://u:p@ep-x.neon.tech/apollo?sslmode=require"),
    ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
    ("", None),
])
def test_neon_urls_use_the_psycopg_driver(monkeypatch, tmp_path, given, expected):
    monkeypatch.setattr(config, "DATABASE_URL", given)
    monkeypatch.setattr(config, "LOCAL_DB", str(tmp_path / "local.db"))
    assert db.database_url() == (expected or f"sqlite:///{tmp_path / 'local.db'}")
    assert db.kind() == ("postgres" if expected else "sqlite")


def _finished_run(run_id="r1", owner=None, text="clean"):
    run = api.Run("Topic", 1, False, run_id=run_id, owner=owner)
    run.status, run.finished_at = "complete", run.created_at + 1000
    run.events = [{"type": "section_draft", "data": {}, "ts": 1}, {"type": "complete", "data": {}, "ts": 2}]
    run.state = {"report": f"# Topic\n\n## Introduction\n\n{text}", "references": [], "findings": [], "plan": ["q"],
                 "claims": [], "contradictions": [], "errors": [], "round_no": 1, "status": "complete",
                 "stop_reason": "hermes_complete", "sections": [], "topic": "Topic"}
    return run


def test_saved_run_round_trips_without_drafts_or_nul_characters():
    api._save(_finished_run(text="bad\x00byte"))
    api._runs.clear()
    api._load_saved()
    run = api._runs["r1"]
    assert [e["type"] for e in run.events] == ["complete"]
    assert "\x00" not in run.state["report"] and "badbyte" in run.state["report"]


def test_report_download_needs_no_local_files():
    c = TestClient(api.app)
    user = c.post("/api/auth/register", json={"name": "A", "email": "a@example.com", "password": "correct horse"}).json()["user"]
    api._runs["r1"] = _finished_run(owner=user["id"])  # no output/runs/r1 directory exists
    r = c.get("/api/runs/r1/report.md")
    assert r.status_code == 200 and r.text.startswith("# Topic") and "attachment" in r.headers["content-disposition"]


def test_older_file_stores_are_imported_once(isolated):
    # an account from the old users.db, with its password hash
    old = sqlite3.connect(config.LEGACY_USERS_DB)
    old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, name TEXT, password_hash TEXT, created_at INTEGER)")
    old.execute("INSERT INTO users VALUES (7, 'old@example.com', 'Old', ?, 1)", (auth.hash_password("old password 1"),))
    old.commit()
    old.close()
    # a run file owned by that account, and one from before accounts
    for rid, owner in (("owned", "7"), ("legacy", None)):
        d = isolated / "runs" / rid
        d.mkdir(parents=True)
        d.joinpath("run.json").write_text(json.dumps({"id": rid, "owner": owner, "topic": rid, "status": "complete",
                                                       "max_rounds": 1, "hitl": False, "created_at": 1, "events": [],
                                                       "state": None, "calls": []}), encoding="utf-8")
    api._load_saved()
    api._load_saved()  # idempotent: nothing is imported twice
    with db.engine().connect() as conn:
        users = conn.execute(select(db.users)).all()
        runs = {r.id: r.owner for r in conn.execute(select(db.runs.c.id, db.runs.c.owner))}
    assert len(users) == 1 and runs == {"owned": str(users[0].id), "legacy": None}
    assert auth.login("old@example.com", "old password 1")["id"] == str(users[0].id)  # same password still works


def test_claim_script_gives_ownerless_runs_to_one_account():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("claim_runs", Path(__file__).parent.parent / "scripts" / "claim_runs.py")
    claim_runs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(claim_runs)

    owner = auth.register("Me", "me@example.com", "correct horse")
    other = auth.register("Other", "other@example.com", "correct horse")
    api._save(_finished_run("old1"))
    api._save(_finished_run("old2"))
    api._save(_finished_run("theirs", owner=other["id"]))
    assert claim_runs.unclaimed() == 2
    assert claim_runs.claim("ME@example.com") == 2
    assert claim_runs.unclaimed() == 0
    with db.engine().connect() as conn:
        owners = dict(conn.execute(select(db.runs.c.id, db.runs.c.owner)).all())
    assert owners == {"old1": owner["id"], "old2": owner["id"], "theirs": other["id"]}  # others' runs untouched
    with pytest.raises(SystemExit):
        claim_runs.claim("nobody@example.com")


def test_migration_copies_local_data_into_the_target_database(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("migrate_to_db", Path(__file__).parent.parent / "scripts" / "migrate_to_db.py")
    migrate_to_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrate_to_db)

    # the local SQLite database a user has been working in
    source = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", source)
    me = auth.register("Me", "me@example.com", "correct horse")
    api._save(_finished_run("mine", owner=me["id"]))
    api._save(_finished_run("old"))
    # the target (Neon in real use) already has another account, so ids will differ
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'target.db'}")
    auth.register("Someone", "someone@example.com", "correct horse")

    assert migrate_to_db.migrate(source) == (1, 2)
    assert migrate_to_db.migrate(source) == (1, 0)  # safe to run again
    new_id = auth.login("me@example.com", "correct horse")["id"]  # same password works on the target
    with db.engine().connect() as conn:
        owners = dict(conn.execute(select(db.runs.c.id, db.runs.c.owner)).all())
    assert owners == {"mine": new_id, "old": None}


def test_run_cut_off_by_a_restart_is_reported_as_interrupted_once():
    run = api.Run("Topic", 1, False, run_id="cut")  # saved at start, then the server died
    api._save(run)
    for _ in range(2):  # two restarts in a row
        api._runs.clear()
        api._load_saved()
    loaded = api._runs["cut"]
    types = [e["type"] for e in loaded.events]
    assert loaded.status == "failed" and types == ["error", "complete"]  # added once, not per restart
    assert "server restarted" in loaded.events[0]["data"]["message"]
    assert loaded.events[1]["data"] == {"status": "failed", "stop_reason": "interrupted"}


# ---------------------------------------------------------------- several servers on one database


def _row_from_other_server(run_id: str, heartbeat_age_s: float | None, age_s: float = 60):
    """A run another server process saved as running, with its heartbeat `heartbeat_age_s` ago (None: no heartbeat)."""
    import time

    now = int(time.time() * 1000)
    run = api.Run("Topic", 1, False, run_id=run_id)
    row = api._row(run)
    row.update(created_at=now - int(age_s * 1000), server="render:7:abcd",
               heartbeat=None if heartbeat_age_s is None else now - int(heartbeat_age_s * 1000))
    with db.engine().begin() as conn:
        api._upsert(conn, row)


def test_run_alive_on_another_server_is_not_marked_interrupted(monkeypatch):
    monkeypatch.setattr(api, "_ensure_watcher", lambda: None)
    _row_from_other_server("alive", heartbeat_age_s=5)
    api._load_saved()
    assert api._runs["alive"].status == "running" and api._runs["alive"].events == []
    with db.engine().connect() as conn:
        assert conn.execute(select(db.runs.c.status).where(db.runs.c.id == "alive")).scalar() == "running"


@pytest.mark.parametrize("heartbeat_age_s, age_s, cut_off", [
    (api.STALE_SECONDS + 5, 600, True),        # its server went quiet
    (None, api.LEGACY_STALE_SECONDS + 60, True),  # saved by an older server long ago
    (None, 60, False),                         # an older server's run that may still be going
])
def test_run_is_interrupted_only_when_its_server_has_stopped(monkeypatch, heartbeat_age_s, age_s, cut_off):
    monkeypatch.setattr(api, "_ensure_watcher", lambda: None)
    _row_from_other_server("r", heartbeat_age_s, age_s)
    api._load_saved()
    assert (api._runs["r"].status == "failed") is cut_off


def test_watcher_keeps_own_runs_alive_and_catches_up_on_others():
    import time

    mine = api.Run("Mine", 1, False, run_id="mine")
    mine.local = True
    api._save(mine)
    _row_from_other_server("theirs", heartbeat_age_s=5)
    _row_from_other_server("gone", heartbeat_age_s=api.STALE_SECONDS + 5)
    with db.engine().begin() as conn:  # make "mine" look old, so the beat must refresh it
        conn.execute(db.runs.update().where(db.runs.c.id == "mine").values(heartbeat=1))
    api._runs["mine"] = mine
    for run_id in ("theirs", "gone"):
        api._runs[run_id] = api.Run("Topic", 1, False, run_id=run_id)

    api._beat()
    with db.engine().connect() as conn:
        beats = dict(conn.execute(select(db.runs.c.id, db.runs.c.heartbeat)).all())
    assert beats["mine"] > int(time.time() * 1000) - 5000
    assert api._runs["theirs"].status == "running"            # its server is alive: left alone
    assert api._runs["gone"].status == "failed"               # its server stopped: recorded once
    assert api._runs["gone"].events[-1]["data"]["stop_reason"] == "interrupted"

    finished = api.Run("Topic", 1, False, run_id="theirs")    # the other server finishes its run
    finished.status, finished.finished_at = "complete", 1
    with db.engine().begin() as conn:
        row = api._row(finished)
        row["server"] = "render:7:abcd"
        api._upsert(conn, row)
    api._beat()
    assert api._runs["theirs"].status == "complete"


def test_old_runs_table_gains_the_new_columns(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE runs (id VARCHAR(64) PRIMARY KEY, owner VARCHAR(64), topic TEXT NOT NULL, status VARCHAR(32) "
                "NOT NULL, max_rounds INTEGER NOT NULL, hitl BOOLEAN NOT NULL, created_at BIGINT NOT NULL, finished_at BIGINT, "
                "doc JSON NOT NULL)")
    con.execute("INSERT INTO runs VALUES ('old', NULL, 't', 'complete', 1, 0, 1, 2, '{}')")
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{path}")
    with db.engine().connect() as conn:
        row = conn.execute(select(db.runs).where(db.runs.c.id == "old")).one()
    assert row.status == "complete" and row.server is None and row.heartbeat is None
