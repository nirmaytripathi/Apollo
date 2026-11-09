"""Research-quality rules (tools/quality.py and their use in the graph), with no network or API keys."""

import json
import time

from core import config
from graph import nodes
from graph.state import initial_state
from graph.workflow import run_research
from tools import passages as passages_mod
from tools import quality

from .fakes import fake_embed, fake_llm, fake_search

ARTICLE = ("Researchers followed 2,000 farmers for three seasons and measured water use on every plot. "
           "Drip irrigation cut water use by 31 percent compared with flood irrigation in the same villages. "
           "Yields stayed the same, and the authors note that subsidies paid for most of the equipment. ") * 3


def _state(**extra):
    s = initial_state("topic", max_rounds=2, timeout_seconds=60, started_at=time.time())
    s["plan"] = ["q one", "q two"]
    s.update(extra)
    return s


def test_source_kind_puts_research_and_official_sources_apart():
    assert quality.source_kind("https://pmc.ncbi.nlm.nih.gov/articles/PMC123/") == "primary"
    assert quality.source_kind("https://www.nature.com/articles/s41586") == "primary"
    assert quality.source_kind("https://pib.gov.in/PressRelease.aspx?id=1") == "primary"
    assert quality.source_kind("https://www.bls.gov/opub/") == "primary"
    assert quality.source_kind("https://medium.com/@x/what-is-rag") == "secondary"
    assert quality.source_kind("https://www.geeksforgeeks.org/dijkstra/") == "secondary"


def test_published_date_comes_from_page_metadata_or_is_unknown():
    meta = '<head><meta property="article:published_time" content="2024-03-05T10:00:00Z"></head>'
    assert quality.published_date(meta) == "2024-03-05"
    assert quality.published_date('<meta name="citation_publication_date" content="2021/7">') == "2021-07"
    assert quality.published_date('<script>{"datePublished": "2019-11-02"}</script>') == "2019-11-02"
    assert quality.published_date("<head><title>No date here</title></head>") == ""  # never guessed


def test_search_reads_original_research_first_and_drops_duplicate_articles(monkeypatch):
    results = [{"title": "Blog", "url": "https://blog.example.com/drip", "snippet": "s"},
               {"title": "Copy", "url": "https://news.example.com/drip-copy", "snippet": "s"},
               {"title": "Study", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC9/", "snippet": "s"},
               {"title": "Other", "url": "https://other.example.com/x", "snippet": "s"}]
    monkeypatch.setattr(nodes, "web_search", lambda q, errors: results)
    texts = {"https://blog.example.com/drip": ARTICLE, "https://news.example.com/drip-copy": ARTICLE + " Shared from a blog.",
             "https://pmc.ncbi.nlm.nih.gov/articles/PMC9/": "A different study text about yields. " * 30}
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (texts.get(url, ""), "trafilatura"))
    monkeypatch.setattr(nodes, "page_date", lambda url: "2023-04-01" if "pmc" in url else "")
    state = _state(pending_queries=[{"query": "drip irrigation", "section": 0}])

    out = nodes.athena_search(state)

    found = out["findings"]
    assert found[0]["url"].startswith("https://pmc.") and found[0]["kind"] == "primary"  # research read first
    assert [f["id"] for f in found] == ["S1", "S2"]  # the copy was dropped, and the numbering has no gap
    assert out["duplicates"] == [{"url": "https://news.example.com/drip-copy", "duplicate_of": "S2"}]
    assert found[0]["published"] == "2023-04-01" and found[1]["published"] == ""  # unknown stays unknown
    assert all(f["retrieved_at"] > 0 for f in found)  # research date, kept apart from the publication date


def test_thin_sub_question_gets_a_follow_up_even_when_hermes_says_complete(monkeypatch):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": true, "gaps": []}'))
    monkeypatch.setattr(config, "MIN_SOURCES_PER_QUESTION", 10)  # never met: 3 readable pages per query, 2 rounds
    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(nodes, "verify_report", lambda report, findings, errors: [])  # verification has its own tests

    final = run_research("topic", max_rounds=2, timeout_seconds=60)

    assert final["round_no"] == 2  # Hermes said complete, but coverage was below target
    assert any(q.endswith("research study") for q in final["searched_queries"])
    coverage = next(c for c in final["quality"] if c["id"] == "coverage")
    assert coverage["status"] == "warn" and "sub-question" in coverage["detail"]


def test_quality_checks_report_limits_and_conflicts_in_the_report():
    findings = [
        {"id": "S1", "section": 0, "url": "https://pmc.ncbi.nlm.nih.gov/a", "title": "Trial", "content": "x", "snippet": "",
         "kind": "primary", "published": "2022-01-01", "retrieved_at": 1},
        {"id": "S2", "section": 0, "url": "https://blog.example.com/b", "title": "Blog", "content": "y", "snippet": "",
         "kind": "secondary", "published": "", "retrieved_at": 1},
    ]
    claims = [
        {"claim": "Yields rose [S1].", "status": "verified", "source_ids": ["S1"], "best_match": "", "similarity": 0.9, "evidence": "",
         "parts": [{"text": "Yields rose", "verdict": "supported", "passage": "S1.1", "source_id": "S1", "quote": "yields rose", "note": ""}],
         "passages": []},
        {"claim": "Costs doubled [S2].", "status": "contradicted", "source_ids": ["S2"], "best_match": "", "similarity": 0.5, "evidence": "",
         "parts": [{"text": "Costs doubled", "verdict": "contradicted", "passage": "S2.1", "source_id": "S2", "quote": "costs fell", "note": ""}],
         "passages": []},
        {"claim": "Water use fell [S2].", "status": "verified", "source_ids": ["S2"], "best_match": "", "similarity": 0.5, "evidence": "",
         "parts": [], "passages": []},
    ]
    state = _state(findings=findings, claims=claims, report="# T\n\nYields rose [S1]. Costs doubled [S2]. Water use fell [S2].",
                   contradictions=[{"source_a": "S1", "source_b": "S2", "claim": "whether costs rose"}],
                   sections=[{"index": 1, "question": "q two", "text": "Not found in sources.", "source_ids": [], "errors": []}],
                   events=["[Athena] No results for 'q two'; continuing."])

    out = nodes.quality_check(state)

    checks = {c["id"]: c for c in out["quality"]}
    assert checks["coverage"]["status"] == "warn"            # 2 readable sources for sub-question 1, none for 2
    assert checks["results"]["status"] == "warn" and "sub-question(s) 2" in checks["results"]["detail"]
    assert checks["primary"]["status"] == "pass"
    assert checks["quotes"]["status"] == "fail" and "[3]" in checks["quotes"]["detail"]  # verified without a quote
    assert checks["contradictions"]["status"] == "warn" and "C2" in checks["contradictions"]["detail"]
    assert checks["dates"]["status"] == "warn" and "1 of 2" in checks["dates"]["detail"]
    assert out["quality_overall"] == "failed"
    section = out["report"].split("## Evidence limits and quality checks", 1)[1]
    assert "Sources S1 (Trial) and S2 (Blog) disagree: whether costs rose" in section
    assert 'Claim C2 is contradicted by its source (S2: "costs fell")' in section
    assert "[S" not in section  # the added section cites nothing, so it can never be mistaken for a claim


def test_writer_sees_original_research_first_and_marked(monkeypatch):
    monkeypatch.setattr(nodes, "relevant_passages", lambda text, q, n, errors=None: text)
    block = nodes._source_block([
        {"id": "S1", "title": "Blog", "content": "blog text", "snippet": "", "kind": "secondary"},
        {"id": "S2", "title": "Trial", "content": "trial text", "snippet": "", "kind": "primary"},
    ], "q", [])
    assert block.startswith("[S2] (original research or official) Trial") and "[S1] Blog" in block


def test_overall_verdict():
    assert quality.overall([{"status": "pass"}]) == "passed"
    assert quality.overall([{"status": "pass"}, {"status": "warn"}]) == "passed_with_warnings"
    assert quality.overall([{"status": "warn"}, {"status": "fail"}]) == "failed"
    assert json.dumps(quality.quality_checks(_state(report="# T"))) # an empty run still yields checks


def test_pages_not_about_their_sub_question_are_dropped(monkeypatch):
    question = "What are the reported impacts of alternate-day fasting on insulin sensitivity in type 2 diabetes?"
    results = [{"title": "reported", "url": "https://www.dictionary.com/browse/reported", "snippet": "s"},
               {"title": "Trial", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/", "snippet": "s"}]
    texts = {results[0]["url"]: "Reported: past tense of report. Synonyms: told, stated. Example sentences. " * 5,
             results[1]["url"]: ("In this trial, alternate-day fasting improved insulin sensitivity in adults with type 2 "
                                 "diabetes; the reported impacts on glucose were modest. ") * 5}
    monkeypatch.setattr(nodes, "web_search", lambda q, errors: results)
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (texts[url], "trafilatura"))
    monkeypatch.setattr(nodes, "page_date", lambda url: "")
    state = _state(plan=[question], pending_queries=[{"query": question, "section": 0}])

    out = nodes.athena_search(state)

    assert [f["url"] for f in out["findings"]] == [results[1]["url"]] and out["findings"][0]["id"] == "S1"
    assert out["irrelevant"][0]["url"] == results[0]["url"] and out["irrelevant"][0]["relevance"] < config.MIN_RELEVANCE


def test_results_check_tells_unanswered_from_partly_answered_sub_questions():
    sections = [{"index": 0, "question": "a", "text": "Yields rose [S1]. Costs were not found in sources.", "source_ids": [], "errors": []},
                {"index": 1, "question": "b", "text": "Not found in sources.", "source_ids": [], "errors": []}]
    checks = {c["id"]: c for c in quality.quality_checks(_state(report="# T", sections=sections))}
    detail = checks["results"]["detail"]
    assert "no answer found in the sources for sub-question(s) 2" in detail.lower()
    assert "parts of sub-question(s) 1 were not found" in detail


def test_a_dropped_page_is_not_read_again_in_a_later_round(monkeypatch):
    url = "https://www.dictionary.com/browse/reported"
    monkeypatch.setattr(nodes, "web_search", lambda q, errors: [{"title": "t", "url": url, "snippet": "s"}])
    read = []
    monkeypatch.setattr(nodes, "page_content", lambda u, raw, errors: read.append(u) or ("text", "trafilatura"))
    state = _state(pending_queries=[{"query": "q one", "section": 0}], irrelevant=[{"url": url, "relevance": 0.0}])

    out = nodes.athena_search(state)

    assert read == [] and out["findings"] == [] and len(out["irrelevant"]) == 1


def test_one_paper_on_two_urls_is_a_duplicate_even_with_page_clutter():
    paper = ("Coffee and cardiovascular disease. Habitual coffee intake was not associated with a higher risk of "
             "coronary heart disease in large cohort studies, and moderate intake was linked to lower mortality. ") * 6
    clutter = "This website utilizes technologies such as cookies to enable essential site functionality, analytics and personalization. " * 4
    assert quality.same_content(paper, clutter + paper[: len(paper) * 2 // 3])
    other = "Drip irrigation cut water use by a third in Maharashtra while yields stayed the same across three seasons. " * 6
    assert not quality.same_content(paper, other)


def test_the_same_conflict_is_listed_once(monkeypatch):
    reply = json.dumps({"complete": True, "gaps": [], "contradictions": [
        {"source_a": "S1", "source_b": "S3", "claim": "heavy coffee has no role in heart disease"},
        {"source_a": "S3", "source_b": "S1", "claim": "heavy coffee raises heart disease risk"}]})
    monkeypatch.setattr(nodes, "llm_chat", lambda *a, **k: reply)
    findings = [{"id": i, "section": 0, "title": i, "url": f"https://x/{i}", "snippet": "", "content": "text " * 50}
                for i in ("S1", "S2", "S3")]
    out = nodes.hermes_review(_state(findings=findings, searched_queries=["q one"]))
    assert len(out["contradictions"]) == 1
