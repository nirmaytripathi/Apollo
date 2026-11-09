"""Phase 3 tests: the FastAPI layer and event conversion, fully offline.

The graph itself is the frozen Phase 1-2 code; search, extraction, the LLM and
the embedding model are fakes (tests/fakes.py), so a whole run takes well under
a second and these tests check the API's behaviour, not external services.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

import api
from core import config, db
from sqlalchemy import select
from graph import nodes
from tools import passages as passages_mod
from verify import claims as claims_mod

from .fakes import PLAN, fake_embed, fake_llm, fake_search, verify_reply

COMPLETE = lambda n: '{"complete": true, "gaps": []}'
GAPS = lambda n: json.dumps({"complete": False, "gaps": [{"section": 1, "query": f"gap query {n}"}]})


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'apollo-test.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "no-legacy-users.db"))
    api._runs.clear()


@pytest.fixture
def client():
    """A signed-in client: every API route except /api/auth/* needs a session."""
    c = TestClient(api.app)
    assert c.post("/api/auth/register", json={"name": "Tester", "email": "tester@example.com", "password": "correct horse"}).status_code == 201
    return c


def _wait(client, run_id, predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        status = client.get(f"/api/runs/{run_id}").json()
        if predicate(status):
            return status
        time.sleep(0.05)
    raise AssertionError(f"timed out; last status {status}")


def _sse_events(client, run_id):
    """Read the SSE stream to the end and return the decoded events."""
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _start(client, **body):
    resp = client.post("/api/runs", json={"topic": "drones in farming", **body})
    assert resp.status_code == 200, resp.text
    return resp.json()["run_id"]


def test_run_streams_ordered_events_and_serves_report(client):
    run_id = _start(client, max_rounds=2)
    events = _sse_events(client, run_id)
    types = [e["type"] for e in events]

    assert types[0] == "plan" and types[-1] == "complete"
    assert types.index("search_started") < types.index("search_done") < types.index("review")
    assert types.index("review") < types.index("section_written") < types.index("verification")
    assert types[-3:] == ["contradictions", "report_ready", "complete"]
    assert types.count("section_written") == len(PLAN)
    assert types.count("search_started") == len(PLAN)  # one event per query
    assert all({"type", "data", "ts"} <= e.keys() and isinstance(e["ts"], int) for e in events)
    assert events[-1]["data"]["status"] == "complete"

    # contract field names
    first = {t: next(e["data"] for e in events if e["type"] == t) for t in set(types)}
    assert set(first["plan"]) == {"sub_questions", "awaiting_approval"} and first["plan"]["sub_questions"] == PLAN
    assert [e["data"]["index"] for e in events if e["type"] == "search_started"] == [1, 2, 3]
    assert {"round", "index", "total", "query"} <= set(first["search_started"]) and first["search_started"]["total"] == 3
    assert {"query", "result_count"} <= set(first["search_done"])
    assert {"round", "complete", "gaps"} <= set(first["review"])
    assert "section" in first["section_written"]
    assert {"verified", "partial", "not_found"} <= set(first["verification"])
    assert set(first["contradictions"]) == {"count", "reviews", "reviews_failed"} and first["report_ready"] == {}
    assert types.count("plan") == 1  # an unchanged, auto-approved plan is not announced twice

    report = client.get(f"/api/runs/{run_id}/report").json()
    assert report["markdown"].startswith("# drones in farming")
    assert {s["id"] for s in report["sources"]} >= {r["id"] for r in report["references"]}
    assert report["contradictions_html_path"] is None
    assert all({"claim", "status", "evidence"} <= set(c) for c in report["verification"])
    md = client.get(f"/api/runs/{run_id}/report.md")
    assert md.status_code == 200 and "## References" in md.text
    assert "attachment" in md.headers["content-disposition"]

    assert {c["id"] for c in report["quality"]} >= {"coverage", "citations", "quotes", "contradictions", "dates"}
    assert report["quality_overall"] in {"passed", "passed_with_warnings"} and types.count("quality") == 1
    assert all(s["kind"] in {"primary", "secondary"} and s["retrieved_at"] for s in report["sources"])

    evidence = client.get(f"/api/runs/{run_id}/evidence").json()
    assert evidence["legacy"] is False and len(evidence["claims"]) == len(report["verification"])
    first = evidence["claims"][0]
    assert first["n"] == 1 and "[S" not in first["text"] and first["status"] == "verified"
    assert first["parts"][0]["verdict"] == "supported" and first["parts"][0]["passage"] in {p["label"] for p in first["passages"]}
    assert {p["source_id"] for p in first["passages"]} <= {s["id"] for s in evidence["sources"]}


def test_evidence_of_runs_verified_before_claim_parts_is_marked_legacy(client):
    run_id = _start(client)
    _sse_events(client, run_id)
    with api._lock:
        state = api._runs[run_id].state
        for c in state["claims"]:
            c.pop("parts"), c.pop("passages")
    evidence = client.get(f"/api/runs/{run_id}/evidence").json()
    assert evidence["legacy"] is True and evidence["claims"][0]["parts"] == []


def test_failed_search_becomes_search_failed_event_and_run_completes(client, monkeypatch):
    def search(query, errors):
        if query.startswith("q two"):  # also its coverage follow-up, "q two research study"
            errors.append(f"search[tavily] returned no results for {query!r}")
            return []
        return fake_search()(query, errors)

    monkeypatch.setattr(nodes, "web_search", search)
    events = _sse_events(client, _start(client))
    failed = [e for e in events if e["type"] == "search_failed"]
    assert [e["data"]["query"] for e in failed] == ["q two", "q two research study"]  # + coverage follow-up
    assert events[-1]["data"]["status"] == "complete"
    assert any(e["type"] == "section_written" and e["data"]["not_found"] for e in events)


def test_hitl_edit_plan_then_skip_gaps(client, monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(GAPS))
    run_id = _start(client, hitl=True)

    status = _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    assert status["pending"]["type"] == "approve_plan"
    edited = ["edited question one", "q two"]
    # contract: "approve with edits" carries payload {sub_questions}
    assert client.post(f"/api/runs/{run_id}/resume", json={"action": "approve", "payload": {"sub_questions": edited}}).json() == {"ok": True}

    status = _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    assert status["pending"]["type"] == "approve_gaps"
    client.post(f"/api/runs/{run_id}/resume", json={"action": "skip"})
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)

    events = _sse_events(client, run_id)  # full replay of the finished run
    started = [e["data"]["query"] for e in events if e["type"] == "search_started" and e["data"]["round"] == 1]
    assert started == edited
    plans = [e["data"] for e in events if e["type"] == "plan"]
    assert [p["awaiting_approval"] for p in plans] == [False, True, False] and plans[-1]["sub_questions"] == edited
    assert any(e["type"] == "gaps_pending" and e["data"]["gaps"] for e in events)
    assert events[-1]["data"] == {**events[-1]["data"], "status": "complete", "stop_reason": "gaps_skipped_by_user"}


def test_hitl_approve_gaps_with_edits(client, monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(GAPS))
    run_id = _start(client, hitl=True)
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    client.post(f"/api/runs/{run_id}/resume", json={"action": "approve"})
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input" and s["pending"]["type"] == "approve_gaps")
    client.post(f"/api/runs/{run_id}/resume", json={"action": "approve", "payload": {"gaps": ["my own gap query"]}})
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)

    events = _sse_events(client, run_id)
    round2 = [e["data"]["query"] for e in events if e["type"] == "search_started" and e["data"]["round"] == 2]
    assert round2 == ["my own gap query"]


def test_reject_plan_cancels_run(client):
    run_id = _start(client, hitl=True)
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    client.post(f"/api/runs/{run_id}/resume", json={"action": "reject"})
    status = _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    assert status["status"] == "rejected"
    end = _sse_events(client, run_id)[-1]
    assert end["type"] == "complete" and end["data"]["status"] == "failed" and end["data"]["stop_reason"] == "plan_rejected_by_user"
    assert client.get(f"/api/runs/{run_id}/report").status_code == 409


def test_graph_exception_becomes_error_event_not_500(client, monkeypatch):
    def broken(state):
        raise RuntimeError("search backend exploded")

    monkeypatch.setattr(nodes, "athena_search", broken)
    run_id = _start(client)
    events = _sse_events(client, run_id)
    fatal = [e for e in events if e["type"] == "error"]
    assert len(fatal) == 1 and "search backend exploded" in fatal[0]["data"]["message"]
    assert "Traceback" not in json.dumps(events)
    assert events[-1] == {**events[-1], "type": "complete"} and events[-1]["data"]["status"] == "failed"
    assert client.get(f"/api/runs/{run_id}/report").status_code == 409


def test_request_validation(client):
    assert client.post("/api/runs", json={"topic": "x" * 10, "max_rounds": 3}).status_code == 422  # hard limit is 2
    assert client.post("/api/runs", json={"topic": "ab"}).status_code == 422
    assert client.get("/api/runs/nope/events").status_code == 404
    run_id = _start(client)
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    assert client.post(f"/api/runs/{run_id}/resume", json={"action": "approve"}).status_code == 409


def test_sse_resumes_after_last_event_id(client):
    run_id = _start(client)
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    everything = _sse_events(client, run_id)
    with client.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "4"}) as resp:
        tail = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    assert tail == everything[5:]


def test_demo_topics_and_cache_clear(client, monkeypatch, tmp_path):
    md = tmp_path / "topics.md"
    md.write_text(
        "# Demo\n\n## Topic A: two rounds\nSome notes.\n**Topic:** `First topic`\nSettings: max rounds **2**.\n\n"
        "## Topic B\n**Topic:** Second topic\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "DEMO_TOPICS_FILE", md)
    assert client.get("/api/demo-topics").json() == [
        {"id": "A", "title": "Two rounds", "topic": "First topic", "note": "max rounds 2."},
        {"id": "B", "title": "Topic B", "topic": "Second topic", "note": None},
    ]
    run_id = _start(client)
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    assert client.post("/api/cache/clear").json() == {"runs_forgotten": 1, "output_dirs_removed": 1}
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_demo_switch_fails_only_topic_b_subquestion(client, monkeypatch, tmp_path):
    md = tmp_path / "topics.md"
    md.write_text("## Topic A\n**Topic:** topic alpha\n\n## Topic B\n**Topic:** topic bravo\n", encoding="utf-8")
    monkeypatch.setattr(api, "DEMO_TOPICS_FILE", md)
    monkeypatch.setattr(api, "DEMO_FAIL_SUBQUESTION", 2)
    monkeypatch.setattr(api, "_real_search", fake_search())
    monkeypatch.setattr(nodes, "web_search", api._demo_search)

    def run(topic):
        resp = client.post("/api/runs", json={"topic": topic, "max_rounds": 1})
        return _sse_events(client, resp.json()["run_id"])

    events_b = run("topic bravo")
    failed = [e["data"] for e in events_b if e["type"] == "search_failed"]
    assert [f["query"] for f in failed] == [PLAN[1]]
    assert "simulated outage (demo switch" in failed[0]["reason"]
    assert events_b[-1]["data"]["status"] == "complete"
    section2 = [e["data"] for e in events_b if e["type"] == "section_written" and e["data"]["index"] == 1]
    assert section2[0]["not_found"] and section2[0]["citations"] == 0

    events_a = run("topic alpha")  # any other topic is never affected
    assert not any(e["type"] == "search_failed" for e in events_a)


def test_problems_the_run_survives_are_non_terminal_warnings(client, monkeypatch):
    def flaky_page(url, raw, errors):
        errors.append(f"extract failed for {url}: HTTPError: 403")
        return "", ""

    monkeypatch.setattr(nodes, "page_content", flaky_page)
    events = _sse_events(client, _start(client))
    warnings = [e["data"] for e in events if e["type"] == "warning"]
    assert warnings and warnings[0]["message"].endswith("while searching") and warnings[0]["details"]
    assert not any(e["type"] == "error" for e in events)  # nothing terminal...
    assert events[-1]["type"] == "complete"  # ...and the run ran to the end


def test_contradictions_route(client):
    run_id = _start(client)
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    resp = client.get(f"/api/runs/{run_id}/contradictions")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------- run list, metrics, persistence, system


def test_usage_meter_attributes_calls_to_run_and_node(client, monkeypatch):
    from core import llm

    fake = fake_llm(COMPLETE)
    monkeypatch.setattr(llm, "_call", lambda provider, name, system, user, temperature: (fake(system, user, temperature), 100, 20))
    monkeypatch.setattr(llm, "_stream", lambda provider, name, system, user, temperature, on_text: (fake(system, user, temperature, on_text=on_text), 100, 20))
    monkeypatch.setattr(nodes, "llm_chat", llm.chat)  # the real wrapper, so every call is metered
    monkeypatch.setattr(api, "_real_search", fake_search())
    monkeypatch.setattr(nodes, "web_search", api._metered_search)
    run_id = _start(client)
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)

    m = client.get(f"/api/runs/{run_id}/metrics").json()
    nodes_seen = {row["name"] for row in m["by_node"]}
    assert {"apollo_plan", "athena_search", "hermes_review"} <= nodes_seen
    assert m["summary"]["llm_calls"] >= 3 and m["summary"]["prompt_tokens"] == 100 * m["summary"]["llm_calls"]
    assert m["summary"]["searches"] == len(PLAN)
    assert m["cost_usd"] is None  # no PRICE_* configured: no invented cost
    assert [s["stage"] for s in m["stages"]][:2] == ["planning", "searching"]


def test_run_list_and_reload_from_disk(client):
    run_id = _start(client)
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    row = client.get("/api/runs").json()[0]
    assert row["run_id"] == run_id and row["has_report"] and row["sources"] > 0
    report = client.get(f"/api/runs/{run_id}/report").json()
    assert report["plan"] == PLAN and report["citation_check"]["dangling"] == []

    api._runs.clear()  # simulate a server restart
    api._load_saved()
    assert client.get(f"/api/runs/{run_id}/report").json()["markdown"] == report["markdown"]
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "complete"


def test_system_reports_key_presence_never_values(client, monkeypatch):
    from core import config

    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_secretvalue_should_not_leak_1234567")
    body = client.get("/api/system").text
    assert "secretvalue" not in body
    assert json.loads(body)["keys"]["GROQ_API_KEY"] is True


def test_failed_hermes_review_is_reported_not_hidden_as_zero_conflicts(client, monkeypatch):
    def broken_review(n):
        raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(nodes, "llm_chat", fake_llm(broken_review))
    run_id = _start(client)
    events = _sse_events(client, run_id)
    conflict = next(e["data"] for e in events if e["type"] == "contradictions")
    assert conflict["count"] == 0 and conflict["reviews"] >= 1
    assert conflict["reviews_failed"] == conflict["reviews"]  # nothing was actually checked
    assert client.get(f"/api/runs/{run_id}/report").json()["review_health"] == {k: conflict[k] for k in ("reviews", "reviews_failed")}
    row = client.get("/api/runs").json()[0]
    assert row["reviews_failed"] == row["reviews"] >= 1


def test_successful_review_reports_no_failures(client):
    events = _sse_events(client, _start(client))
    conflict = next(e["data"] for e in events if e["type"] == "contradictions")
    assert conflict["reviews"] >= 1 and conflict["reviews_failed"] == 0


# ---------------------------------------------------------------- live streaming of the report


def test_report_streams_before_verification(client):
    run_id = _start(client)
    events = _sse_events(client, run_id)
    types = [e["type"] for e in events]

    drafts = [e for e in events if e["type"] == "section_draft"]
    assert drafts and all({"index", "text"} <= e["data"].keys() and e["data"]["text"] for e in drafts)
    for d in drafts:  # each section's draft arrives before that section's final text
        final = next(i for i, e in enumerate(events) if e["type"] == "section_written" and e["data"]["index"] == d["data"]["index"])
        assert events.index(d) < final
    written = [e["data"] for e in events if e["type"] == "section_written"]
    assert all(w["text"] for w in written)  # final text travels with the event

    # the whole report is on screen before the (slow) claim verification ends
    assert types.index("report_written") < types.index("verification")
    report = client.get(f"/api/runs/{run_id}/report").json()
    streamed = next(e["data"]["markdown"] for e in events if e["type"] == "report_written")
    # The final report is the streamed one plus the section the quality checks add.
    assert report["markdown"].startswith(streamed) and "## Evidence limits and quality checks" in report["markdown"]

    found = next(e["data"] for e in events if e["type"] == "search_done" and e["data"]["result_count"])
    assert len(found["sources"]) == found["result_count"] and {"id", "title", "url"} <= found["sources"][0].keys()


def test_drafts_are_not_saved_to_disk(client):
    run_id = _start(client)
    _wait(client, run_id, lambda s: s["status"] not in api.ACTIVE)
    with db.engine().connect() as conn:
        saved = conn.execute(select(db.runs.c.doc).where(db.runs.c.id == run_id)).scalar_one()
    assert not any(e["type"] == "section_draft" for e in saved["events"])
    assert any(e["type"] == "section_written" for e in saved["events"])


def test_search_hits_stream_before_the_search_step_ends(client, monkeypatch):
    monkeypatch.setattr(api, "_real_search", fake_search())
    monkeypatch.setattr(nodes, "web_search", api._metered_search)
    events = _sse_events(client, _start(client))
    types = [e["type"] for e in events]
    hits = [e["data"] for e in events if e["type"] == "search_results"]
    assert len(hits) == len(PLAN) and all(h["results"] and {"title", "url"} <= h["results"][0].keys() for h in hits)
    assert types.index("search_results") < types.index("search_done")


def test_stream_resumes_after_last_event_id_query(client):
    run_id = _start(client)
    events = _sse_events(client, run_id)
    resumed = []
    with client.stream("GET", f"/api/runs/{run_id}/events?last_event_id=2") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                resumed.append(json.loads(line[6:]))
    assert resumed == events[3:]  # nothing repeated, nothing missed


def test_verification_progress_reaches_the_stream(client):
    events = _sse_events(client, _start(client))
    progress = [e["data"] for e in events if e["type"] == "claim_checked"]
    total = next(e["data"] for e in events if e["type"] == "verification")
    assert progress and progress[-1]["total"] == sum(total.values())  # one per checked claim
    assert sorted(p["done"] for p in progress) == list(range(1, len(progress) + 1))
    types = [e["type"] for e in events]
    assert types.index("report_written") < types.index("claim_checked") < types.index("verification")
