"""Domain routing with recovery: primary API -> fallback API -> cache -> graceful error.

fetch(domain, query) follows these steps:
  1. Call the domain's primary API (retries on 429 / 5xx / timeouts happen inside the adapter).
  2. Keep only usable, relevant results: tools/quality.relevance (the MIN_RELEVANCE scorer, 0-1 scale)
     against the domain's threshold (config.DOMAIN_RELEVANCE). A result with too little text to judge
     (under MIN_CONTENT_CHARS, e.g. a Serper snippet) is kept: Athena judges its full page later, as before.
  3. Primary failed, timed out, was not configured, returned nothing, or nothing relevant: call the fallback.
  4. Both failed: the cached answer for the same domain + normalised query + parameters, marked
     "cached" or, past its TTL, "stale". Never presented as live.
  5. Nothing usable: a graceful error (success False, no results). Nothing raises.

Every answer has the common shape
  {"success", "source", "domain", "results", "retrieved_at", "is_cached", "is_stale", "error",
   "errors", "log"}
and each result item carries "source", "domain", "freshness" ("live" | "cached" | "stale") and
"retrieved_at". Domains recover independently: each fetch is one domain, and one domain's failure
never touches another's results.

classify(query) picks the domain from the words of the query (deterministic keyword rules).
To add a domain: write its two adapters in adapters.py, add them to ADAPTERS, add a DOMAINS entry
here and its words to _DOMAIN_WORDS, then list it in DOMAIN_SOURCES (.env).
"""

import hashlib
import json
import logging
import re
import time

import requests

from core import config, live, replay
from tools.quality import relevance
from tools.retry_policy import _status_code
from tools.sources import adapters, cache

log = logging.getLogger("apollo.sources")

DOMAINS: dict[str, dict[str, str]] = {
    "general": {"primary": "tavily", "fallback": "serper"},
    # Crossref needs no key; Semantic Scholar mostly answers 429 without one (its adapter stays available).
    "academic": {"primary": "openalex", "fallback": "crossref"},
    "health": {"primary": "pubmed", "fallback": "europe_pmc"},
    "statistics": {"primary": "world_bank", "fallback": "imf"},
    # The Guardian gives full article text; without GUARDIAN_API_KEY it is skipped and Tavily news answers.
    "news": {"primary": "guardian", "fallback": "tavily_news"},
    "tech": {"primary": "github", "fallback": "stack_exchange"},
}

# Checked in this order; the first domain with a matching word wins, else "general".
_DOMAIN_WORDS: list[tuple[str, re.Pattern]] = [
    ("health", re.compile(
        r"\b(health|medical|medicine|clinical|patients?|disease|diseases|symptoms?|treatments?|therap(y|ies)|"
        r"drugs?|vaccines?|cancer|diabetes|blood pressure|hypertension|infections?|virus(es)?|covid|"
        r"mental health|depression|anxiety|obesity|nutrition|diet|dose|dosage|hospital|mortality)\b", re.I)),
    ("tech", re.compile(
        # Only words that mean software: "framework", "library", "code" and "bug" were left out on purpose (seen live:
        # "What mechanisms or frameworks ..." of a climate question was sent to GitHub and Stack Overflow).
        r"\b(python|javascript|typescript|java|rust|golang|c\+\+|programming|source code|coding|software|"
        r"javascript library|python library|api|apis|sdk|github|open[- ]source|compiler|docker|kubernetes|"
        r"react\.?js|sql|devops|llms?|langchain|langgraph|npm|pip install)\b", re.I)),
    ("statistics", re.compile(
        r"\b(gdp|inflation|unemployment|population|poverty|statistics|statistical|per capita|census|"
        r"exports?|imports?|trade deficit|public debt|interest rates?|economy|economic|economies|growth rate|"
        r"income|wages|literacy rate|fiscal)\b", re.I)),
    ("news", re.compile(
        r"\b(news|latest|recent|recently|today|this week|this month|announced|announcement|breaking|"
        r"election|elections|summit|protests?|ceasefire|policy change|20[2-9]\d)\b", re.I)),
    ("academic", re.compile(
        r"\b(stud(y|ies)|research|papers?|journals?|peer[- ]reviewed|meta[- ]analys[ie]s|systematic review|"
        r"experiments?|evidence|theory|theories|scientific|scientists?|academic|literature)\b", re.I)),
]


def classify(query: str) -> str:
    for domain, words in _DOMAIN_WORDS:
        if domain in DOMAINS and words.search(query):
            return domain
    return "general"


def normalize_query(query: str) -> str:
    """Trimmed, lower case, inner whitespace collapsed; every search term kept."""
    return " ".join(query.strip().lower().split())


def cache_key(domain: str, query: str, params: dict | None = None) -> str:
    """SHA-256 of domain + normalised query + search parameters (JSON, sorted keys, so no field can run
    into the next and two orders of the same parameters give one key)."""
    payload = json.dumps({"domain": domain, "query": normalize_query(query), "params": params or {}},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def error_category(exc: BaseException) -> str:
    code = _status_code(exc)
    name = type(exc).__name__
    if code in (401, 403) or name in {"ConfigError", "InvalidAPIKeyError", "MissingAPIKeyError", "ForbiddenError",
                                      "AuthenticationError", "PermissionDeniedError"}:
        return "auth"
    if code == 429 or name == "UsageLimitExceededError":
        return "rate_limit"
    if code is not None and code >= 500:
        return "server"
    if code is not None and 400 <= code < 500:
        return "bad_request"
    if isinstance(exc, (requests.Timeout, TimeoutError)) or "timeout" in name.lower():
        return "timeout"
    if isinstance(exc, (requests.ConnectionError, ConnectionError, OSError)):
        return "network"
    return "error"


# A failed request's message carries its URL, and some APIs take their key in the URL (api-key=, api_key=, key=).
# Error messages reach the UI and the saved run, so every such value is removed first.
_SECRET_PARAM = re.compile(r"(?i)\b((?:api[-_]?key|key|token|access_token|mailto)=)[^&\s'\"]+")


def redact(message: str) -> str:
    return _SECRET_PARAM.sub(r"\1REDACTED", message)


def _usable(results: list[dict], query: str, threshold: float) -> list[dict]:
    kept = []
    for r in results:
        if not r.get("url"):
            continue
        text = " ".join(str(r.get(k) or "") for k in ("title", "snippet", "raw_content"))
        if len(text) < config.MIN_CONTENT_CHARS or relevance(text, query) >= threshold:
            kept.append(r)
    return kept


def _attempt(name: str, role: str, query: str, threshold: float) -> tuple[list[dict], str, str]:
    """One API: (usable results, status, error message). status "ok" or an error category."""
    adapter = adapters.ADAPTERS[name]
    if not replay.active() and adapter.key is not None and not adapter.key():
        return [], "not_configured", f"search[{name}] skipped for {query!r}: API key not set"
    try:
        raw = replay.source(name, role, query) if replay.active() else adapter.fn(query)
    except Exception as exc:  # retries already happened inside the adapter
        category = error_category(exc)
        return [], category, (f"search[{name}] failed for {query!r}: {category}: {type(exc).__name__}: "
                              f"{redact(str(exc))[:200]}")
    if not raw:
        return [], "empty", f"search[{name}] returned no results for {query!r}"
    kept = _usable(raw, query, threshold)
    if not kept:
        return [], "irrelevant", (f"search[{name}] returned no relevant results for {query!r} "
                                  f"(all {len(raw)} below relevance {threshold:.2f})")
    return kept, "ok", ""


def _stamp(results: list[dict], source: str, domain: str, freshness: str, retrieved_at: int) -> list[dict]:
    return [{**r, "source": source, "domain": domain, "freshness": freshness, "retrieved_at": retrieved_at}
            for r in results]


def fetch(domain: str, query: str, params: dict | None = None) -> dict:
    """Results for `query` from `domain`'s APIs, recovering from failures (see the module docstring)."""
    started = time.monotonic()
    spec = DOMAINS[domain]
    params = {"limit": config.RESULTS_PER_QUERY, **(params or {})}
    key = cache_key(domain, query, params)
    threshold = config.DOMAIN_RELEVANCE.get(domain, config.MIN_RELEVANCE)
    record = {"domain": domain, "query_id": key[:12], "primary": spec["primary"], "primary_status": "",
              "fallback": spec["fallback"], "fallback_reason": "", "fallback_status": "", "cache": "not_used",
              "stale": False, "final_source": "", "error_category": "", "ms": 0}
    errors: list[str] = []
    answer = {"success": False, "source": "", "domain": domain, "results": [], "retrieved_at": "",
              "is_cached": False, "is_stale": False, "error": None, "errors": errors, "log": record}

    for role in ("primary", "fallback"):
        name = spec[role]
        results, status, message = _attempt(name, role, query, threshold)
        record[f"{role}_status"] = status
        if status == "ok":
            now = int(time.time())
            if config.SOURCE_CACHE_ENABLED:
                cache.put(key, domain, normalize_query(query), params, name, results, now)
            answer.update(success=True, source=name, results=_stamp(results, name, domain, "live", now),
                          retrieved_at=now)
            record["final_source"] = name
            break
        errors.append(message)
        if role == "primary":
            record["fallback_reason"] = status
    else:
        hit = cache.get(key) if config.SOURCE_CACHE_ENABLED else None
        record["cache"] = "hit" if hit else "miss"
        if hit and hit["domain"] == domain and hit["query"] == normalize_query(query) and hit["results"]:
            freshness = "stale" if hit["is_stale"] else "cached"
            answer.update(success=True, source=hit["source"], is_cached=True, is_stale=hit["is_stale"],
                          retrieved_at=hit["retrieved_at"],
                          results=_stamp(hit["results"], hit["source"], domain, freshness, hit["retrieved_at"]))
            record.update(final_source=f"cache:{hit['source']}", stale=hit["is_stale"])
            when = time.strftime("%Y-%m-%d", time.gmtime(hit["retrieved_at"]))
            errors.append(f"search[{domain}] live APIs failed for {query!r}; using {freshness} cached results "
                          f"from {hit['source']} retrieved {when}")
        else:
            record["error_category"] = record["fallback_status"] or record["primary_status"]
            answer["error"] = (f"{domain}: {spec['primary']} {record['primary_status']}, "
                               f"{spec['fallback']} {record['fallback_status']}, no cached results")

    record["ms"] = round((time.monotonic() - started) * 1000)
    log.info("source_fetch %s", json.dumps(record, sort_keys=True))  # ids and statuses only: no query text, no keys
    return answer


def search_domain(domain: str, query: str, errors: list[str]) -> list[dict]:
    """Athena's entry point: the results of fetch(), its messages appended to `errors`. Raises only
    live.RunStopped, when the user stopped the run (no further searches for it)."""
    live.raise_if_stopped()
    if domain not in DOMAINS:
        return []
    answer = fetch(domain, query)
    errors.extend(answer["errors"])
    return answer["results"]
