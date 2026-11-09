"""Page text extraction for Athena.

page_content(url, raw_content, errors) -> (text, extracted_by)

1. extract_text(url): our own fetch + trafilatura (main article text only).
   - Checks Content-Type first and skips non-HTML (PDFs etc.) without downloading
     the body, since trafilatura only parses HTML.
   - Streams the body with a TOTAL deadline and byte cap. (requests' `timeout`
     only limits the gap between packets, so a big file could otherwise take
     far longer than REQUEST_TIMEOUT_SECONDS.)
   - Sends an honest bot User-Agent. Sites that refuse it (403) are not worked around.
2. If that yields fewer than MIN_CONTENT_CHARS, fall back to `raw_content`: the
   page text the search provider (Tavily) already retrieved. This is still
   retrieved source text, never LLM output.

Text is capped at EXTRACT_MAX_CHARS. Failures return "" and are logged to
`errors`; the finding is still kept with its search snippet.

page_date(url) -> the page's own publication date from its metadata, or "" (see below).
"""

import re
import time

import requests
import trafilatura

from core import config, replay
from tools.quality import published_date

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ApolloResearchBot/0.1)"}
_HTML_TYPES = ("text/html", "application/xhtml+xml")
MENU_LINE_CHARS = 40  # shorter lines without sentence punctuation are treated as menu items
HEAD_BYTES = 65536    # publication-date metadata sits in a page's <head>, well within this

# url -> publication date found while downloading the page ("" = the page states none). Kept for
# page_date() so a downloaded page is not fetched twice; entries are tiny strings.
_dates: dict[str, str] = {}


def page_date(url: str) -> str:
    """The page's stated publication date (see tools.quality.published_date), or "" if unknown.

    Uses the page downloaded by extract_text() when there was one; otherwise, if FETCH_PUBLISH_DATES,
    reads only the first HEAD_BYTES of the page (no text extraction). Never raises, never guesses."""
    if replay.active():
        return ""
    if url in _dates:
        return _dates.pop(url)
    if not config.FETCH_PUBLISH_DATES:
        return ""
    try:
        with requests.get(url, headers=_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS, stream=True) as resp:
            if not resp.ok or resp.headers.get("Content-Type", "").split(";")[0].strip().lower() not in _HTML_TYPES:
                return ""
            head = b""
            for chunk in resp.iter_content(chunk_size=16384):
                head += chunk
                if len(head) >= HEAD_BYTES:
                    break
        return published_date(head.decode("utf-8", "ignore"))
    except Exception:
        return ""


def extract_text(url: str, errors: list[str]) -> str:
    """Fetch `url` and return its main text, or "" (reason appended to `errors`)."""
    try:
        with requests.get(url, headers=_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS, stream=True) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype and ctype not in _HTML_TYPES:
                errors.append(f"extract skipped non-HTML ({ctype}) at {url}")
                return ""
            body, deadline = b"", time.time() + config.REQUEST_TIMEOUT_SECONDS
            for chunk in resp.iter_content(chunk_size=65536):
                body += chunk
                if len(body) >= config.MAX_PAGE_BYTES or time.time() > deadline:
                    break
        _dates[url] = published_date(body[:HEAD_BYTES].decode("utf-8", "ignore"))  # read later by page_date()
        # Pass bytes so trafilatura detects the character encoding itself.
        text = trafilatura.extract(body, include_comments=False, include_tables=False) or ""
    except Exception as exc:
        errors.append(f"extract failed for {url}: {type(exc).__name__}: {str(exc)[:200]}")
        return ""
    if len(text) < config.MIN_CONTENT_CHARS:
        errors.append(f"extract found no main text at {url} ({len(text)} chars)")
        return ""
    return text[: config.EXTRACT_MAX_CHARS]


def _clean_raw(raw_content: str) -> str:
    # Collapse spaces but keep line breaks: they mark headings/paragraphs, which sentence splitting needs.
    raw = re.sub(r"[ \t\f\v\x08]+", " ", raw_content or "")
    lines = re.sub(r"\s*\n\s*", "\n", raw).strip().split("\n")
    # The provider's text still has the site's menus ("Patient & Visitor Guide", "Make a Donation", often
    # twice). Drop repeated lines and short lines that are not sentences or questions.
    seen: set[str] = set()
    kept = []
    for line in lines:
        if line in seen or (len(line) < MENU_LINE_CHARS and not line.endswith((".", "?", "!", ":"))):
            continue
        seen.add(line)
        kept.append(line)
    return "\n".join(kept)


def api_content(raw_content: str) -> str:
    """Text a source API returned itself (an abstract), cleaned and capped; "" when too short to use."""
    raw = _clean_raw(raw_content)
    return raw[: config.EXTRACT_MAX_CHARS] if len(raw) >= config.MIN_CONTENT_CHARS else ""


def page_content(url: str, raw_content: str, errors: list[str]) -> tuple[str, str]:
    """Return (text, extracted_by) from our extractor and the search provider's text, in EXTRACT_ORDER."""
    raw = _clean_raw(raw_content)
    if replay.active():  # no page is downloaded in replay mode
        return (raw[: config.EXTRACT_MAX_CHARS], "replay") if raw else ("", "")
    if config.EXTRACT_ORDER == "provider_first" and len(raw) >= config.MIN_CONTENT_CHARS:
        return raw[: config.EXTRACT_MAX_CHARS], "tavily_raw"  # no download or HTML parsing (slow CPUs)
    own_errors: list[str] = []
    text = extract_text(url, own_errors)
    if text:
        return text, "trafilatura"
    if len(raw) >= config.MIN_CONTENT_CHARS:
        return raw[: config.EXTRACT_MAX_CHARS], "tavily_raw"
    errors.extend(own_errors)
    errors.append(f"no page text for {url}: extraction failed and search provider gave none")
    return "", ""
