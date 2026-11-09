"""Phase 1 tests: loop control and failure handling, with no network or API keys.

The LLM, web search, and page extraction are replaced by fakes via monkeypatch
on graph.nodes, so these tests check OUR control logic, not external services.
"""

import json
import random
import time

import pytest

from core import config
from graph import nodes
from graph.state import initial_state
from graph.workflow import run_research
from tools import extract as extract_mod
from tools import passages as passages_mod
from tools import search as search_mod
from tools.jsonparse import parse_json
from tools.retry_policy import ConfigError, with_retries

from .fakes import PLAN, fake_embed, fake_llm, fake_search

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Page text names its page (and so the query), as a real page is about what was searched for.
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"page text from {url}", "trafilatura"))
    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(nodes, "verify_report", lambda report, findings, errors: [])  # covered in test_phase2


def test_loop_stops_at_max_rounds(monkeypatch):
    # Hermes never says complete and always asks for a new gap query.
    reply = lambda n: json.dumps({"complete": False, "gaps": [f"gap query {n}"], "contradictions": []})
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(reply))

    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert final["round_no"] == 2
    assert final["stop_reason"] == "max_rounds_reached"
    assert final["status"] == "complete"
    assert "gap query 1" in final["searched_queries"]  # round-2 query was actually searched
    assert any(f["query"] == "gap query 1" for f in final["findings"])


def test_invalid_hermes_json_does_not_crash(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: "Sure! Here is my review: looks good"))

    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert final["status"] == "complete"
    assert final["stop_reason"] == "no_follow_up_queries"
    assert any("invalid JSON" in e for e in final["errors"])


def test_empty_search_does_not_crash(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": true, "gaps": []}'))
    monkeypatch.setattr(nodes, "web_search", lambda q, errors: [])

    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert final["status"] == "failed"  # nothing found, but the run ended cleanly
    assert final["findings"] == []
    # Below the coverage target, every sub-question gets one more search ("... research study") before stopping.
    assert sum("No results" in e for e in final["events"]) == 2 * len(PLAN)
    assert final["round_no"] == 2 and all(q.endswith("research study") for q in final["searched_queries"][len(PLAN):])


def test_empty_result_for_one_subquestion_continues(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": true, "gaps": []}'))
    monkeypatch.setattr(nodes, "web_search", fake_search(empty_for={"q two", "q two research study"}))

    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    queries_with_findings = {f["query"] for f in final["findings"]}
    assert queries_with_findings == {"q one", "q three"}
    assert [f["id"] for f in final["findings"]] == [f"S{i}" for i in range(1, 7)]
    assert any("q two" in e for e in final["errors"])


def test_failed_search_is_logged_not_raised(monkeypatch):
    def boom(query):
        raise ConnectionError("simulated outage")

    monkeypatch.setattr(config, "TAVILY_API_KEY", "x")
    monkeypatch.setattr(config, "SERPER_API_KEY", "y")
    monkeypatch.setattr(search_mod, "_tavily", boom)
    monkeypatch.setattr(search_mod, "_serper", boom)

    errors = []
    assert search_mod.search("anything", errors) == []
    assert len(errors) == 2 and all("simulated outage" in e for e in errors)


def test_retry_limit_transient_vs_permanent():
    calls = {"transient": 0, "permanent": 0}

    @with_retries(2)
    def flaky():
        calls["transient"] += 1
        raise ConnectionError("temporary")

    @with_retries(2)
    def misconfigured():
        calls["permanent"] += 1
        raise ConfigError("key missing")

    with pytest.raises(ConnectionError):
        flaky()
    with pytest.raises(ConfigError):
        misconfigured()
    assert calls == {"transient": 3, "permanent": 1}  # 1 try + 2 retries vs. no retry


def test_wall_clock_timeout_terminates(monkeypatch):
    reply = lambda n: json.dumps({"complete": False, "gaps": [f"gap {n}"]})
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(reply))

    def slow_search(q, errors):
        time.sleep(1.2)
        return fake_search()(q, errors)

    monkeypatch.setattr(nodes, "web_search", slow_search)

    started = time.time()
    final = run_research("topic", max_rounds=5, timeout_seconds=1)

    assert final["stop_reason"] == "timeout"
    assert final["round_no"] == 1
    assert len(final["searched_queries"]) == 3
    assert time.time() - started < 3  # the 3 searches ran at once (one after another: 3.6 s)


def test_search_skipped_after_timeout(monkeypatch):
    monkeypatch.setattr(nodes, "web_search", lambda q, errors: pytest.fail("searched after the deadline"))
    state = initial_state("topic", max_rounds=2, timeout_seconds=1, started_at=time.time() - 5)
    state["pending_queries"] = [{"section": 0, "query": "q one"}]

    out = nodes.athena_search(state)

    assert out["searched_queries"] == []
    assert any("timeout" in e for e in out["errors"])


def test_plan_falls_back_to_topic_on_bad_json(monkeypatch):
    calls = {"n": 0}

    def chat(system, user, temperature=0.2, model=None):
        calls["n"] += 1
        return "not json" if system == nodes.PLAN_SYSTEM else '{"complete": true, "gaps": []}'

    monkeypatch.setattr(nodes, "llm_chat", chat)
    final = run_research("my topic", max_rounds=2, timeout_seconds=60)

    assert final["plan"] == ["my topic"]
    assert sum("invalid plan JSON" in e for e in final["errors"]) == 2  # first try + one retry


def test_hermes_cannot_invent_sources(monkeypatch):
    reply = lambda n: json.dumps(
        {
            "complete": True,
            "gaps": [],
            "contradictions": [
                {"source_a": "S1", "source_b": "S2", "claim": "real ids"},
                {"source_a": "S1", "source_b": "S99", "claim": "invented id"},
            ],
        }
    )
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(reply))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert [c["claim"] for c in final["contradictions"]] == ["real ids"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here you go: {"a": 1} hope it helps', {"a": 1}),
        ("no json here", None),
        ("[1, 2]", None),
        ("", None),
    ],
)
def test_parse_json(text, expected):
    assert parse_json(text) == expected


# ---------------------------------------------------------------- extraction


def test_extraction_falls_back_to_search_provider_text(monkeypatch):
    def own_fails(url, errors):
        errors.append("403 Forbidden")
        return ""

    monkeypatch.setattr(extract_mod, "extract_text", own_fails)

    errors = []
    text, method = extract_mod.page_content("https://x", "word " * 100, errors)
    assert method == "tavily_raw" and text.startswith("word word")
    assert errors == []  # recovered, so the own-fetch failure is not reported

    text, method = extract_mod.page_content("https://x", "too short", errors)
    assert (text, method) == ("", "")
    assert any("403" in e for e in errors) and any("no page text" in e for e in errors)


class _FakeResponse:
    def __init__(self, ctype):
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        raise AssertionError("body must not be downloaded for non-HTML content")


def test_non_html_is_skipped_without_downloading(monkeypatch):
    monkeypatch.setattr(extract_mod.requests, "get", lambda *a, **k: _FakeResponse("application/pdf"))
    errors = []
    assert extract_mod.extract_text("https://x/report.pdf", errors) == ""
    assert "non-HTML (application/pdf)" in errors[0]


def test_parallel_extraction_keeps_ids_stable(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": true, "gaps": []}'))

    def slow_random(url, raw, errors):
        time.sleep(random.uniform(0, 0.05))  # finish in random order
        return f"text of {url}", "trafilatura"

    monkeypatch.setattr(nodes, "page_content", slow_random)
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert [f["id"] for f in final["findings"]] == [f"S{i}" for i in range(1, 10)]
    assert all(f["content"] == f"text of {f['url']}" for f in final["findings"])


def test_fallback_text_keeps_line_breaks(monkeypatch):
    monkeypatch.setattr(extract_mod, "extract_text", lambda url, errors: "")
    raw = "Heading line:\n\n   First paragraph   with  spaces. " + "x " * 120 + "\nSecond paragraph."
    text, method = extract_mod.page_content("https://x", raw, [])
    assert method == "tavily_raw" and text.startswith("Heading line:\nFirst paragraph with spaces.")


def test_provider_text_drops_menus_and_can_go_first(monkeypatch):
    calls = []
    monkeypatch.setattr(extract_mod, "extract_text", lambda url, errors: calls.append(url) or "own text " * 50)
    body = "A real sentence about the topic that is long enough to keep. " * 5
    raw = "Home\nDonate\nHome\nWhat is it?\n" + body + "\nDonate"

    assert extract_mod.page_content("https://x", raw, [])[1] == "trafilatura"  # default: own extraction first
    monkeypatch.setattr(config, "EXTRACT_ORDER", "provider_first")
    text, method = extract_mod.page_content("https://y", raw, [])

    assert method == "tavily_raw" and calls == ["https://x"]  # no download when the provider's text suffices
    assert text == "What is it?\n" + body.strip()
