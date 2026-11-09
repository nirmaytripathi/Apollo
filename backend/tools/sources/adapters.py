"""One function per source API. Each returns Athena's search-result shape:

    {"title", "url", "snippet", "raw_content", "published", "api_text"}

`raw_content` is text the API itself returned (an abstract for papers); `api_text` = True means it is
the source's own text, so Athena uses it as the page content instead of downloading the landing page
(publisher pages are often paywalled or blocked). Calls are retried on transient errors only
(429, 5xx, timeouts, connection errors) by tools/retry_policy.py; 400/401/403 fail at once.
An adapter raises on failure; the router (router.py) turns failures into fallbacks and errors.

Every API here is free; the optional keys only raise rate limits.
"""

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

import requests

from core import config
from tools import search as web
from tools.retry_policy import with_retries

_retry = with_retries(config.SEARCH_MAX_RETRIES)
_TAG = re.compile(r"<[^>]+>")


def _text(value: str) -> str:
    """Plain text: tags removed (Europe PMC abstracts carry <h4>, <i>), entities decoded (&amp;), spaces collapsed."""
    return " ".join(html.unescape(_TAG.sub(" ", value or "")).split())


def _get(url: str, headers: dict | None = None, timeout: float | None = None, **params) -> requests.Response:
    resp = requests.get(url, params=params, headers=headers, timeout=timeout or config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp


def _result(title: str, url: str, abstract: str, published: str) -> dict:
    abstract = _text(abstract)
    return {"title": _text(title), "url": url, "snippet": abstract[:300], "raw_content": abstract,
            "published": published or "", "api_text": bool(abstract)}


# ---------------------------------------------------------------- academic


def abstract_from_inverted_index(index: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; put the words back in order."""
    if not index:
        return ""
    slots: dict[int, str] = {}
    for word, positions in index.items():
        for p in positions:
            slots[p] = word
    return " ".join(slots[p] for p in sorted(slots))


# One attempt, no retries: OpenAlex's 429 asks to wait ~30 s (measured: 34 s), and honouring that with retries
# cost minutes per run. Failing fast hands the query to Crossref at once; with OPENALEX_API_KEY 429s are rare.
@with_retries(0)
def openalex(query: str) -> list[dict]:
    params = {"search": query, "per_page": config.RESULTS_PER_QUERY}
    if config.OPENALEX_MAILTO:
        params["mailto"] = config.OPENALEX_MAILTO
    headers = {"Authorization": f"Bearer {config.OPENALEX_API_KEY}"} if config.OPENALEX_API_KEY else None
    works = _get("https://api.openalex.org/works", headers=headers, **params).json().get("results", [])
    out = []
    for w in works:
        url = w.get("doi") or (w.get("primary_location") or {}).get("landing_page_url") or w.get("id") or ""
        if url:
            out.append(_result(w.get("display_name") or w.get("title") or "", url,
                               abstract_from_inverted_index(w.get("abstract_inverted_index")),
                               w.get("publication_date") or ""))
    return out


@_retry
def semantic_scholar(query: str) -> list[dict]:
    headers = {"x-api-key": config.SEMANTIC_SCHOLAR_API_KEY} if config.SEMANTIC_SCHOLAR_API_KEY else None
    papers = _get("https://api.semanticscholar.org/graph/v1/paper/search", query=query,
                  limit=config.RESULTS_PER_QUERY, fields="title,abstract,url,publicationDate,year,externalIds",
                  headers=headers).json().get("data", []) or []
    out = []
    for p in papers:
        doi = (p.get("externalIds") or {}).get("DOI")
        url = f"https://doi.org/{doi}" if doi else p.get("url") or ""
        if url:
            out.append(_result(p.get("title") or "", url, p.get("abstract") or "",
                               p.get("publicationDate") or str(p.get("year") or "")))
    return out


@_retry
def crossref(query: str) -> list[dict]:
    """Crossref, the DOI registry: no key needed. Abstracts (JATS XML) come with only some records; for the
    rest Athena reads the landing page. OPENALEX_MAILTO, when set, also joins Crossref's faster polite pool."""
    params = {"query": query, "rows": config.RESULTS_PER_QUERY, "select": "DOI,title,abstract,published,URL"}
    if config.OPENALEX_MAILTO:
        params["mailto"] = config.OPENALEX_MAILTO
    items = _get("https://api.crossref.org/works", **params).json().get("message", {}).get("items", [])
    out = []
    for it in items:
        doi = it.get("DOI")
        url = f"https://doi.org/{doi}" if doi else it.get("URL") or ""
        parts = ((it.get("published") or {}).get("date-parts") or [[]])[0]
        published = "-".join(str(p).zfill(2) for p in parts if p)
        if url:
            out.append(_result((it.get("title") or [""])[0], url, it.get("abstract") or "", published))
    return out


# ---------------------------------------------------------------- health

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def parse_pubmed_xml(xml: str) -> list[dict]:
    """PubMed efetch XML -> results (title, abstract sections joined, publication date)."""
    out = []
    for art in ET.fromstring(xml).iter("PubmedArticle"):
        pmid = art.findtext(".//MedlineCitation/PMID") or ""
        node = art.find(".//ArticleTitle")
        title = "".join(node.itertext()) if node is not None else ""
        parts = []
        for node in art.iterfind(".//Abstract/AbstractText"):
            label, text = node.get("Label"), "".join(node.itertext())
            parts.append(f"{label}: {text}" if label else text)
        date = art.find(".//Article/ArticleDate")
        if date is None:
            date = art.find(".//JournalIssue/PubDate")
        ymd = [date.findtext(k) for k in ("Year", "Month", "Day")] if date is not None else []
        published = "-".join(v.zfill(2) for v in ymd if v and v.isdigit())
        if pmid:
            out.append(_result(title, f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", " ".join(parts), published))
    return out


# Words that say how a question is asked, not what it is about: PubMed would require them in every paper.
_GENERIC = set("""current scientific evidence effect effects regular compare compared comparison forms form influence
impact impacts outcome outcomes specific populations population benefit benefits groups group known exist exists
limits role whether factors associated association data studies study research recent latest overall""".split())
PUBMED_MIN_TERMS = 2


def pubmed_terms(query: str) -> list[str]:
    """The query's key words in order, minus generic research words (the relevance check's own word rules)."""
    from tools.quality import _WORD, key_words

    keep = key_words(query) - _GENERIC
    return list(dict.fromkeys(w for w in _WORD.findall(query.lower()) if w in keep)) or [query]


PUBMED_TIMEOUT_SECONDS = 10


class PubMedBackendError(Exception):
    """PubMed answered 200 but its search backend failed: treated like a 502 (retried once, then the fallback)."""

    status_code = 502


# One retry and a 10 s timeout (not the usual 2 retries, 15 s): measured 2026-11-05, PubMed's search answered 500
# to most requests for a while, and full retries cost 15-25 s per query before Europe PMC took over.
@with_retries(1)
def pubmed(query: str) -> list[dict]:
    """PubMed requires every term in a record, so a whole question finds nothing. Its key words are ANDed;
    when that finds nothing, words are dropped from the front (a question's topic usually comes last)."""
    key = {"api_key": config.NCBI_API_KEY} if config.NCBI_API_KEY else {}
    terms, ids = pubmed_terms(query), []
    while not ids:
        found = _get(f"{EUTILS}/esearch.fcgi", timeout=PUBMED_TIMEOUT_SECONDS, db="pubmed", term=" ".join(terms),
                     sort="relevance", retmax=config.RESULTS_PER_QUERY, retmode="json", **key).json().get("esearchresult", {})
        if "ERROR" in found:  # seen live: HTTP 200 with "Search Backend failed": an outage, not "no papers"
            raise PubMedBackendError(f"PubMed search backend failed: {str(found['ERROR'])[:120]}")
        ids = found.get("idlist", [])
        if ids or len(terms) <= PUBMED_MIN_TERMS:
            break
        terms = terms[1:]
    if not ids:
        return []
    return parse_pubmed_xml(_get(f"{EUTILS}/efetch.fcgi", db="pubmed", id=",".join(ids), retmode="xml", **key).text)


@_retry
def europe_pmc(query: str) -> list[dict]:
    hits = (_get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", query=query, format="json",
                 pageSize=config.RESULTS_PER_QUERY, resultType="core")
            .json().get("resultList", {}).get("result", []))
    out = []
    for h in hits:
        url = (f"https://doi.org/{h['doi']}" if h.get("doi")
               else f"https://europepmc.org/article/{h.get('source', 'MED')}/{h.get('id', '')}" if h.get("id") else "")
        if url:
            out.append(_result(h.get("title") or "", url, h.get("abstractText") or "",
                               h.get("firstPublicationDate") or str(h.get("pubYear") or "")))
    return out


# ---------------------------------------------------------------- statistics


def _date(value) -> str:
    """"20261103T120000Z", "2026-11-03T..." or epoch seconds -> "2026-11-03"; "" when unknown."""
    if isinstance(value, (int, float)) and value > 0:
        return time.strftime("%Y-%m-%d", time.gmtime(value))
    m = re.match(r"((?:19|20)\d{2})-?(\d{2})-?(\d{2})", str(value or ""))
    return "-".join(m.groups()) if m else ""


@_retry
def world_bank(query: str) -> list[dict]:
    """World Bank Documents & Reports: reports, working papers and data notes, with their abstracts."""
    docs = _get("https://search.worldbank.org/api/v3/wds", format="json", qterm=query, rows=config.RESULTS_PER_QUERY,
                fl="display_title,abstracts,docdt,url,pdfurl").json().get("documents", {})
    out = []
    for key, d in docs.items():
        if not isinstance(d, dict) or key == "facets":
            continue
        abstract = d.get("abstracts")
        abstract = abstract.get("cdata!", "") if isinstance(abstract, dict) else str(abstract or "")
        url = d.get("url") or d.get("pdfurl") or ""
        if url:
            out.append(_result(d.get("display_title") or "", url, abstract, _date(d.get("docdt"))))
    return out


IMF = "https://www.imf.org/external/datamapper/api/v1"
IMF_INDICATORS_TTL = 3600  # the indicator list (hundreds of entries) changes rarely: kept in memory an hour
_imf_lock = threading.Lock()
_imf_indicators: dict = {"at": 0.0, "items": {}}  # guarded by _imf_lock


def _imf_indicator_list() -> dict:
    with _imf_lock:
        if _imf_indicators["items"] and time.monotonic() - _imf_indicators["at"] < IMF_INDICATORS_TTL:
            return _imf_indicators["items"]
    items = _get(f"{IMF}/indicators").json().get("indicators", {})  # no lock held during the request
    with _imf_lock:
        _imf_indicators.update(at=time.monotonic(), items=items)
    return items


@_retry
def imf(query: str) -> list[dict]:
    """IMF DataMapper: the indicators whose name and description share most words with the query, each with
    its definition and the world series for recent years, written out as text that a quote can be checked in.
    Only as many series are fetched as Athena keeps per query (DOMAIN_PAGES_PER_QUERY), plus one spare."""
    from tools.quality import key_words  # the same key words the relevance check uses

    words = key_words(query)
    indicators = _imf_indicator_list()
    scored = []
    for code, ind in indicators.items():
        if not code or not isinstance(ind, dict):
            continue
        text = f"{ind.get('label', '')} {ind.get('description', '')}".lower()
        hits = sum(w in text for w in words)
        if hits:
            scored.append((hits, code, ind))
    out = []
    for _, code, ind in sorted(scored, key=lambda s: -s[0])[: config.DOMAIN_PAGES_PER_QUERY + 1]:
        series = (_get(f"{IMF}/{code}/WEOWORLD").json().get("values", {}).get(code, {}).get("WEOWORLD", {}))
        # The IMF series runs years ahead: the current year and later are IMF estimates and projections, said so.
        this_year = time.gmtime().tm_year
        years = [y for y in sorted(series) if y.isdigit() and int(y) <= this_year + 1][-6:]
        values = "; ".join(f"{y}: {series[y]}" + (" (IMF projection)" if int(y) >= this_year else "") for y in years)
        text = (f"{ind.get('label', code)} ({ind.get('unit', '')}). {ind.get('description', '')} "
                + (f"World values from the IMF DataMapper ({ind.get('source', 'IMF')}): {values}." if values else ""))
        past = [y for y in years if int(y) < this_year]
        out.append(_result(f"IMF DataMapper: {ind.get('label', code)}",
                           f"https://www.imf.org/external/datamapper/{code}", text, past[-1] if past else ""))
    return out


# ---------------------------------------------------------------- news


GDELT_TIMEOUT_SECONDS = 8


# One attempt, no retries: GDELT allows about one request per 5 s and its 429s do not clear within a quick
# retry (measured: 46 s through retries). Failing fast hands the query to the fallback (Tavily news) at once.
@with_retries(0)
def gdelt(query: str) -> list[dict]:
    """GDELT DOC 2.0: recent news articles (titles and links; Athena reads the article pages)."""
    resp = _get("https://api.gdeltproject.org/api/v2/doc/doc", timeout=GDELT_TIMEOUT_SECONDS, query=query,
                mode="ArtList", format="json",
                maxrecords=config.RESULTS_PER_QUERY, sort="HybridRel")
    try:
        articles = resp.json().get("articles", [])
    except ValueError:  # GDELT answers plain text for queries it rejects (too short, bad syntax)
        raise ValueError(f"GDELT did not return JSON: {resp.text[:120]}") from None
    return [{"title": a.get("title", ""), "url": a["url"], "snippet": a.get("title", ""), "raw_content": "",
             "published": _date(a.get("seendate")), "api_text": False} for a in articles if a.get("url")]


@_retry
def guardian(query: str) -> list[dict]:
    """The Guardian Open Platform: articles with their full body text, so claims are checked against the
    article itself. Its search requires every word, so it searches key words like PubMed, dropping words
    from the front until something is found. Needs GUARDIAN_API_KEY (free, non-commercial developer key)."""
    terms, items = pubmed_terms(query), []
    while not items:
        items = (_get("https://content.guardianapis.com/search", q=" ".join(terms), **{"api-key": config.GUARDIAN_API_KEY},
                      **{"page-size": config.RESULTS_PER_QUERY, "order-by": "relevance", "show-fields": "bodyText,trailText"})
                 .json().get("response", {}).get("results", []))
        if items or len(terms) <= PUBMED_MIN_TERMS:
            break
        terms = terms[1:]
    out = []
    for it in items:
        fields = it.get("fields") or {}
        if it.get("webUrl"):
            result = _result(it.get("webTitle") or "", it["webUrl"], fields.get("bodyText") or "",
                             _date(it.get("webPublicationDate")))
            result["snippet"] = _text(fields.get("trailText") or "") or result["snippet"]
            out.append(result)
    return out


def tavily_news(query: str) -> list[dict]:
    return web._tavily(query, topic="news")


# ---------------------------------------------------------------- tech


@_retry
def github(query: str) -> list[dict]:
    """GitHub repository search (Athena reads each repository page, README included)."""
    headers = {"Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    repos = _get("https://api.github.com/search/repositories", headers=headers, q=query,
                 per_page=config.RESULTS_PER_QUERY).json().get("items", [])
    return [{"title": r.get("full_name", ""), "url": r["html_url"],
             "snippet": f"{r.get('description') or ''} ({r.get('stargazers_count', 0)} stars, {r.get('language') or 'n/a'})",
             "raw_content": "", "published": _date(r.get("created_at")), "api_text": False}
            for r in repos if r.get("html_url")]


@_retry
def stack_exchange(query: str) -> list[dict]:
    """Stack Overflow questions (Athena reads each page, answers included)."""
    params = {"order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow",
              "pagesize": config.RESULTS_PER_QUERY, "filter": "withbody"}
    if config.STACKEXCHANGE_KEY:
        params["key"] = config.STACKEXCHANGE_KEY
    items = _get("https://api.stackexchange.com/2.3/search/advanced", **params).json().get("items", [])
    return [{"title": _text(i.get("title", "")), "url": i["link"], "snippet": _text(i.get("body", ""))[:300],
             "raw_content": _text(i.get("body", "")), "published": _date(i.get("creation_date")), "api_text": False}
            for i in items if i.get("link")]


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class Adapter:
    fn: Callable[[str], list[dict]]
    key: Callable[[], str] | None = None  # None = no key needed; else the key, "" when not configured


ADAPTERS: dict[str, Adapter] = {
    # Looked up on each call (not bound at import), so the web providers stay replaceable in tools/search.py.
    "tavily": Adapter(lambda q: web._tavily(q), lambda: config.TAVILY_API_KEY),
    "serper": Adapter(lambda q: web._serper(q), lambda: config.SERPER_API_KEY),
    "openalex": Adapter(openalex),
    "semantic_scholar": Adapter(semantic_scholar),  # kept for anyone with a key; not in DOMAINS by default
    "crossref": Adapter(crossref),
    "pubmed": Adapter(pubmed),
    "europe_pmc": Adapter(europe_pmc),
    "world_bank": Adapter(world_bank),
    "imf": Adapter(imf),
    "gdelt": Adapter(gdelt),  # kept, not in DOMAINS: it answered 429 to most requests (about 1 allowed per 5 s)
    "guardian": Adapter(guardian, lambda: config.GUARDIAN_API_KEY),
    "tavily_news": Adapter(tavily_news, lambda: config.TAVILY_API_KEY),
    "github": Adapter(github),
    "stack_exchange": Adapter(stack_exchange),
}
