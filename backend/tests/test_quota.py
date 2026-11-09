"""Running out of LLM quota: claims move to another model, and a run with no model left stops early and says so.

Offline: the LLM calls are fakes raising provider-shaped errors (status 429 / 503), as in test_llm_chain.py.
"""

import time

import pytest

from core import config, llm
from graph import nodes
from graph.workflow import run_research
from verify import claims as claims_mod

from .fakes import fake_embed, verify_reply


class PerMinute(Exception):
    """A per-minute burst still failing after the retries: not a spent daily quota."""
    status_code = 429

    def __str__(self):
        return "Rate limit reached for model on tokens per minute (TPM): Limit 8000"


class PerDay(Exception):
    status_code = 429

    def __str__(self):
        return "Rate limit reached for model on tokens per day (TPD): Limit 200000"


FINDINGS = [{"id": f"S{i}", "content": f"Fact number {i} says the river is {i} kilometres long. Boats sail here."}
            for i in range(1, 4)]
REPORT = "# T\n\n" + " ".join(f"Fact number {i} says the river is {i} kilometres long [S{i}]." for i in range(1, 4))


def _verify(monkeypatch, fail):
    calls = []

    def fake_llm(system, prompt, temperature=0.0, model=None, provider=None):
        calls.append(model)
        if model in fail:
            raise fail[model]()
        return verify_reply(prompt)

    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", fake_llm)
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "busy"), ("gemini", "ok")])
    monkeypatch.setattr(config, "VERIFY_WORKERS", 1)
    errors: list[str] = []
    return claims_mod.verify_report(REPORT, FINDINGS, errors), calls, errors


def test_a_busy_model_moves_the_claim_to_the_next_model_instead_of_unchecked(monkeypatch):
    results, calls, errors = _verify(monkeypatch, {"busy": PerMinute})
    assert [r["status"] for r in results] == ["verified"] * 3
    assert calls == ["busy", "ok"] * 3  # a per-minute limit is not a spent quota: "busy" is tried again next claim
    assert all("trying the next model" in e for e in errors if "LLM call failed" in e)


def test_a_spent_model_is_dropped_for_the_rest_of_the_report(monkeypatch):
    results, calls, errors = _verify(monkeypatch, {"busy": PerDay})
    assert [r["status"] for r in results] == ["verified"] * 3 and calls == ["busy", "ok", "ok", "ok"]


def test_claim_is_unchecked_only_when_every_model_fails(monkeypatch):
    results, calls, errors = _verify(monkeypatch, {"busy": PerMinute, "ok": PerMinute})
    assert [r["status"] for r in results] == ["unchecked"] * 3


# ---------------------------------------------------------------- whole run


@pytest.fixture
def spent_chain(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "k")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_CHAIN", [("groq", "big"), ("gemini", "flash")])

    def spent(provider, name, system, user, temperature):
        raise PerDay()

    monkeypatch.setattr(llm, "_call", spent)


def test_all_exhausted_only_when_every_keyed_model_is_cooling(spent_chain):
    assert not llm.all_exhausted()  # nothing known yet
    with pytest.raises(PerDay):
        llm.chat("s", "u")
    assert llm.all_exhausted() and "in about 10 min" in llm.quota_message()


def test_run_stops_before_searching_when_no_model_has_quota(spent_chain, monkeypatch):
    searched = []
    monkeypatch.setattr(nodes, "web_search", lambda q, errors: searched.append(q) or [])
    with pytest.raises(llm.LLMUnavailable, match="stopped before searching"):
        run_research("drones in farming", max_rounds=1)
    assert searched == []


def test_planning_failure_with_quota_left_still_falls_back_to_the_topic(monkeypatch):
    # A bad answer is not a spent quota: the run continues with the topic as its only question (unchanged).
    monkeypatch.setattr(nodes, "llm_chat", lambda *a, **k: "not json")
    out = nodes.apollo_plan({"topic": "drones in farming", "events": [], "errors": [], "findings": []})
    assert out["plan"] == ["drones in farming"]


def test_api_refuses_a_new_run_while_every_model_is_spent(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import api

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'q.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    c = TestClient(api.app)
    assert c.post("/api/auth/register", json={"name": "T", "email": "q@example.com", "password": "correct horse"}).status_code == 201
    monkeypatch.setattr(llm, "all_exhausted", lambda: True)
    resp = c.post("/api/runs", json={"topic": "drones in farming", "max_rounds": 1})
    assert resp.status_code == 503 and "quota is used up" in resp.json()["detail"]
