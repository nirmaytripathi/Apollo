"""Research-quality rules that code (not the LLM) applies to sources and to the finished run.

- source_kind(url): "primary" (original research or an official publication) or "secondary"
  (news, blogs, explainers, company pages). A RULE-BASED GUESS from the web address, used to put
  primary sources first; it is not a credibility score.
- same_content(a, b): two pages carry the same text (one article at two URLs), by word 5-gram overlap.
- relevance(text, question): share of the sub-question's key words a page contains; pages below
  MIN_RELEVANCE (e.g. dictionary entries for one word of the question) are dropped.
- published_date(html): the page's own publication date from its metadata, or "" (never guessed).
- quality_checks(state): the checks run before a report is marked complete. Each is pass / warn /
  fail with a plain-language detail; nothing is hidden or rounded up.
"""

import re

from core import config
from tools.text import CITE, find_citations

# ---------------------------------------------------------------- source kind

# Hosts and paths that publish original research or official primary material.
_PRIMARY = re.compile(
    r"(^|\.)("
    r"doi\.org|ncbi\.nlm\.nih\.gov|pubmed|europepmc\.org|arxiv\.org|biorxiv\.org|medrxiv\.org|ssrn\.com|"
    r"nature\.com|science\.org|sciencedirect\.com|link\.springer\.com|springer\.com|onlinelibrary\.wiley\.com|"
    r"tandfonline\.com|jamanetwork\.com|nejm\.org|thelancet\.com|bmj\.com|cell\.com|plos\.org|frontiersin\.org|"
    r"mdpi\.com|ieeexplore\.ieee\.org|dl\.acm\.org|aclanthology\.org|openreview\.net|cochranelibrary\.com|"
    r"academic\.oup\.com|journals\.sagepub\.com|cambridge\.org|jstor\.org|semanticscholar\.org|"
    r"who\.int|un\.org|worldbank\.org|imf\.org|oecd\.org|europa\.eu|"
    r"[a-z0-9-]+\.gov|gov\.[a-z]{2}|[a-z0-9-]+\.gov\.[a-z]{2}|nic\.in|[a-z0-9-]+\.nic\.in|"
    r"bls\.gov|census\.gov|data\.[a-z.]+"
    r")$"
)
_PRIMARY_PATH = re.compile(r"/(doi|abs|pdf|article|articles|science/article|content/\d)/", re.I)


def host_of(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/:?#]+)", url.strip().lower())
    host = m.group(1) if m else ""
    return host[4:] if host.startswith("www.") else host


def source_kind(url: str) -> str:
    host = host_of(url)
    if _PRIMARY.search(host):
        return "primary"
    if host.endswith((".edu", ".ac.in", ".ac.uk", ".edu.au")) and _PRIMARY_PATH.search(url):
        return "primary"  # a university's own papers, not its news pages
    return "secondary"


# ---------------------------------------------------------------- duplicate content

_WORD = re.compile(r"[a-z0-9]+")


def _shingles(text: str, n: int = 5) -> set[int]:
    words = _WORD.findall(text.lower()[:4000])
    return {hash(" ".join(words[i : i + n])) for i in range(max(0, len(words) - n + 1))}


def same_content(a: str, b: str) -> bool:
    """True when two page texts are essentially the same article (word 5-gram overlap)."""
    sa, sb = _shingles(a), _shingles(b)
    if len(sa) < 20 or len(sb) < 20:
        return False  # too little text to tell
    return len(sa & sb) / min(len(sa), len(sb)) >= config.DUPLICATE_OVERLAP


# ---------------------------------------------------------------- relevance

# Question words that say nothing about the topic ("what", "have", "does" ...), left out of relevance.
_STOP = set("""what which when where while have does with their there about been from that this into were they than
more most such also other among through these those would could should them then will your some many much over
under between being each only very""".split())


def key_words(question: str) -> set[str]:
    return {w for w in _WORD.findall(question.lower()) if len(w) > 3 and w not in _STOP}


def relevance(text: str, question: str) -> float:
    """Share of the sub-question's key words that the page text contains (0-1).

    Measured on a real run: dictionary pages returned for a long sub-question scored 0.08-0.23,
    every on-topic page 0.38 or more; MIN_RELEVANCE sits between."""
    words = key_words(question)
    if not words:
        return 1.0
    return len(words & set(_WORD.findall(text.lower()))) / len(words)


# ---------------------------------------------------------------- publication date

_META_DATE = re.compile(
    r"<meta[^>]+(?:property|name|itemprop)\s*=\s*[\"']("
    r"article:published_time|datepublished|citation_publication_date|citation_date|citation_online_date|"
    r"dc\.date|dc\.date\.issued|dcterms\.issued|pubdate|publish_date|publication_date|og:published_time"
    r")[\"'][^>]*>",
    re.I,
)
_CONTENT = re.compile(r"content\s*=\s*[\"']([^\"']+)[\"']", re.I)
_JSONLD_DATE = re.compile(r"\"datePublished\"\s*:\s*\"([^\"]+)\"")
_YMD = re.compile(r"((?:19|20)\d{2})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?")


def published_iso(value: str) -> str:
    """A date string from metadata or an API as YYYY, YYYY-MM or YYYY-MM-DD; "" when none is found."""
    m = _YMD.search(value)
    if not m:
        return ""
    year, month, day = m.group(1), m.group(2), m.group(3)
    if month and not 1 <= int(month) <= 12:
        return year
    if day and not 1 <= int(day) <= 31:
        day = None
    return "-".join([year] + [f"{int(x):02d}" for x in (month, day) if x])


def published_date(html: str) -> str:
    """The page's stated publication date (YYYY, YYYY-MM or YYYY-MM-DD), or "" when it states none."""
    for tag in _META_DATE.finditer(html):
        content = _CONTENT.search(tag.group(0))
        if content and (date := published_iso(content.group(1))):
            return date
    m = _JSONLD_DATE.search(html)
    return published_iso(m.group(1)) if m else ""


# ---------------------------------------------------------------- checks before completion


def _sentence(text: str) -> str:
    """First letter upper case (unlike str.capitalize, ids such as "C2" keep their case), ending in a full stop."""
    return text[:1].upper() + text[1:] + "."


def _check(cid: str, label: str, status: str, detail: str) -> dict:
    return {"id": cid, "label": label, "status": status, "detail": detail}


def usable_per_section(state: dict) -> list[int]:
    """Sources with readable page text collected for each sub-question."""
    counts = [0] * len(state["plan"])
    for f in state["findings"]:
        if f.get("content") and 0 <= f["section"] < len(counts):
            counts[f["section"]] += 1
    return counts


def quality_checks(state: dict) -> list[dict]:
    findings, claims, plan = state["findings"], state.get("claims", []), state["plan"]
    by_id = {f["id"]: f for f in findings}
    checks = []

    # 1. Coverage: enough readable sources per sub-question.
    target = config.MIN_SOURCES_PER_QUESTION
    thin = [(i, n) for i, n in enumerate(usable_per_section(state)) if n < target]
    checks.append(_check(
        "coverage", f"At least {target} readable sources per sub-question",
        "warn" if thin else "pass",
        "; ".join(f"sub-question {i + 1} has {n}" for i, n in thin) + ". Its answer may be incomplete."
        if thin else f"Every sub-question has at least {target}.",
    ))

    # 2. Search results: say clearly when a search returned nothing or a section found nothing.
    sections = state.get("sections", [])
    not_found = sorted(s["index"] + 1 for s in sections if not find_citations(s["text"]))  # no cited answer at all
    partly = sorted(s["index"] + 1 for s in sections
                    if find_citations(s["text"]) and "not found in sources" in s["text"].lower())
    # A query counts as failed only when every provider came back empty (Athena logs that once per query).
    failed = [e for e in state["events"] if e.startswith("[Athena] No results for")]
    problems = []
    if not_found:
        problems.append(f"no answer found in the sources for sub-question(s) {', '.join(map(str, not_found))}")
    if partly:
        problems.append(f"parts of sub-question(s) {', '.join(map(str, partly))} were not found in the sources")
    if failed:
        problems.append(f"{len(failed)} search(es) failed or returned nothing")
    checks.append(_check("results", "Search results were sufficient", "warn" if problems else "pass",
                         _sentence("; ".join(problems)) if problems else "Every sub-question got sources."))

    # 3. Original research first.
    cited = set(find_citations(state["report"]))
    primary_cited = [i for i in cited if by_id.get(i, {}).get("kind") == "primary"]
    primary_found = [f for f in findings if f.get("kind") == "primary"]
    checks.append(_check(
        "primary", "Original research or official sources used", "pass" if primary_cited else "warn",
        f"{len(primary_cited)} of {len(cited)} cited sources are original research or official publications "
        f"(rule-based, from the web address)."
        if primary_cited else
        ("None of the cited sources is original research or an official publication; the report rests on "
         "secondary articles." + (f" {len(primary_found)} such source(s) were found but not cited." if primary_found else "")),
    ))

    # 4. Every citation points at a retrieved source.
    unknown = sorted({i for i in CITE.findall(state["report"]) if i not in by_id})
    checks.append(_check("citations", "Every citation maps to a retrieved source", "fail" if unknown else "pass",
                         f"Unknown ids: {', '.join(unknown)}." if unknown else f"{len(cited)} sources cited, all retrieved."))

    # 5. Verified means quoted: no claim is "verified" unless every part has a quote found in its source.
    if claims and any("parts" in c for c in claims):
        bad = [n for n, c in enumerate(claims, 1) if c["status"] == "verified"
               and not (c.get("parts") and all(p["verdict"] == "supported" and p["quote"] for p in c["parts"]))]
        checks.append(_check("quotes", "Verified claims have an exact quote from their source", "fail" if bad else "pass",
                             f"Claims {bad} are marked verified without a checked quote." if bad else
                             "Every quote was found in the cited source's text."))

    # 6. Unsupported claims are named, never labelled verified.
    weak = [(n, c["status"]) for n, c in enumerate(claims, 1) if c["status"] in ("partial", "not_found", "unchecked")]
    checks.append(_check("unsupported", "Claims not fully supported are flagged", "warn" if weak else "pass",
                         ", ".join(f"C{n} {s.replace('_', ' ')}" for n, s in weak) + "." if weak else
                         "Every checked claim is fully supported."))

    # 7. Contradictory evidence is shown, not hidden.
    contra_claims = [n for n, c in enumerate(claims, 1) if c["status"] == "contradicted"]
    pairs = state["contradictions"]
    parts = []
    if pairs:
        parts.append(f"{len(pairs)} conflict(s) between sources")
    if contra_claims:
        parts.append(f"claim(s) {', '.join(f'C{n}' for n in contra_claims)} contradicted by a cited source")
    checks.append(_check("contradictions", "Contradictory evidence is reported", "warn" if parts else "pass",
                         _sentence("; ".join(parts)) + " Listed below." if parts else
                         "No contradictions were found."))

    # 8. Duplicates removed.
    dups, off = state.get("duplicates", []), state.get("irrelevant", [])
    removed = [f"{len(dups)} duplicate page(s)"] if dups else []
    removed += [f"{len(off)} page(s) not about the sub-question"] if off else []
    checks.append(_check("duplicates", "Duplicate and off-topic sources removed", "pass",
                         _sentence(" and ".join(removed) + " removed") if removed else "No duplicate or off-topic pages found."))

    # 9. Dates: publication date (from the page) is kept apart from the research date (retrieval).
    dated = [f for f in findings if f.get("published")]
    checks.append(_check(
        "dates", "Publication date recorded separately from research date",
        "pass" if findings and len(dated) == len(findings) else "warn",
        f"{len(dated)} of {len(findings)} sources state a publication date; the others are marked unknown. "
        "Every source keeps the date Apollo retrieved it.",
    ))
    return checks


def overall(checks: list[dict]) -> str:
    statuses = {c["status"] for c in checks}
    return "failed" if "fail" in statuses else "passed_with_warnings" if "warn" in statuses else "passed"


def limits_section(state: dict, checks: list[dict]) -> str:
    """Plain-language section appended to the report: what the evidence cannot support, and conflicts."""
    lines = ["## Evidence limits and quality checks", ""]
    for c in checks:
        mark = {"pass": "Passed", "warn": "Warning", "fail": "Failed"}[c["status"]]
        lines.append(f"- {mark}: {c['label']}. {c['detail']}")
    by_id = {f["id"]: f for f in state["findings"]}
    for freshness, words in (("stale", "stale cached results (past their freshness period; may be out of date)"),
                             ("cached", "cached results")):
        ids = [f["id"] for f in state["findings"] if f.get("freshness") == freshness]
        if ids:
            lines.append(f"- Note: live search failed for some queries, so {', '.join(ids)} came from {words}, "
                         "not from a live search.")
    if state["contradictions"]:
        lines += ["", "Conflicting evidence:"]
        for x in state["contradictions"]:
            a, b = by_id.get(x["source_a"], {}), by_id.get(x["source_b"], {})
            lines.append(f"- Sources {x['source_a']} ({a.get('title', '')[:60]}) and {x['source_b']} "
                         f"({b.get('title', '')[:60]}) disagree: {x['claim']}")
    for n, c in enumerate(state.get("claims", []), 1):
        if c["status"] == "contradicted":
            quotes = [f"{p['source_id']}: \"{p['quote']}\"" for p in c.get("parts", []) if p["verdict"] == "contradicted"]
            lines.append(f"- Claim C{n} is contradicted by its source ({'; '.join(quotes)}).")
    return "\n".join(lines)
