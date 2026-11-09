"""Source APIs per domain (tools/sources/): primary -> fallback -> cache -> graceful error, the cache
keys and TTLs, thread safety, replay mode, and Athena's use of domain results.

Every test here runs with the network switched off at the socket level (`no_network`), so a test that
reaches a real API fails instead of spending quota. HTTP failures are simulated with fake responses.
"""

import json
import socket
import threading
import time

import pytest
import requests

from core import config
from graph import nodes
from graph.workflow import run_research
from tools import retry_policy
from tools.sources import adapters, cache, router
from tools.sources.adapters import Adapter

from .fakes import fake_embed, fake_llm, fake_search, verify_reply

Q = "exercise trial results"
ACADEMIC = adapters.ADAPTERS["openalex"], adapters.ADAPTERS["crossref"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(retry_policy, "_backoff", lambda retry_state: 0)  # retries without waiting
    return attempts


def good(query):
    return [{"title": f"{query} paper {i}", "url": f"https://doi.org/10.1000/{i}", "snippet": f"On {query}.",
             "raw_content": f"This paper reports {query} from a controlled study. " * 8, "published": "2024-05-01",
             "api_text": True} for i in range(3)]


def failing(exc):
    def _fn(query):
        raise exc
    return _fn


def install(monkeypatch, name, fn):
    monkeypatch.setitem(adapters.ADAPTERS, name, Adapter(fn))


def http(status: int, payload: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code, resp.url = status, "https://api.test/"
    resp._content = json.dumps(payload or {}).encode()
    return resp


@pytest.fixture
def counted_get(monkeypatch):
    """Replace requests.get with a fake answering one status; returns the list of URLs called."""
    calls = []

    def setup(status=None, exc=None):
        def fake_get(url, **kwargs):
            calls.append(url)
            if exc is not None:
                raise exc
            return http(status)
        monkeypatch.setattr(adapters.requests, "get", fake_get)
        return calls

    return setup


# ---------------------------------------------------------------- 1-7: primary and fallback


def test_1_primary_success_skips_the_fallback(monkeypatch):
    install(monkeypatch, "openalex", good)
    install(monkeypatch, "crossref", failing(AssertionError("fallback must not be called")))
    out = router.fetch("academic", Q)
    assert out["success"] and out["source"] == "openalex" and not out["is_cached"] and out["error"] is None
    assert len(out["results"]) == 3 and all(r["freshness"] == "live" and r["source"] == "openalex" for r in out["results"])
    assert out["log"]["primary_status"] == "ok" and out["log"]["fallback_status"] == ""


def test_2_primary_empty_triggers_the_fallback(monkeypatch):
    install(monkeypatch, "openalex", lambda q: [])
    install(monkeypatch, "crossref", good)
    out = router.fetch("academic", Q)
    assert out["source"] == "crossref" and out["log"]["fallback_reason"] == "empty"
    assert any("search[openalex] returned no results" in e for e in out["errors"])


def test_3_irrelevant_primary_results_trigger_the_fallback(monkeypatch):
    off_topic = [{"title": "Cooking", "url": "https://ex.com/a", "snippet": "",
                  "raw_content": "A recipe for bread and soup with onions and garlic. " * 10}]
    install(monkeypatch, "openalex", lambda q: off_topic)
    install(monkeypatch, "crossref", good)
    out = router.fetch("academic", Q)
    assert out["source"] == "crossref" and out["log"]["fallback_reason"] == "irrelevant"
    assert "below relevance 0.30" in out["errors"][0]


def test_relevance_uses_the_existing_scorer_and_a_domain_override(monkeypatch):
    half = [{"title": "", "url": "https://ex.com/b", "snippet": "",
             "raw_content": "Exercise was measured in many people over the years of the programme. " * 5}]
    install(monkeypatch, "openalex", lambda q: half)  # shares 1 of the 3 key words: relevance 0.33
    install(monkeypatch, "crossref", good)
    assert router.fetch("academic", Q)["source"] == "openalex"
    monkeypatch.setitem(config.DOMAIN_RELEVANCE, "academic", 0.5)
    assert router.fetch("academic", Q)["source"] == "crossref"


def test_4_http_429_is_retried_then_falls_back(monkeypatch, counted_get):
    calls = counted_get(status=429)
    install(monkeypatch, "europe_pmc", good)
    out = router.fetch("health", Q)
    assert len(calls) == 2  # bounded: PubMed gets one retry
    assert out["source"] == "europe_pmc" and out["log"]["fallback_reason"] == "rate_limit"


def test_5_http_500_is_retried_then_falls_back(monkeypatch, counted_get):
    calls = counted_get(status=500)
    install(monkeypatch, "europe_pmc", good)
    out = router.fetch("health", Q)
    assert len(calls) == 2
    assert out["source"] == "europe_pmc" and out["log"]["fallback_reason"] == "server"


def test_6_timeout_is_retried_then_falls_back(monkeypatch, counted_get):
    calls = counted_get(exc=requests.Timeout("read timed out"))
    install(monkeypatch, "europe_pmc", good)
    out = router.fetch("health", Q)
    assert len(calls) == 2
    assert out["source"] == "europe_pmc" and out["log"]["fallback_reason"] == "timeout"


def test_7_fallback_success_is_live_and_cached(monkeypatch):
    install(monkeypatch, "openalex", failing(requests.ConnectionError("unreachable")))
    install(monkeypatch, "crossref", good)
    out = router.fetch("academic", Q)
    assert out["success"] and out["source"] == "crossref" and not out["is_cached"]
    assert out["log"]["fallback_status"] == "ok" and out["log"]["fallback_reason"] == "network"
    assert cache.get(router.cache_key("academic", Q, {"limit": config.RESULTS_PER_QUERY}))["source"] == "crossref"


# ---------------------------------------------------------------- 8-10: cache recovery


def _both_fail(monkeypatch):
    install(monkeypatch, "openalex", failing(requests.ConnectionError("down")))
    install(monkeypatch, "crossref", failing(requests.ConnectionError("down")))


def test_8_both_fail_fresh_cache_answers_marked_cached(monkeypatch):
    install(monkeypatch, "openalex", good)
    install(monkeypatch, "crossref", good)
    router.fetch("academic", Q)  # a live answer, stored
    _both_fail(monkeypatch)
    out = router.fetch("academic", "  EXERCISE   trial results ")  # same query once normalised
    assert out["success"] and out["is_cached"] and not out["is_stale"] and out["source"] == "openalex"
    assert all(r["freshness"] == "cached" for r in out["results"])
    assert out["log"]["cache"] == "hit" and out["log"]["final_source"] == "cache:openalex"


def test_9_both_fail_only_stale_cache_answers_marked_stale(monkeypatch):
    params = {"limit": config.RESULTS_PER_QUERY}
    old = time.time() - cache.ttl_seconds("academic") - 60
    cache.put(router.cache_key("academic", Q, params), "academic", router.normalize_query(Q), params,
              "openalex", good(Q), now=old)
    _both_fail(monkeypatch)
    out = router.fetch("academic", Q)
    assert out["success"] and out["is_cached"] and out["is_stale"] and out["log"]["stale"] is True
    assert all(r["freshness"] == "stale" for r in out["results"])
    assert any("using stale cached results" in e for e in out["errors"])


def test_10_both_fail_empty_cache_is_a_graceful_error(monkeypatch):
    _both_fail(monkeypatch)
    out = router.fetch("academic", Q)
    assert out["success"] is False and out["results"] == [] and "no cached results" in out["error"]
    assert out["log"]["cache"] == "miss" and out["log"]["error_category"] == "network"
    assert config.LLM_MODE == "live"  # a failure never switches to replay


def test_one_domain_failing_keeps_another_domains_results(monkeypatch):
    _both_fail(monkeypatch)
    install(monkeypatch, "pubmed", good)
    errors: list[str] = []
    assert router.search_domain("academic", Q, errors) == []
    assert len(router.search_domain("health", "blood pressure trial", errors)) == 3


def test_expired_entries_are_kept_not_deleted():
    cache.put("k", "news", "q", {}, "gdelt", [{"url": "u"}], now=time.time() - 10 * 86400)
    entry = cache.get("k")
    assert entry is not None and entry["is_stale"] and entry["results"] == [{"url": "u"}]


def test_ttl_defaults_and_override(monkeypatch):
    assert cache.ttl_seconds("academic") == 30 * 86400 and cache.ttl_seconds("news") == 86400
    assert config._per_domain("X_TTL_UNSET", {"news": 1})["news"] == 1
    monkeypatch.setenv("X_TTL", "news:0.5, bad, tech:x")
    assert config._per_domain("X_TTL", {"news": 1, "tech": 3}) == {"news": 0.5, "tech": 3}


# ---------------------------------------------------------------- 11-13: cache keys


def test_11_same_normalised_query_same_key():
    assert router.cache_key("academic", "  Blood   PRESSURE\ttrial ") == router.cache_key("academic", "blood pressure trial")
    assert router.cache_key("academic", "blood pressure trial") != router.cache_key("academic", "blood pressure trials")


def test_12_different_domains_different_keys():
    assert router.cache_key("academic", "blood pressure") != router.cache_key("health", "blood pressure")


def test_13_different_parameters_different_keys():
    a = router.cache_key("news", "elections", {"since": "2024-01-01", "limit": 5})
    assert a != router.cache_key("news", "elections", {"since": "2025-01-01", "limit": 5})
    assert a == router.cache_key("news", "elections", {"limit": 5, "since": "2024-01-01"})  # order does not matter


# ---------------------------------------------------------------- 14: concurrency


def test_14_concurrent_cache_access_is_safe():
    failures: list[BaseException] = []

    def worker(n):
        try:
            for i in range(25):
                key = "shared" if i % 2 else f"own-{n}"
                assert cache.put(key, "general", "q", {}, f"w{n}", [{"url": f"https://ex.com/{n}/{i}"}])
                got = cache.get(key)
                assert got is not None and isinstance(got["results"], list)
        except BaseException as exc:  # collected: an assert inside a thread would be lost otherwise
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    assert all(cache.get(f"own-{n}")["source"] == f"w{n}" for n in range(16))


# ---------------------------------------------------------------- 15: replay mode


def test_the_network_guard_really_blocks_connections(no_network):
    with pytest.raises(requests.ConnectionError):
        requests.get("https://example.com", timeout=1)
    assert no_network  # the attempt was seen, so an empty list below means no call was made


def test_15_replay_mode_full_run_makes_no_network_call(monkeypatch, no_network):
    monkeypatch.setattr(config, "LLM_MODE", "replay")
    monkeypatch.setattr(config, "DOMAIN_SOURCES", ("academic", "health"))
    final = run_research("Does regular walking lower blood pressure?", max_rounds=1)
    assert no_network == []
    assert final["status"] == "complete" and final["report"]
    assert any(f.get("source_api") == "pubmed" for f in final["findings"])  # the health domain was used
    assert final["claims"] and all(c["status"] == "verified" for c in final["claims"])
    assert all("replay.invalid" in f["url"] for f in final["findings"])


def test_replay_scenarios_drive_fallback_cache_and_error(monkeypatch, no_network):
    monkeypatch.setattr(config, "LLM_MODE", "replay")
    monkeypatch.setattr(config, "REPLAY_SCENARIO", "ok")
    assert router.fetch("health", "walking blood pressure")["source"] == "pubmed"
    monkeypatch.setattr(config, "REPLAY_SCENARIO", "primary_down")
    assert router.fetch("health", "walking blood pressure")["source"] == "europe_pmc"
    monkeypatch.setattr(config, "REPLAY_SCENARIO", "all_down")
    assert router.fetch("health", "walking blood pressure")["is_cached"]  # recovered from the cache
    assert router.fetch("health", "never asked before")["success"] is False
    assert no_network == []


# ---------------------------------------------------------------- 16: credentials


def test_16_invalid_credentials_are_not_retried(monkeypatch, counted_get):
    calls = counted_get(status=401)
    out = router.fetch("academic", Q)
    assert len(calls) == 2  # one call to the primary, one to the fallback: no retries
    assert out["log"]["primary_status"] == "auth" and out["log"]["fallback_status"] == "auth"
    assert out["success"] is False


def test_missing_key_skips_the_provider_without_calling_it(monkeypatch):
    monkeypatch.setattr(adapters.web, "_tavily", failing(AssertionError("must not be called without a key")))
    monkeypatch.setattr(config, "SERPER_API_KEY", "k")
    monkeypatch.setattr(adapters.web, "_serper", good)
    out = router.fetch("general", Q)
    assert out["source"] == "serper" and out["log"]["primary_status"] == "not_configured"


# ---------------------------------------------------------------- adapters: parsing


def test_openalex_abstract_is_rebuilt_from_its_inverted_index():
    index = {"Walking": [0], "lowers": [1], "blood": [2, 5], "pressure.": [3], "Measured": [4]}
    assert adapters.abstract_from_inverted_index(index) == "Walking lowers blood pressure. Measured blood"
    assert adapters.abstract_from_inverted_index(None) == ""


def test_pubmed_xml_is_parsed_with_sections_and_dates():
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
      <ArticleTitle>Walking and <i>blood</i> pressure</ArticleTitle>
      <Abstract><AbstractText Label="RESULTS">Pressure fell by 5 mmHg.</AbstractText>
      <AbstractText>No harms were seen.</AbstractText></Abstract>
      <ArticleDate><Year>2024</Year><Month>3</Month><Day>7</Day></ArticleDate>
    </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    [r] = adapters.parse_pubmed_xml(xml)
    assert r["url"] == "https://pubmed.ncbi.nlm.nih.gov/123/" and r["title"] == "Walking and blood pressure"
    assert r["raw_content"] == "RESULTS: Pressure fell by 5 mmHg. No harms were seen." and r["published"] == "2024-03-07"
    assert r["api_text"] is True


def test_classify_routes_by_keywords():
    assert router.classify("Does walking lower blood pressure?") == "health"
    assert router.classify("What does research say about remote work?") == "academic"
    assert router.classify("Who won the G20 summit debate?") == "news"
    assert router.classify("How fast is India's GDP growth?") == "statistics"
    assert router.classify("Which Python library parses PDFs?") == "tech"
    assert router.classify("Who painted the Mona Lisa?") == "general"


def _json_get(monkeypatch, routes: dict):
    """requests.get answering JSON by URL substring (the first matching key wins)."""
    def fake_get(url, **kwargs):
        for part, payload in routes.items():
            if part in url:
                resp = http(200, payload)
                resp.url = url
                return resp
        raise AssertionError(f"unexpected URL {url}")
    monkeypatch.setattr(adapters.requests, "get", fake_get)


def test_world_bank_documents_are_parsed(monkeypatch):
    _json_get(monkeypatch, {"worldbank": {"documents": {
        "D1": {"display_title": "India development update", "abstracts": {"cdata!": "Growth was 7 percent."},
               "docdt": "2018-02-01T00:00:00Z", "url": "http://documents.worldbank.org/d1"},
        "facets": {}}}})
    [r] = adapters.world_bank("India growth")
    assert r["raw_content"] == "Growth was 7 percent." and r["published"] == "2018-02-01" and r["api_text"]


def test_imf_marks_current_and_future_years_as_projections(monkeypatch):
    year = time.gmtime().tm_year
    series = {str(y): v for y, v in zip(range(year - 2, year + 4), [3.1, 3.2, 3.3, 3.4, 3.5, 3.6])}
    _json_get(monkeypatch, {"/indicators": {"indicators": {
        "NGDP_RPCH": {"label": "Real GDP growth", "unit": "Annual percent change", "description": "GDP growth.",
                      "source": "World Economic Outlook"},
        "OTHER": {"label": "Unrelated", "description": "Nothing in common."}}},
        "/NGDP_RPCH/WEOWORLD": {"values": {"NGDP_RPCH": {"WEOWORLD": series}}}})
    monkeypatch.setattr(adapters, "_imf_indicators", {"at": 0.0, "items": {}})  # no list cached by another test
    [r] = adapters.imf("GDP growth")
    assert f"{year - 1}: 3.2;" in r["raw_content"] and f"{year}: 3.3 (IMF projection)" in r["raw_content"]
    assert str(year + 2) not in r["raw_content"] and r["published"] == str(year - 1)


def test_imf_indicator_list_is_fetched_once_then_kept(monkeypatch):
    monkeypatch.setattr(adapters, "_imf_indicators", {"at": 0.0, "items": {}})
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return http(200, {"indicators": {"X": {"label": "Unrelated"}}})
    monkeypatch.setattr(adapters.requests, "get", fake_get)
    adapters.imf("GDP growth")
    adapters.imf("inflation rate")
    assert len(calls) == 1


def test_gdelt_429_is_not_retried(counted_get):
    calls = counted_get(status=429)
    with pytest.raises(requests.HTTPError):
        adapters.gdelt("climate summit agreement")
    assert len(calls) == 1


def test_guardian_429_falls_back_to_tavily_news(monkeypatch, counted_get):
    calls = counted_get(status=429)
    monkeypatch.setattr(config, "GUARDIAN_API_KEY", "k")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "k")
    monkeypatch.setattr(adapters.web, "_tavily", lambda q, topic="general": good(q) if topic == "news" else [])
    out = router.fetch("news", "climate summit agreement")
    assert len(calls) == config.SEARCH_MAX_RETRIES + 1  # bounded retries on the Guardian only
    assert out["source"] == "tavily_news" and out["log"]["fallback_reason"] == "rate_limit"


def test_gdelt_text_answer_is_an_error_not_an_empty_success(monkeypatch):
    def fake_get(url, **kwargs):
        resp = http(200)
        resp._content = b"Your search contained a phrase that was too short."
        return resp
    monkeypatch.setattr(adapters.requests, "get", fake_get)
    with pytest.raises(ValueError, match="did not return JSON"):
        adapters.gdelt("ai")


def test_github_and_stack_exchange_results_are_read_as_pages(monkeypatch):
    _json_get(monkeypatch, {
        "api.github.com": {"items": [{"full_name": "a/b", "html_url": "https://github.com/a/b", "description": "A lib",
                                      "stargazers_count": 5, "language": "Python", "created_at": "2023-08-09T10:00:00Z"}]},
        "stackexchange": {"items": [{"title": "Timeout &amp; asyncio", "link": "https://stackoverflow.com/q/1",
                                     "body": "<p>How do I set a timeout?</p>", "creation_date": 1424304000}]}})
    [g] = adapters.github("python lib")
    [s] = adapters.stack_exchange("asyncio timeout")
    assert g["url"] == "https://github.com/a/b" and g["published"] == "2023-08-09" and not g["api_text"]
    assert s["title"] == "Timeout & asyncio" and s["snippet"] == "How do I set a timeout?"
    assert s["published"] == "2015-02-19" and not s["api_text"]


# ---------------------------------------------------------------- Athena


@pytest.fixture
def offline_run(monkeypatch):
    from tools import passages as passages_mod
    from verify import claims as claims_mod

    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. A finding.", "trafilatura"))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])
    monkeypatch.setattr(config, "DOMAIN_SOURCES", ("health",))


HEALTH_PLAN = ["blood pressure treatment trial", "walking blood pressure study", "salt blood pressure"]


def test_athena_adds_domain_sources_on_top_of_web_results(monkeypatch, offline_run):
    install(monkeypatch, "pubmed", good)
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": true, "gaps": []}', plan=HEALTH_PLAN))
    final = run_research("blood pressure", max_rounds=1)
    by_api = [f for f in final["findings"] if f.get("source_api") == "pubmed"]
    web = [f for f in final["findings"] if not f.get("source_api")]
    assert web and by_api and all(f["extracted_by"] == "pubmed_text" for f in by_api)  # abstract used, no download
    assert all(f["freshness"] == "live" for f in by_api)


def test_stale_sources_are_marked_for_the_writer_and_in_the_report(monkeypatch, offline_run):
    params = {"limit": config.RESULTS_PER_QUERY}
    for q in HEALTH_PLAN:
        cache.put(router.cache_key("health", q, params), "health", router.normalize_query(q), params, "pubmed",
                  good(q), now=time.time() - cache.ttl_seconds("health") - 60)
    install(monkeypatch, "pubmed", failing(requests.ConnectionError("down")))
    install(monkeypatch, "europe_pmc", failing(requests.ConnectionError("down")))
    prompts: list[str] = []
    writer = lambda user: prompts.append(user) or "A finding on this [S1]."
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": true, "gaps": []}', writer=writer,
                                                    plan=HEALTH_PLAN))
    final = run_research("blood pressure", max_rounds=1)
    stale = [f for f in final["findings"] if f.get("freshness") == "stale"]
    assert stale and any("stale cached result from" in p for p in prompts)
    assert "came from stale cached results" in final["report"]
    assert any("using stale cached results" in e for e in final["errors"])


def test_crossref_records_are_parsed_with_jats_abstracts(monkeypatch):
    _json_get(monkeypatch, {"api.crossref.org": {"message": {"items": [
        {"DOI": "10.1/x", "title": ["Walking and blood pressure"], "published": {"date-parts": [[2019, 5]]},
         "abstract": "<jats:p>Walking lowered pressure.</jats:p>"},
        {"DOI": "10.1/y", "title": ["No abstract here"], "published": {"date-parts": [[2008, 1, 1]]}}]}}})
    a, b = adapters.crossref("walking blood pressure")
    assert a["url"] == "https://doi.org/10.1/x" and a["raw_content"] == "Walking lowered pressure."
    assert a["published"] == "2019-05" and a["api_text"] and b["published"] == "2008-01-01" and not b["api_text"]


def test_pubmed_searches_key_words_and_drops_from_the_front_until_found(monkeypatch):
    assert adapters.pubmed_terms("What is the current evidence on walking and blood pressure?") == ["walking", "blood", "pressure"]
    terms_sent = []

    def fake_get(url, params=None, **kwargs):
        if "esearch" in url:
            terms_sent.append(params["term"])
            ids = ["9"] if params["term"] == "blood pressure" else []
            return http(200, {"esearchresult": {"idlist": ids}})
        resp = http(200)
        resp._content = (b"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>9</PMID><Article>"
                         b"<ArticleTitle>T</ArticleTitle></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>")
        return resp
    monkeypatch.setattr(adapters.requests, "get", fake_get)
    [r] = adapters.pubmed("Does brisk walking lower blood pressure?")
    assert terms_sent == ["brisk walking lower blood pressure", "walking lower blood pressure", "lower blood pressure",
                          "blood pressure"] and r["url"].endswith("/9/")


def test_keys_in_failed_request_urls_never_reach_error_messages(monkeypatch, counted_get):
    def fake_get(url, params=None, **kwargs):
        resp = http(500)
        resp.url = f"https://content.guardianapis.com/search?q=x&api-key=SECRET123&page-size=5"
        return resp
    monkeypatch.setattr(adapters.requests, "get", fake_get)
    monkeypatch.setattr(config, "GUARDIAN_API_KEY", "SECRET123")
    out = router.fetch("news", "climate summit agreement")
    assert out["errors"] and not any("SECRET123" in e for e in out["errors"])
    assert "api-key=REDACTED" in out["errors"][0]
    assert router.redact("x?api_key=abc&key=def&mailto=me@ex.com&q=ok") == \
        "x?api_key=REDACTED&key=REDACTED&mailto=REDACTED&q=ok"


def test_guardian_returns_full_article_text_and_falls_back_without_a_key(monkeypatch):
    _json_get(monkeypatch, {"content.guardianapis.com": {"response": {"results": [
        {"webTitle": "Summit agrees climate fund", "webUrl": "https://www.theguardian.com/a",
         "webPublicationDate": "2026-11-02T10:00:00Z",
         "fields": {"bodyText": "Leaders agreed a climate fund at the summit. " * 10, "trailText": "<p>Fund agreed</p>"}}]}}})
    monkeypatch.setattr(config, "GUARDIAN_API_KEY", "k")
    [r] = adapters.guardian("climate summit agreement")
    assert r["api_text"] and r["published"] == "2026-11-02" and r["snippet"] == "Fund agreed"
    monkeypatch.setattr(config, "GUARDIAN_API_KEY", "")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "k")
    monkeypatch.setattr(adapters.web, "_tavily", lambda q, topic="general": good(q))
    out = router.fetch("news", "climate summit agreement")
    assert out["source"] == "tavily_news" and out["log"]["primary_status"] == "not_configured"


def test_openalex_429_is_not_retried_and_the_key_goes_in_a_header(monkeypatch):
    seen = []

    def fake_get(url, params=None, headers=None, **kwargs):
        seen.append((params, headers))
        resp = http(429)
        resp.headers["Retry-After"] = "34"
        return resp
    monkeypatch.setattr(adapters.requests, "get", fake_get)
    install(monkeypatch, "crossref", good)
    monkeypatch.setattr(config, "OPENALEX_API_KEY", "OA-SECRET")
    out = router.fetch("academic", Q)
    assert len(seen) == 1 and out["source"] == "crossref"  # no 34 s wait: straight to the fallback
    params, headers = seen[0]
    assert headers == {"Authorization": "Bearer OA-SECRET"} and "OA-SECRET" not in str(params)


def test_pubmed_200_with_a_backend_error_is_an_outage_not_no_papers(monkeypatch):
    terms_sent = []

    def fake_get(url, params=None, **kwargs):
        terms_sent.append(params["term"])
        return http(200, {"esearchresult": {"ERROR": "Search Backend failed: An error occurred"}})
    monkeypatch.setattr(adapters.requests, "get", fake_get)
    install(monkeypatch, "europe_pmc", good)
    out = router.fetch("health", "Does brisk walking lower blood pressure?")
    assert out["source"] == "europe_pmc" and out["log"]["fallback_reason"] == "server"
    assert len(terms_sent) == 2 and len(set(terms_sent)) == 1  # one retry, no word-dropping on an outage


def test_ordinary_words_do_not_route_to_tech():
    """Seen live on Render: a climate sub-question with "frameworks" was sent to GitHub and Stack Overflow."""
    assert router.classify("What mechanisms or frameworks (e.g., carbon markets) did the summit agree?") != "tech"
    assert router.classify("Is the public library system underfunded?") != "tech"
    assert router.classify("Which bug species spread crop disease?") != "tech"
    assert router.classify("How do I fix a Python import error?") == "tech"
    assert router.classify("Which open-source SDK handles PDF parsing?") == "tech"
