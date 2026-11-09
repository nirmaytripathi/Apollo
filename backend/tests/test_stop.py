"""Pause (POST /stop) and restart with an edited topic (POST /restart). Offline: search and LLMs are fakes."""

import json
import threading
import time

import pytest

import api
from core import config, live
from graph import nodes
from tools import passages as passages_mod
from verify import claims as claims_mod

from .fakes import fake_embed, fake_llm, fake_search, verify_reply
from .test_documents import PAGE1, PAGE2, make_pdf

COMPLETE = lambda n: '{"complete": true, "gaps": []}'


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'stop.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "none.db"))
    api._runs.clear()
    api._uploads.clear()
    c = TestClient(api.app)
    assert c.post("/api/auth/register", json={"name": "S", "email": "s@example.com", "password": "correct horse"}).status_code == 201
    return c


def _events(client, run_id):
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ") and line != "data: {}"]


def _wait(client, run_id, pred, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get(f"/api/runs/{run_id}").json()
        if pred(st):
            return st
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting; last status {st}")


def test_pause_mid_run_stops_before_writing(client, monkeypatch):
    """Hermes is held until the test has pressed Pause; the run must then end without writing anything."""
    gate, reviewing, written = threading.Event(), threading.Event(), []

    def review(n):
        reviewing.set()
        gate.wait(5)
        return '{"complete": true, "gaps": []}'

    monkeypatch.setattr(nodes, "llm_chat", fake_llm(review, writer=lambda user: written.append(user) or "x [S1]."))
    run_id = client.post("/api/runs", json={"topic": "walking and heart health", "max_rounds": 1}).json()["run_id"]
    assert reviewing.wait(5)
    assert client.post(f"/api/runs/{run_id}/stop").json() == {"ok": True}
    gate.set()
    st = _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    events = _events(client, run_id)
    assert st["status"] == "rejected" and written == []
    assert events[-1]["type"] == "complete" and events[-1]["data"]["stop_reason"] == "stopped_by_user"
    assert not any(e["type"] in ("section_written", "report_ready") for e in events)
    assert client.post(f"/api/runs/{run_id}/stop").status_code == 409  # already finished


def test_pause_while_waiting_for_approval(client, monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    run_id = client.post("/api/runs", json={"topic": "walking and heart health", "max_rounds": 1, "hitl": True}).json()["run_id"]
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    assert client.post(f"/api/runs/{run_id}/stop").status_code == 200
    st = _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    assert st["status"] == "rejected"
    assert _events(client, run_id)[-1]["data"]["stop_reason"] == "stopped_by_user"


def test_restart_with_an_edited_topic_keeps_settings_and_documents(client, monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE, writer=lambda user: "Walking lowered blood pressure [S1]."))
    up = client.post("/api/documents", files={"file": ("study.pdf", make_pdf([PAGE1, PAGE2]), "application/pdf")}).json()
    run_id = client.post("/api/runs", json={"topic": "walking", "max_rounds": 1, "hitl": True, "documents": [up["id"]]}).json()["run_id"]
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    assert client.post(f"/api/runs/{run_id}/restart", json={"topic": "x y z"}).status_code == 409  # still active
    client.post(f"/api/runs/{run_id}/stop")
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)

    assert client.post(f"/api/runs/{run_id}/restart", json={"topic": "no"}).status_code == 422  # too short
    out = client.post(f"/api/runs/{run_id}/restart", json={"topic": "  walking and blood pressure in older adults  "}).json()
    assert out["documents_kept"] == 1 and out["documents_lost"] is False and out["run_id"] != run_id
    new = api._runs[out["run_id"]]
    assert new.topic == "walking and blood pressure in older adults" and new.max_rounds == 1 and new.hitl is True
    _wait(client, out["run_id"], lambda s: s["status"] == "awaiting_input")  # same approval setting
    client.post(f"/api/runs/{out['run_id']}/resume", json={"action": "approve"})
    _wait(client, out["run_id"], lambda s: s["status"] == "complete")
    report = client.get(f"/api/runs/{out['run_id']}/report").json()
    assert any(s["id"] == "S1" and s["kind"] == "document" for s in report["sources"])


def test_someone_elses_run_cannot_be_stopped_or_restarted(client, monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    run_id = client.post("/api/runs", json={"topic": "walking", "max_rounds": 1, "hitl": True}).json()["run_id"]
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    api._runs[run_id].owner = "someone-else"
    assert client.post(f"/api/runs/{run_id}/stop").status_code == 404
    assert client.post(f"/api/runs/{run_id}/restart", json={"topic": "walking again"}).status_code == 404
    api._runs[run_id].answers.put(api._REJECT)  # let the waiting thread end


def test_stopped_run_refuses_further_model_calls(monkeypatch):
    stop = threading.Event()
    live.register_stop("run-x", stop)
    monkeypatch.setattr(live, "_current_run", lambda: "run-x")
    try:
        live.raise_if_stopped()  # not stopped yet: nothing happens
        stop.set()
        with pytest.raises(live.RunStopped):
            live.raise_if_stopped()
        from core import llm
        with pytest.raises(live.RunStopped):
            llm.chat("system", "user")
    finally:
        live.unregister_stop("run-x")
    monkeypatch.setattr(live, "_current_run", lambda: None)
    live.raise_if_stopped()  # outside a run: never raises


def test_pause_ends_a_rate_limit_wait_at_once(monkeypatch):
    """Seen live: a paused run kept waiting out a provider's retry-after (up to 60 s) before stopping."""
    from tools import retry_policy

    stop = threading.Event()
    live.register_stop("run-y", stop)
    monkeypatch.setattr(live, "_current_run", lambda: "run-y")
    calls = []

    @retry_policy.with_retries(2)
    def flaky():
        calls.append(1)
        if len(calls) == 1:
            threading.Timer(0.3, stop.set).start()  # Pause pressed 0.3 s into the wait
        raise TimeoutError("busy")

    monkeypatch.setattr(retry_policy, "_wait", lambda state: 30)  # as if the provider asked for 30 s
    started = time.monotonic()
    try:
        with pytest.raises(live.RunStopped):
            flaky()
    finally:
        live.unregister_stop("run-y")
    assert time.monotonic() - started < 2 and len(calls) == 1  # no 30 s wait, no second attempt


def test_a_paused_chat_does_not_move_on_to_the_next_model(monkeypatch):
    from core import llm

    tried = []

    def one(p, m, *a, **k):
        tried.append(m)
        raise live.RunStopped()

    monkeypatch.setattr(llm, "_chat_one", one)
    monkeypatch.setattr(config, "GROQ_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_CHAIN", [("groq", "a"), ("groq", "b")])
    with pytest.raises(live.RunStopped):
        llm.chat("s", "u")
    assert tried == ["a"]


# ---------------------------------------------------------------- Pause / Resume (same run, nothing restarts)


def _paused_after_review(client, monkeypatch, writer=None):
    """Start a run, press Pause while Hermes reviews, and wait until it is paused after that step."""
    gate, reviewing = threading.Event(), threading.Event()

    def review(n):
        reviewing.set()
        gate.wait(5)
        return '{"complete": true, "gaps": []}'

    monkeypatch.setattr(nodes, "llm_chat", fake_llm(review, writer=writer or (lambda user: "A finding [S1].")))
    run_id = client.post("/api/runs", json={"topic": "walking and heart health", "max_rounds": 1}).json()["run_id"]
    assert reviewing.wait(5)
    assert client.post(f"/api/runs/{run_id}/pause").json() == {"ok": True}
    gate.set()  # the step in progress is never cut short: Hermes finishes, then the run waits
    _wait(client, run_id, lambda s: s["status"] == "paused")
    return run_id


def test_pause_then_resume_continues_the_same_run(client, monkeypatch):
    written = []
    run_id = _paused_after_review(client, monkeypatch, writer=lambda user: written.append(user) or "A finding [S1].")
    time.sleep(0.3)
    assert written == []  # nothing runs while paused
    assert client.post(f"/api/runs/{run_id}/continue").json() == {"ok": True}
    st = _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    events = _events(client, run_id)
    types = [e["type"] for e in events]
    assert st["status"] == "complete" and written  # it went on to write the report
    assert types.count("plan") == 1 and types.count("search_started") == len(set(e["data"]["query"] for e in events if e["type"] == "search_started"))
    assert types.index("paused") < types.index("resumed") < types.index("section_written")  # resumed, not restarted
    assert api._runs[run_id].topic == "walking and heart health" and len(api._runs) == 1


def test_paused_time_does_not_count_against_the_research_time_limit(client, monkeypatch):
    run_id = _paused_after_review(client, monkeypatch)
    time.sleep(1.2)
    client.post(f"/api/runs/{run_id}/continue")
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    resumed = next(e for e in _events(client, run_id) if e["type"] == "resumed")
    assert resumed["data"]["paused_seconds"] >= 1
    live.register_stop(run_id, threading.Event())  # the run has ended; check the clock the nodes would read
    monkeypatch.setattr(live, "_current_run", lambda: run_id)
    live.add_paused(run_id, 100)
    state = {"started_at": time.time() - 150, "timeout_seconds": 180}
    assert round(nodes._elapsed(state)) == 50 and not nodes._timed_out(state)
    live.unregister_stop(run_id)


def test_a_paused_run_can_be_stopped_and_restarted(client, monkeypatch):
    run_id = _paused_after_review(client, monkeypatch)
    assert client.post(f"/api/runs/{run_id}/restart", json={"topic": "walking and sleep"}).status_code == 409
    client.post(f"/api/runs/{run_id}/stop")
    st = _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    assert st["status"] == "rejected" and _events(client, run_id)[-1]["data"]["stop_reason"] == "stopped_by_user"
    assert client.post(f"/api/runs/{run_id}/continue").status_code == 409  # finished: nothing to resume


def test_pause_is_refused_while_waiting_for_approval(client, monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    run_id = client.post("/api/runs", json={"topic": "walking", "max_rounds": 1, "hitl": True}).json()["run_id"]
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    assert client.post(f"/api/runs/{run_id}/pause").status_code == 409
    client.post(f"/api/runs/{run_id}/stop")
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)


def test_a_run_left_paused_too_long_ends_as_stopped(client, monkeypatch):
    monkeypatch.setattr(api, "PAUSE_MAX_SECONDS", 0.5)
    run_id = _paused_after_review(client, monkeypatch)
    st = _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    assert st["status"] == "rejected" and _events(client, run_id)[-1]["data"]["stop_reason"] == "stopped_by_user"
