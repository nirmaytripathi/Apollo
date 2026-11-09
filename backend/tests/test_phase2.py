"""Phase 2 tests: citations, References, HITL pause/resume, verification, export.

All offline: the LLM, search, extraction and the embedding model are fakes, so
these tests check our code's rules, not the model's behaviour.
"""

import json
import re
import subprocess
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from core import config
from reports.export import write_report
from graph import nodes
from graph.workflow import build_graph, run_research, stream_research
from langgraph.types import Command
from tools.text import find_citations, normalize_citations, split_sentences
from tools import passages as passages_mod
from verify import claims as claims_mod
from reports.contradictions import NO_CONFLICTS, write_contradiction_map

REAL_AVAILABLE_CHAIN = claims_mod._available_chain  # captured before any fixture patches it

from .fakes import PLAN, fake_embed, fake_llm, fake_search, verify_reply

ROOT = Path(__file__).resolve().parents[1]
CHAIN = [("groq", "small-model"), ("groq", "big-model"), ("gemini", "gemini-model")]
COMPLETE = lambda n: '{"complete": true, "gaps": []}'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    # verify/claims.py has its own llm_chat reference; without this it would call the real API.
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    # conftest blanks the API keys, which would empty the chain; use a fixed fake chain instead.
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: list(CHAIN))


# ---------------------------------------------------------------- citations


def test_report_citations_are_valid(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    finding_ids = {f["id"] for f in final["findings"]}
    cited = set(find_citations(final["report"]))
    assert cited and cited <= finding_ids
    assert "[S99]" not in final["report"]  # invented id stripped
    assert "uncited opinion" not in final["report"]  # uncited sentence dropped
    assert any("S99" in e for e in final["errors"]) and any("uncited sentence" in e for e in final["errors"])
    for p in PLAN:
        assert f"## {PLAN.index(p) + 1}. {p}" in final["report"]


def test_uncited_ids_excluded_from_references(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    ref_ids = [r["id"] for r in final["references"]]
    assert set(ref_ids) == set(find_citations(final["report"]))
    assert len(final["findings"]) == 9 and len(ref_ids) == 3  # writer cites one source per section
    by_id = {f["id"]: f for f in final["findings"]}
    assert all(r["url"] == by_id[r["id"]]["url"] for r in final["references"])


def test_writer_sees_only_its_sections_findings(monkeypatch):
    seen = {}

    def writer(user):
        question = re.search(r"Sub-question: (.*)", user).group(1)
        seen[question] = set(re.findall(r"\[(S\d+)\]", user))
        return f"Claim [{sorted(seen[question])[0]}]."

    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE, writer=writer))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    for i, q in enumerate(PLAN):
        assert seen[q] == {f["id"] for f in final["findings"] if f["section"] == i}


def test_section_without_sources_says_not_found(monkeypatch):
    monkeypatch.setattr(nodes, "web_search", fake_search(empty_for={"q two", "q two research study"}))
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    section2 = final["report"].split("## 2. q two")[1].split("##")[0].strip()
    assert section2 == "Not found in sources."


def test_duplicate_sentences_removed(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE, writer=lambda user: "Same sentence everywhere [S1]."))
    monkeypatch.setattr(nodes, "write_section", _same_ids_writer(nodes.write_section))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert final["report"].count("Same sentence everywhere") == 1
    assert any("duplicate" in e for e in final["errors"])


def _same_ids_writer(real):
    """Give every section the same source S1 so the writer can repeat one cited sentence."""

    def wrapped(task):
        task = dict(task, findings=[dict(task["findings"][0], id="S1")])
        return real(task)

    return wrapped


def test_normalize_citations():
    assert normalize_citations("A [S1, S3] and B [S2; S4].") == "A [S1][S3] and B [S2][S4]."
    # A short range names each source; a long one is more likely a mistake and is still removed.
    assert normalize_citations("Range [S1-S3] here [S5].") == "Range [S1][S2][S3] here [S5]."
    assert normalize_citations("Range [S1-S9] here [S5].") == "Range  here [S5]."
    assert split_sentences("Yield rose 20%. [S2] Costs fell [S3].") == ["Yield rose 20%. [S2]", "Costs fell [S3]."]


@pytest.mark.parametrize("raw,canonical", [
    ("Fact 【S6】.", "Fact [S6]."),                 # gpt-oss writes these
    ("Fact 【6†L1-L4】.", "Fact [S6]."),
    ("Fact 【S1, S6】.", "Fact [S1][S6]."),
    ("Fact [S6†L1-L4].", "Fact [S6]."),
    ("Fact (S6).", "Fact [S6]."),
    ("Fact (S1, S6).", "Fact [S1][S6]."),
    ("Fact 【note】 stays.", "Fact 【note】 stays."),   # no id inside: untouched
    ("Fact (see 2019).", "Fact (see 2019)."),        # ordinary parentheses: untouched
])
def test_other_citation_shapes_become_canonical(raw, canonical):
    assert normalize_citations(raw) == canonical


def test_correctly_cited_sentences_in_other_shapes_are_kept_but_unknown_ids_are_not():
    errors: list[str] = []
    kept = nodes.enforce_citations("Fact one 【S6】. Fact two (S2). Fact three 【S9】.", {"S2", "S6"}, errors, "w")
    assert kept[:2] == ["Fact one [S6].", "Fact two [S2]."]
    assert len(kept) == 2 and any("S9" in e for e in errors)  # an id the writer was not given is still removed


# ---------------------------------------------------------------- HITL


def test_hitl_pauses_after_plan_and_resumes_with_edited_plan(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    graph = build_graph()
    cfg = {"configurable": {"thread_id": "t1"}}
    from graph.state import initial_state

    start = initial_state("topic", max_rounds=2, timeout_seconds=60, started_at=time.time(), auto_approve=False)
    list(graph.stream(start, cfg))

    snap = graph.get_state(cfg)
    assert snap.next == ("review_plan",)  # paused before any searching
    assert snap.interrupts[0].value == {"type": "approve_plan", "topic": "topic", "plan": PLAN}
    assert snap.values["findings"] == []

    edited = ["new question a", "new question b"]
    list(graph.stream(Command(resume={"plan": edited}), cfg))
    final = graph.get_state(cfg).values
    assert graph.get_state(cfg).next == ()  # ran to END
    assert final["plan"] == edited
    assert final["searched_queries"] == edited
    assert final["status"] == "complete" and final["report"].startswith("# topic")


def test_hitl_gap_review_can_skip(monkeypatch):
    reply = lambda n: json.dumps({"complete": False, "gaps": [{"section": 1, "query": "gap one"}]})
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(reply))
    payloads = []

    def handler(payload):
        payloads.append(payload)
        return {"approved": True} if payload["type"] == "approve_plan" else {"skip": True}

    final = run_research("topic", max_rounds=2, timeout_seconds=60, auto_approve=False, on_interrupt=handler)
    assert [p["type"] for p in payloads] == ["approve_plan", "approve_gaps"]
    assert payloads[1]["gaps"] == ["gap one"]
    assert final["stop_reason"] == "gaps_skipped_by_user" and final["round_no"] == 1


def test_hitl_gap_review_approved_runs_round_2_in_right_section(monkeypatch):
    reply = lambda n: json.dumps({"complete": False, "gaps": [{"section": 2, "query": "gap for two"}]})
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(reply))
    final = run_research("topic", max_rounds=2, timeout_seconds=60, auto_approve=False, on_interrupt=lambda p: True)

    assert final["round_no"] == 2 and "gap for two" in final["searched_queries"]
    assert {f["section"] for f in final["findings"] if f["query"] == "gap for two"} == {1}


def test_auto_approve_never_pauses(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": false, "gaps": ["g"]}'))

    def must_not_be_called(payload):
        raise AssertionError("paused although auto_approve=True")

    final = run_research("topic", max_rounds=2, timeout_seconds=60, auto_approve=True, on_interrupt=must_not_be_called)
    assert final["round_no"] == 2


def test_human_pause_does_not_count_against_timeout(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))

    def slow_human(payload):
        time.sleep(1.5)  # longer than the 1s research timeout
        return True

    final = run_research("topic", max_rounds=2, timeout_seconds=1, auto_approve=False, on_interrupt=slow_human)
    assert final["searched_queries"] == PLAN
    assert not any("timeout" in e for e in final["errors"])


# ---------------------------------------------------------------- verification


def test_verification_statuses(monkeypatch):
    findings = [
        {"id": "S1", "content": "Drone spraying cut pesticide use by 30 percent in Punjab trials.", "snippet": ""},
        {"id": "S2", "content": "Drone spraying in Punjab trials was studied for pesticide use outcomes.", "snippet": ""},
        {"id": "S3", "content": "The weather in Kerala was rainy this year overall.", "snippet": ""},
    ]
    report = (
        "# T\n\n## 1. Q\n\n"
        "Drone spraying cut pesticide use by 30 percent in Punjab trials [S1]. "
        "Drone spraying cut pesticide use in Punjab trials [S2]. "
        "Satellite credit scoring raised farm loans [S3]. "
        "Not found in sources."
    )

    def chat(system, user, temperature=0.0, model=None, provider=None):
        if "[S1.1]" in user:  # S1 states the whole claim
            return verify_reply(user)
        if "[S2.1]" in user:  # S2 backs the drone part but not the number
            return json.dumps({"parts": [
                {"text": "Drone spraying in Punjab trials", "verdict": "supported", "passage": "S2.1", "quote": "Drone spraying in Punjab trials"},
                {"text": "cut pesticide use", "verdict": "unsupported", "passage": "", "quote": ""}]})
        return verify_reply(user, "unsupported")

    monkeypatch.setattr(claims_mod, "llm_chat", chat)
    results = claims_mod.verify_report(report, findings, [])

    assert [r["status"] for r in results] == ["verified", "partial", "not_found"]
    assert results[0]["best_match"].startswith("Drone spraying cut pesticide")
    assert results[0]["evidence"] == "Drone spraying cut pesticide use"
    assert [p["verdict"] for p in results[1]["parts"]] == ["supported", "unsupported"]  # the missing part is named
    assert results[0]["passages"][0]["label"] == "S1.1" and results[0]["passages"][0]["source_id"] == "S1"
    assert all(r["source_ids"] for r in results)  # the uncited "Not found" sentence is not a claim


def test_invented_quotes_and_contradictions(monkeypatch):
    findings = [{"id": "S1", "content": "Intro line.\nResults:\nThe trial found no change in yield. Costs fell across all farms.", "snippet": ""},
                {"id": "S2", "content": "Yields rose by 12 percent in the second season of the trial.", "snippet": ""}]
    report = "# T\n\nThe trial raised yields by 12 percent [S1][S2]. The trial raised yields by 40 percent [S2]."

    def chat(system, user, temperature=0.0, model=None, provider=None):
        if "40 percent" in user:  # quotes words that are not in the source: must not count
            return json.dumps({"parts": [{"text": "yields rose 40 percent", "verdict": "supported", "passage": "S2.1",
                                          "quote": "Yields rose by 40 percent"}]})
        return json.dumps({"parts": [{"text": "yields rose 12 percent", "verdict": "supported", "passage": "S2.1",
                                      "quote": "Yields rose by 12 percent ... second season"},
                                     {"text": "in the trial", "verdict": "contradicted", "passage": "S1.1",
                                      "quote": "the trial found NO change in yield"}]})

    monkeypatch.setattr(claims_mod, "llm_chat", chat)
    results = claims_mod.verify_report(report, findings, [])

    assert [r["status"] for r in results] == ["contradicted", "not_found"]
    first, second = results
    assert {p["source_id"] for p in first["passages"]} == {"S1", "S2"}  # one call saw both cited sources
    assert first["parts"][1]["source_id"] == "S1" and first["parts"][1]["verdict"] == "contradicted"
    s1 = next(p for p in first["passages"] if p["source_id"] == "S1" and p["text"].startswith("The trial found"))
    assert s1["heading"] == "Results:" and s1["after"] == "Costs fell across all farms." and 0 < s1["position"] < 1
    assert second["parts"][0]["verdict"] == "unsupported" and "not in S2" in second["parts"][0]["note"]


def test_each_clause_of_a_long_claim_gets_its_own_passage():
    sentences = ["Peptides carry risks for many users today.", "Injection site reactions are common.",
                 "Serious concerns include theoretical effects on cell growth.", "Weather was rainy."]
    claim = "Peptides carry risks, such as injection site reactions, and theoretical effects on cell growth"
    assert claims_mod.clause_matches(sentences, claim) == sentences[:3]


def test_unusable_verifier_answer_leaves_the_claim_unchecked(monkeypatch):
    findings = [{"id": "S1", "content": "Drone spraying cut pesticide use by 30 percent in Punjab trials.", "snippet": ""}]
    report = "# T\n\nDrone spraying cut pesticide use [S1]."
    monkeypatch.setattr(claims_mod, "llm_chat", lambda *a, **k: "I think it is supported.")
    errors = []
    assert claims_mod.verify_report(report, findings, errors)[0]["status"] == "unchecked"
    assert any("invalid JSON" in e for e in errors)


# ---------------------------------------------------------------- outputs


def test_contradiction_map_with_and_without_conflicts(tmp_path):
    findings = [{"id": "S1", "title": "A", "url": "https://a"}, {"id": "S2", "title": "B", "url": "https://b"}]
    assert write_contradiction_map([], findings, tmp_path / "none.html") == NO_CONFLICTS
    assert "No conflicts found" in (tmp_path / "none.html").read_text(encoding="utf-8")

    msg = write_contradiction_map([{"source_a": "S1", "source_b": "S2", "claim": "yield up vs down"}], findings, tmp_path / "c.html")
    html = (tmp_path / "c.html").read_text(encoding="utf-8")
    assert msg.startswith("1 conflict") and "yield up vs down" in html and "vis-network" in html.lower()


def test_export_passes_citation_check_script(monkeypatch, tmp_path):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)
    path = write_report(final, tmp_path)

    text = path.read_text(encoding="utf-8")
    assert "## References" in text and "## Claim verification summary" in text
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_citations.py"), str(tmp_path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    assert "Failures: 0" in proc.stdout


def test_check_script_catches_bad_citation(monkeypatch, tmp_path):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)
    final = dict(final, report=final["report"] + "\n\nSneaky claim [S42].")
    write_report(final, tmp_path)

    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_citations.py"), str(tmp_path)], capture_output=True, text=True)
    assert proc.returncode == 1 and "S42: cited but not in findings" in proc.stdout


def test_not_found_variants_are_kept_and_normalised(monkeypatch):
    writer = lambda user: "Yields data was not found in the provided sources. Penetration rates were not found in the sources."
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE, writer=writer))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    section1 = final["report"].split("## 1. q one")[1].split("##")[0]
    assert section1.count("not found in sources") == 2
    assert not any("dropped uncited" in e for e in final["errors"])


def test_writer_retries_once_when_citations_are_missing(monkeypatch):
    prompts = []

    def writer(user):
        prompts.append(user)
        first = re.search(r"\[(S\d+)\]", user).group(1)
        q = re.search(r"Sub-question: (.*)", user).group(1)  # keep sections distinct (dedupe is global)
        if "had sentences without a valid citation" in user:
            return f"Cited fact one on {q} [{first}]. Cited fact two on {q} [{first}]."
        return f"Uncited fact one. Cited fact one on {q} [{first}]."

    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE, writer=writer))
    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert len(prompts) == 2 * len(PLAN)  # exactly one retry per section, never more
    assert "- Uncited fact one." in prompts[1]  # feedback lists the dropped sentence
    assert final["report"].count("Cited fact two") == len(PLAN)


@pytest.mark.parametrize(
    "sentence,kept_as",
    [
        ("The sources do not specify which algorithms work best.", "The sources do not specify which algorithms work best: not found in sources."),
        ("No information on effect sizes was found.", "No information on effect sizes was found: not found in sources."),
        ("however, the provided sources do not report costs.", "However, the provided sources do not report costs: not found in sources."),
        ("AI apps do not raise yields.", None),  # uncited factual negative: must be dropped, not excused
    ],
)
def test_absence_sentences_carry_not_found_phrase(sentence, kept_as):
    errors = []
    kept = nodes.enforce_citations(sentence, {"S1"}, errors, "test")
    assert kept == ([kept_as] if kept_as else [])


# ---------------------------------------------------------------- passage selection

BOILERPLATE = "Cookie settings and privacy notice for this website apply to all visitors. " * 30
RELEVANT = "Drone spraying reduced pesticide use by thirty percent in Punjab rice trials."


def test_relevant_passages_prefer_relevant_text_over_page_start():
    text = BOILERPLATE + "\n" + RELEVANT + "\nMore boilerplate about newsletter subscriptions and site navigation menus."
    out = passages_mod.relevant_passages(text, "How did drone spraying change pesticide use in Punjab?", 200)
    assert RELEVANT in out and len(out) <= 200


def test_relevant_passages_keep_source_order_and_short_text_untouched():
    text = "Alpha fact about drones and pesticide.\nZebra unrelated sentence about the weather.\nOmega fact about drones and pesticide use."
    out = passages_mod.relevant_passages(text, "drones pesticide", 90)
    assert "Zebra" not in out and out.index("Alpha") < out.index("Omega")
    assert passages_mod.relevant_passages("short text", "anything", 100) == "short text"


def test_relevant_passages_fall_back_when_embedding_fails(monkeypatch):
    def broken(texts):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(passages_mod, "embed", broken)
    errors = []
    assert passages_mod.relevant_passages("x" * 50 + ". " + "y" * 500, "q", 40, errors) == ("x" * 50)[:40]
    assert "embedding failed" in errors[0]


def test_writer_and_hermes_see_relevant_passage_beyond_page_start(monkeypatch):
    long_page = BOILERPLATE + "\n" + RELEVANT
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (long_page, "trafilatura"))
    prompts = {"write": [], "review": []}

    def writer(user):
        prompts["write"].append(user)
        question = re.search(r"Sub-question: (.*)", user).group(1)
        first = re.search(r"\[(S\d+)\]", user).group(1)
        return f"Finding about {question} [{first}]."

    plan = ["How did drone spraying change pesticide use?", "Pesticide use in Punjab rice trials", "Drone spraying results in Punjab"]
    chat = fake_llm(COMPLETE, writer=writer, plan=plan)

    def spy(system, user, temperature=0.2, model=None):
        if system == nodes.REVIEW_SYSTEM:
            prompts["review"].append(user)
        return chat(system, user, temperature, model)

    monkeypatch.setattr(nodes, "llm_chat", spy)
    run_research("drone spraying pesticide use Punjab", max_rounds=2, timeout_seconds=60)

    assert RELEVANT not in long_page[:1200]  # the old "first N chars" approach would have missed it
    assert all(RELEVANT in p for p in prompts["write"])
    assert RELEVANT in prompts["review"][0]


# ---------------------------------------------------------------- quota handling


class _QuotaError(Exception):
    """Looks like Groq's 429 for a spent daily quota: retry-after far beyond the cap."""

    def __init__(self):
        super().__init__("Rate limit reached ... tokens per day (TPD)")
        self.response = type("R", (), {"headers": {"retry-after": "212"}})()


def test_daily_quota_error_is_not_retried():
    from tools.retry_policy import is_quota_exhausted, is_transient, with_retries

    assert is_quota_exhausted(_QuotaError()) and not is_transient(_QuotaError())
    calls = {"n": 0}

    @with_retries(2)
    def spent():
        calls["n"] += 1
        raise _QuotaError()

    with pytest.raises(_QuotaError):
        spent()
    assert calls["n"] == 1  # no waiting out a multi-minute retry-after


def test_verifier_walks_the_fallback_chain_then_marks_unchecked(monkeypatch):
    findings = [{"id": "S1", "content": "Drone spraying cut pesticide use by 30 percent in Punjab trials.", "snippet": ""}]
    report = "# T\n\n## 1. Q\n\nDrone spraying cut pesticide use in Punjab [S1]. Drone spraying cut pesticide use by 30 percent [S1]."
    used = []

    def both_groq_spent(system, user, temperature=0.0, model=None, provider=None):
        used.append((provider, model))
        if provider == "groq":
            raise _QuotaError()
        return verify_reply(user)

    monkeypatch.setattr(claims_mod, "llm_chat", both_groq_spent)
    errors = []
    results = claims_mod.verify_report(report, findings, errors)
    assert [r["status"] for r in results] == ["verified", "verified"]
    # each spent model is tried once, then never again; Gemini answers both claims
    assert used == [CHAIN[0], CHAIN[1], CHAIN[2], CHAIN[2]]
    assert sum("switching to" in e for e in errors) == 2

    def all_spent(system, user, temperature=0.0, model=None, provider=None):
        used.append((provider, model))
        raise _QuotaError()

    monkeypatch.setattr(claims_mod, "llm_chat", all_spent)
    used.clear()
    results = claims_mod.verify_report(report, findings, [])
    assert [r["status"] for r in results] == ["unchecked", "unchecked"]
    assert len(used) == 3  # one try per model, then no more LLM calls at all


def test_chain_skips_providers_without_keys(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_CHAIN", [("groq", "a"), ("gemini", "b"), ("groq", "a")])
    monkeypatch.setattr(config, "GEMINI_API_KEY", "set")  # GROQ key stays blank (conftest)
    assert REAL_AVAILABLE_CHAIN() == [("gemini", "b")]


class _GeminiError(Exception):
    """Shaped like google.genai.errors.ClientError: status in `.code`, details in the message."""

    def __init__(self, code, message):
        super().__init__(f"{code} {message}")
        self.code = code


def test_gemini_error_classification():
    from tools.retry_policy import _raw_retry_after, is_quota_exhausted, is_transient

    per_minute = _GeminiError(429, "RESOURCE_EXHAUSTED. {'details': [{'retryDelay': '37s'}], 'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}")
    per_day = _GeminiError(429, "RESOURCE_EXHAUSTED. {'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}")
    not_found = _GeminiError(404, "NOT_FOUND. This model is no longer available")
    server = _GeminiError(504, "DEADLINE_EXCEEDED")

    assert is_transient(per_minute) and not is_quota_exhausted(per_minute) and _raw_retry_after(per_minute) == 37.0
    assert is_quota_exhausted(per_day) and not is_transient(per_day)
    assert not is_transient(not_found)  # a missing model is never retried
    assert is_transient(server)


def test_quote_check_treats_every_dash_as_a_hyphen():
    """Seen live: models wrote a non-breaking hyphen (U+2011) where the source has "-", so a real quote was refused."""
    from verify.claims import quote_in

    source = "The primary evaluation found moderate-certainty evidence that walking lowered systolic blood pressure (MD = -4.1 mm Hg)."
    assert quote_in("moderate‑certainty evidence that walking lowered systolic", source)
    assert quote_in("moderate–certainty evidence", source)  # en dash
    assert quote_in("(MD = −4.1 mm Hg)", source)              # minus sign
    assert quote_in("moder­ate-certainty", source)            # soft hyphen
    assert not quote_in("high-certainty evidence", source)         # different words still fail
