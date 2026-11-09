"""Central configuration for Apollo.

All secrets and loop limits are read from .env (via python-dotenv) with safe
defaults. Nothing here is hardcoded except the defaults themselves. Other
modules import values from here instead of reading os.environ directly, so
there is exactly one place to look when asking "where does X come from?".
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Repository root (this file is backend/core/config.py): .env and output/ live there, wherever the server is
# started from.
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _per_domain(name: str, defaults: dict[str, float]) -> dict[str, float]:
    """`defaults`, overridden by "domain:value,domain:value" in the env variable `name` (bad items ignored)."""
    out = dict(defaults)
    for item in os.getenv(name, "").split(","):
        domain, _, value = item.partition(":")
        try:
            out[domain.strip().lower()] = float(value)
        except ValueError:
            continue
    return out


# --- API keys (empty string means "not configured") ---
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# --- LLM selection ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
# A second Gemini model with its own capacity: used when GEMINI_MODEL is overloaded (503).
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
PLAN_TEMPERATURE = 0.2
REVIEW_TEMPERATURE = 0.2
WRITE_TEMPERATURE = 0.2
# Claim verification uses a separate (smaller) model: on Groq each model has its own rate limit.
VERIFY_MODEL = os.getenv("VERIFY_MODEL", "openai/gpt-oss-20b")
# Fallback chain for planning, review and writing, "provider:model" in order. The default starts
# with LLM_PROVIDER's model, then the other models, so a spent daily quota or a rate-limit burst
# on one model moves the call to the next instead of failing the section. Providers without a key
# are skipped.
# Spare models, each with its own free quota, used only after the main ones are spent. Checked on 2026-11-01:
# every one answered a JSON prompt on this project's keys (gemini-2.5-* were retired for new users; the
# two flash-lite models above were out of quota). Preview models can be withdrawn: override with LLM_CHAIN.
SPARE_MODELS = {
    "groq": os.getenv("GROQ_SPARE_MODELS", "qwen/qwen3.8-27b"),
    "gemini": os.getenv("GEMINI_SPARE_MODELS", "gemini-3-flash-preview,gemini-3.6-flash,gemini-3.5-flash,gemma-4-26b-a4b-it"),
}
_SPARE = {p: [f"{p}:{m.strip()}" for m in ms.split(",") if m.strip()] for p, ms in SPARE_MODELS.items()}
_DEFAULT_LLM_MODELS = {
    "groq": [f"groq:{GROQ_MODEL}", f"groq:{VERIFY_MODEL}"] + _SPARE["groq"],
    "gemini": [f"gemini:{GEMINI_MODEL}", f"gemini:{GEMINI_FALLBACK_MODEL}"] + _SPARE["gemini"],
}
LLM_CHAIN = [
    tuple(item.strip().split(":", 1))
    for item in os.getenv(
        "LLM_CHAIN",
        ",".join(dict.fromkeys(
            _DEFAULT_LLM_MODELS.get(LLM_PROVIDER, []) + [m for ms in _DEFAULT_LLM_MODELS.values() for m in ms]
        )),
    ).split(",")
    if ":" in item
]
LLM_COOLDOWN_SECONDS = _int("LLM_COOLDOWN_SECONDS", 600)  # skip a model this long after its quota ran out

# Fallback chain for claim verification, "provider:model" in order. Each entry has its own quota;
# when one is spent for the day, verification moves on. Providers without a key are skipped.
VERIFY_CHAIN = [
    tuple(item.strip().split(":", 1))
    for item in os.getenv(
        "VERIFY_CHAIN",
        ",".join([f"groq:{VERIFY_MODEL}", f"groq:{GROQ_MODEL}", f"gemini:{GEMINI_MODEL}", f"gemini:{GEMINI_FALLBACK_MODEL}"]
                 + _SPARE["groq"] + _SPARE["gemini"]),
    ).split(",")
    if ":" in item
]

# --- Loop control (the graph enforces these in code) ---
MAX_ROUNDS = _int("MAX_ROUNDS", 2)
TIMEOUT_SECONDS = _int("TIMEOUT_SECONDS", 300)  # research clock: no new search round starts after this (writing still runs)
SEARCH_MAX_RETRIES = _int("SEARCH_MAX_RETRIES", 2)
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 2)
REQUEST_TIMEOUT_SECONDS = _int("REQUEST_TIMEOUT_SECONDS", 15)

# --- Research breadth ---
MIN_SUBQUESTIONS = 3
MAX_SUBQUESTIONS = 5
RESULTS_PER_QUERY = 5      # results requested from the search API
PAGES_PER_QUERY = 3        # top results whose full page text is extracted
MAX_GAP_QUERIES = 3        # cap on Hermes follow-up queries per round
EXTRACT_MAX_CHARS = 12000  # cap on stored page text (LLMs see only selected passages, see tools/passages.py)
MIN_CONTENT_CHARS = 200    # shorter extracts count as failed (use the fallback)
MAX_PAGE_BYTES = _int("MAX_PAGE_BYTES", 3_000_000)  # stop downloading a page after this many bytes
EXTRACT_WORKERS = _int("EXTRACT_WORKERS", 6)  # pages extracted in parallel per round (lower it on small servers)
SEARCH_WORKERS = _int("SEARCH_WORKERS", 5)  # web searches run at once per round (each waits on the API)
# Which page text Athena tries first: "own" (default: download + trafilatura, then the search provider's
# text) or "provider_first" (Tavily's page text when it has enough, download + trafilatura only for the
# rest). provider_first skips most downloads and HTML parsing, the slowest step on Render free's 0.1 CPU.
EXTRACT_ORDER = os.getenv("EXTRACT_ORDER", "own").strip().lower()

# --- Research quality rules (tools/quality.py) ---
MIN_SOURCES_PER_QUESTION = _int("MIN_SOURCES_PER_QUESTION", 2)  # readable sources each sub-question should have
# Share of word 5-grams two pages must have in common to count as the same article. Different articles share
# almost none; one paper on two URLs shared 71% (one copy also carried a cookie banner), so 0.8 missed it.
DUPLICATE_OVERLAP = 0.5
MIN_RELEVANCE = _float("MIN_RELEVANCE", 0.3)  # share (0-1) of the sub-question's key words a page must contain (tools/quality.relevance)
# Read each page's stated publication date from its <head> when the page itself was not downloaded.
FETCH_PUBLISH_DATES = os.getenv("FETCH_PUBLISH_DATES", "true").strip().lower() == "true"

# --- Source APIs per domain (tools/sources/) ---
# Domains whose specialist APIs Athena queries IN ADDITION to the general web search, when a sub-question's
# words route it there (tools/sources/router.classify). "general" (Tavily, then Serper) always runs.
DOMAIN_SOURCES = tuple(d.strip().lower() for d in os.getenv("DOMAIN_SOURCES", "academic,health,statistics,news,tech")
                       .split(",") if d.strip())
DOMAIN_PAGES_PER_QUERY = _int("DOMAIN_PAGES_PER_QUERY", 2)  # specialist results kept per query, on top of PAGES_PER_QUERY
# Results below this relevance (same 0-1 scale and scorer as MIN_RELEVANCE) do not count as a usable answer
# from an API: the fallback is tried. Override per domain with DOMAIN_RELEVANCE="academic:0.4,health:0.3".
DOMAIN_RELEVANCE = _per_domain("DOMAIN_RELEVANCE", {d: MIN_RELEVANCE for d in
                                                    ("academic", "statistics", "health", "news", "tech", "general")})
# How long a cached API result counts as fresh, in days; override with SOURCE_CACHE_TTL_DAYS="news:0.5,tech:7".
# An expired entry is kept, marked stale, and used only when both live APIs fail.
SOURCE_CACHE_TTL_DAYS = _per_domain("SOURCE_CACHE_TTL_DAYS", {"academic": 30, "statistics": 7, "health": 7,
                                                              "news": 1, "tech": 3, "general": 2})
SOURCE_CACHE_ENABLED = os.getenv("SOURCE_CACHE_ENABLED", "true").strip().lower() == "true"
SOURCE_CACHE_BUSY_SECONDS = _float("SOURCE_CACHE_BUSY_SECONDS", 5.0)  # SQLite waits this long for a lock
# Optional: polite-pool e-mail for OpenAlex, and keys that raise the free rate limits. None is required.
OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO", "")
# OpenAlex budgets per day: $0.10 without a key (~100 searches, and keyless search is throttled first when its
# servers are busy), $1 with a free key. Sent as an Authorization header, never in the URL.
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "")
SEMANTIC_SCHOLAR_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")          # GitHub search: 10 requests/min without, 30 with
STACKEXCHANGE_KEY = os.getenv("STACKEXCHANGE_KEY", "")  # Stack Exchange: 300 requests/day without, 10,000 with
GUARDIAN_API_KEY = os.getenv("GUARDIAN_API_KEY", "")  # The Guardian (news primary): free developer key, required

# --- Replay mode (core/replay.py) ---
# "live" (default) or "replay": fixed offline answers for every search, source API, LLM, page and embedding
# call, so tests and demos make no network calls and need no keys. Never switched on automatically.
LLM_MODE = os.getenv("LLM_MODE", "live").strip().lower()
# In replay mode: "ok" (primary APIs answer), "primary_down" (fallbacks answer), "all_down" (cache or error).
REPLAY_SCENARIO = os.getenv("REPLAY_SCENARIO", "ok").strip().lower()

# --- YouTube links (tools/video.py) ---
VIDEO_LANGUAGES = ["en", "en-US", "en-GB", "hi"]  # caption languages tried first, in order
VIDEO_LINE_SECONDS = 20     # caption snippets are grouped into lines of about this length, each with its time
VIDEO_MAX_CHARS = 48000     # transcript kept: a fast 35-minute lecture is about 40k characters; a longer one is marked as cut
VIDEO_PLAN_CHARS = 6000     # transcript lines, spread over the whole video, shown to the planner
# No captions, or YouTube blocks the server: let Gemini watch the video by URL and transcribe it.
VIDEO_GEMINI_FALLBACK = os.getenv("VIDEO_GEMINI_FALLBACK", "true").strip().lower() == "true"
VIDEO_GEMINI_MODEL = os.getenv("VIDEO_GEMINI_MODEL", GEMINI_MODEL)
# Tried in order until one gives a timestamped transcript: a model sometimes answers with no text, or is
# overloaded (503). Measured 2026-11-05 on a 14-minute talk: 21 s (flash-lite), ~47 s (the other two).
VIDEO_GEMINI_MODELS = list(dict.fromkeys(m.strip() for m in os.getenv(
    "VIDEO_GEMINI_MODELS", f"{VIDEO_GEMINI_MODEL},{GEMINI_FALLBACK_MODEL},gemini-3.5-flash").split(",") if m.strip()))
VIDEO_TIMEOUT_SECONDS = _int("VIDEO_TIMEOUT_SECONDS", 180)
# Longest video Apollo reads (35 min). The research form refuses a longer link (a pop-up, from the browser's own
# YouTube player); captions that show a longer video stop the run with that reason. Gemini is sent only this much,
# at few frames a second (the words are in the audio): sent whole, a 2-hour lecture (CS50 Lecture 0) timed out
# (504 DEADLINE_EXCEEDED). Measured 2026-11-06 on that lecture with gemini-3.1-flash-lite: first 20 min 27 s
# (22.7k characters), 30 min 38 s (33.8k), 35 min 43 s (39.9k); 45 min took 329 s.
VIDEO_MAX_SECONDS = _int("VIDEO_MAX_SECONDS", 2100)
VIDEO_GEMINI_FPS = _float("VIDEO_GEMINI_FPS", 0.2)

# --- Attached documents (tools/documents.py) ---
DOCUMENT_MAX_FILES = 3              # documents per run
DOCUMENT_MAX_BYTES = _int("DOCUMENT_MAX_BYTES", 10_000_000)  # per file
DOCUMENT_MAX_CHARS = 40000          # text kept per document (the rest is cut, and the source says so)
DOCUMENT_PLAN_CHARS = 4000          # lines from the attached documents shown to the planner, in total
DOCUMENT_UPLOAD_TTL_SECONDS = 3600  # an uploaded file is kept this long for a run to use it

# --- Writing and verification (Phase 2) ---
WRITE_CHARS_PER_SOURCE = 1200   # most relevant passages per source sent to the writer (TPM limit)
HERMES_CHARS_PER_SOURCE = 400   # most relevant passages per source shown to Hermes
# How source passages are chosen for Hermes and the writers: "embedding" (default) or "keyword"
# (no embedding; for slow hosts such as Render free). Claim verification always uses embeddings.
PASSAGE_RANKING = os.getenv("PASSAGE_RANKING", "embedding").strip().lower()
# Sentences per cited source compared with each claim, picked by shared words; 0 = all (default).
VERIFY_MAX_SENTENCES = _int("VERIFY_MAX_SENTENCES", 0)
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# "torch" (sentence-transformers, default) or "onnx" (fastembed: same model, no PyTorch, for 512 MB servers)
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "torch").strip().lower()
# Load the embedding model in the background when a run starts (tools/embed.warm_up). On for Render.
EMBED_WARM_UP = os.getenv("EMBED_WARM_UP", "false").strip().lower() == "true"
EMBED_TIMEOUT_SECONDS = 120       # a stuck embedding call raises instead of hanging the run
VERIFY_TOP_K = 5               # passages per cited source shown to the verifier LLM
VERIFY_MAX_CLAIMS = 40          # cap on LLM verification calls per report; the rest are "unchecked"
VERIFY_WORKERS = _int("VERIFY_WORKERS", 4)  # claims checked at the same time (each mostly waits for the LLM)
OUTPUT_DIR = os.getenv("OUTPUT_DIR", str(ROOT / "output"))
SOURCE_CACHE_DB = os.getenv("SOURCE_CACHE_DB", os.path.join(OUTPUT_DIR, "source_cache.db"))

# --- Accounts (web app login) ---
# JWT_SECRET signs session tokens. Set it in .env (any long random string); if it is missing, a random
# secret is generated at startup, which works but signs everyone out whenever the server restarts.
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_TTL_HOURS = _int("JWT_TTL_HOURS", 12)

# --- Database (users and saved runs; see db.py) ---
# Postgres URL, e.g. Neon's "postgresql://user:password@host/dbname?sslmode=require". Empty = local SQLite.
DATABASE_URL = os.getenv("DATABASE_URL", "")
LOCAL_DB = os.getenv("LOCAL_DB", os.path.join(OUTPUT_DIR, "apollo.db"))
# Older local stores imported once into the database at startup (then no longer used).
LEGACY_USERS_DB = os.path.join(OUTPUT_DIR, "users.db")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"  # true when served over HTTPS

# Hard backstop for LangGraph itself, independent of our own round logic.
GRAPH_RECURSION_LIMIT = 25
