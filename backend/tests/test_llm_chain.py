"""LLM fallback chain and the "section not written" path (offline: _call is faked)."""

import pytest

from core import config, llm
from graph import nodes
from tools.text import NOT_FOUND, WRITE_FAILED


class QuotaSpent(Exception):
    status_code = 429

    def __str__(self):
        return "Rate limit reached for model on tokens per day (TPD): Limit 200000, Used 199000"


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "k")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_CHAIN", [("groq", "big"), ("groq", "small"), ("gemini", "flash")])
    calls = []

    def fake_call(provider, name, system, user, temperature):
        calls.append(name)
        if name == "big":
            raise QuotaSpent()
        return f"answer from {name}", 10, 5

    monkeypatch.setattr(llm, "_call", fake_call)
    return calls


def test_spent_quota_falls_back_to_next_model(chain):
    assert llm.chat("s", "u") == "answer from small"
    assert chain == ["big", "small"]  # a daily quota is not retried, the next model answers


def test_spent_model_is_skipped_while_cooling(chain):
    llm.chat("s", "u")
    chain.clear()
    llm.chat("s", "u")
    assert chain == ["small"]


def test_pinned_model_does_not_fall_back(chain):
    with pytest.raises(QuotaSpent):
        llm.chat("s", "u", provider="groq", model="big")


def test_all_models_failing_raises_last_error(chain, monkeypatch):
    def down(*a):
        raise QuotaSpent()

    monkeypatch.setattr(llm, "_call", down)
    with pytest.raises(QuotaSpent):
        llm.chat("s", "u")


def _task(findings):
    return {"index": 0, "question": "What is A*?", "findings": findings}


SRC = [{"id": "S1", "section": 0, "query": "q", "title": "t", "url": "https://x.org", "snippet": "A* is a search algorithm.",
        "content": "A* is a best-first search algorithm that uses a heuristic.", "extracted_by": "trafilatura"}]


def test_llm_outage_is_not_reported_as_not_found(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("429")

    monkeypatch.setattr(nodes, "llm_chat", down)
    monkeypatch.setattr(nodes, "relevant_passages", lambda text, q, budget, errors: text)
    section = nodes.write_section(_task(SRC))["sections"][0]
    assert section["text"] == WRITE_FAILED and NOT_FOUND not in section["text"].lower()


def test_no_usable_sources_still_says_not_found():
    section = nodes.write_section(_task([]))["sections"][0]
    assert section["text"].lower().startswith(NOT_FOUND)
