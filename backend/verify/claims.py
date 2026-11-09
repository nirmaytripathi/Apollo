"""Claim verification against the RETRIEVED source text (never the LLM's memory).

verify_report(report, findings, errors) -> list[ClaimCheck]

For every report sentence that carries a citation (a "claim"):
1. Passages: for each cited source, its retrieved text (page content, else snippet) is split
   into sentences; the VERIFY_TOP_K sentences most similar to the claim (all-MiniLM-L6-v2
   embeddings) become numbered passages "S3.1", "S3.2", ... Each passage keeps its neighbouring
   sentences, its position in the source text and the nearest heading above it, so a reader can
   find it in the source.
2. One LLM call per claim, with the passages of ALL its cited sources: the claim is split into
   its factual parts, and each part gets a verdict (supported / partial / contradicted /
   unsupported), the passage that decides it and an exact quote from that passage.
3. The code checks every quote: it must appear in the passage (or elsewhere in that source's
   text). A part whose quote is not there, or that names an unknown passage, counts as
   unsupported, so an invented quote can never verify a claim.
4. Claim status, from its parts:
   - verified    : every part is supported
   - contradicted: a passage states the opposite of some part
   - partial     : some parts are (partly) supported, others are not; the unsupported parts are named
   - not_found   : no part is supported, or the cited sources have no retrieved text
   - unchecked   : over the VERIFY_MAX_CLAIMS cap, or no model could be asked (quota spent, errors)

Claims verified before this format (saved runs) have no parts or passages; the API marks
them `legacy`.

Models: config.VERIFY_CHAIN, e.g. Groq gpt-oss-20b -> Groq gpt-oss-120b -> Gemini
(each has its own quota; providers without an API key are skipped). When a model's
daily quota is spent, verification moves to the next one for the rest of the report.

Speed: claims are checked VERIFY_WORKERS at a time (each claim is mostly waiting for the LLM),
all needed sentences are embedded in one batch up front, and each finished claim is published
as a live `claim_checked` progress event. Results keep the report's claim order.
"""

import contextvars
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from core import config, live, replay
from core.llm import chat as llm_chat
from tools.embed import embed
from tools.jsonparse import parse_json
from tools.retry_policy import is_quota_exhausted
from tools.text import (  # noqa: F401  (CLAIM_STATUSES is re-exported for callers)
    CLAIM_STATUSES,
    find_citations,
    keyword_scores,
    split_sentences,
    strip_citations,
    top_by_keywords,
)

VERDICTS = ("supported", "partial", "contradicted", "unsupported")

VERIFY_SYSTEM = (
    "You check a claim against numbered source passages. Judge ONLY from the passages given; do not use "
    "outside knowledge.\n"
    "1. Split the claim into its separate factual parts (1 to 4 short parts; a simple claim is one part). "
    "Together the parts must cover everything the claim states. Wording that only refers to the report "
    "itself (such as \"The sections below summarise\") is not a factual part: leave it out.\n"
    "2. Give each part a verdict:\n"
    "   supported: a passage states it, or clearly paraphrases it, including its numbers, dates and qualifiers\n"
    "   partial: a passage backs it only in part (weaker or narrower wording, a different number range)\n"
    "   contradicted: a passage states the opposite, or a different number for the same quantity\n"
    "   unsupported: no passage states it (a passage that only discusses the topic is not enough)\n"
    "3. For supported, partial and contradicted parts, name the deciding passage (e.g. \"S3.2\") and copy "
    "the exact words from that passage that justify the verdict. For unsupported parts leave both empty.\n"
    'Answer JSON only: {"parts": [{"text": "...", "verdict": "supported|partial|contradicted|unsupported", '
    '"passage": "S3.2", "quote": "..."}]}'
)

PASSAGE_CONTEXT = 1  # neighbouring sentences kept on each side of a passage (for the reader, not the LLM)


def _available_chain() -> list[tuple[str, str]]:
    if replay.active():  # core.llm.chat answers from core/replay.py; no key is needed
        return [("replay", "replay")]
    keys = {"groq": config.GROQ_API_KEY, "gemini": config.GEMINI_API_KEY}
    return [(p, m) for p, m in dict.fromkeys(config.VERIFY_CHAIN) if keys.get(p)]


def report_claims(report: str) -> list[str]:
    """Sentences of the report body (headings skipped) that carry at least one citation."""
    body = "\n".join(line for line in report.splitlines() if not line.lstrip().startswith("#"))
    return [s for s in split_sentences(body) if find_citations(s)]


def _source_sentences(finding: dict) -> list[str]:
    text = finding.get("content") or finding.get("snippet") or ""
    return [s for s in split_sentences(text) if len(s) >= 20]


def verify_report(report: str, findings: list[dict], errors: list[str]) -> list[dict]:
    by_id = {f["id"]: f for f in findings}
    claims = report_claims(report)
    # Shared by all claims: a model whose daily quota is spent is removed for the rest of the report.
    models = _available_chain()
    if not models:
        errors.append("verify: no verification model has an API key; all claims 'unchecked'")

    def candidates(claim: str, sid: str) -> list[str]:
        # On slow hosts only the sentences sharing the most words with the claim are compared
        # (VERIFY_MAX_SENTENCES > 0). tools.embed caches each sentence's vector for the whole run.
        return top_by_keywords(_source_sentences(by_id[sid]), strip_citations(claim), config.VERIFY_MAX_SENTENCES)

    checked = claims[: config.VERIFY_MAX_CLAIMS]
    # One embedding batch for everything the checks will compare (cached), instead of many small ones.
    wanted = [strip_citations(c) for c in checked]
    wanted += [s for c in checked for sid in find_citations(c) if sid in by_id for s in candidates(c, sid)]
    try:
        if wanted:
            embed(list(dict.fromkeys(wanted)))
    except Exception:
        pass  # each claim retries its own embedding below and reports a failure there

    total, done = len(checked), [0]
    progress = threading.Lock()

    def check(claim: str) -> dict:
        result = _check_claim(claim, by_id, models, errors, candidates)
        with progress:
            done[0] += 1
            n = done[0]
        live.publish("claim_checked", {"done": n, "total": total, "status": result["status"]})
        return result

    with ThreadPoolExecutor(max_workers=max(1, config.VERIFY_WORKERS), thread_name_prefix="verify") as pool:
        # Each task runs in a copy of this context, so LangGraph's run id still reaches the usage
        # meter and the live progress events from the worker threads.
        futures = [pool.submit(contextvars.copy_context().run, check, c) for c in checked]
        results = [f.result() for f in futures]

    for claim in claims[config.VERIFY_MAX_CLAIMS :]:
        ids = [i for i in find_citations(claim) if i in by_id]
        results.append({"claim": claim, "source_ids": ids, "status": "unchecked", "best_match": "",
                        "similarity": 0.0, "evidence": "", "parts": [], "passages": []})
    return results


def _check_claim(claim: str, by_id: dict, models: list[tuple[str, str]], errors: list[str], candidates) -> dict:
    ids = [i for i in find_citations(claim) if i in by_id]
    result = {"claim": claim, "source_ids": ids, "status": "not_found", "best_match": "", "similarity": 0.0,
              "evidence": "", "parts": [], "passages": []}
    try:
        claim_vec = embed([strip_citations(claim)])[0]
    except Exception as exc:
        errors.append(f"verify: embedding failed: {type(exc).__name__}: {str(exc)[:200]}")
        result["status"] = "unchecked"
        return result

    passages = []
    for sid in ids:
        sents = candidates(claim, sid)
        if not sents:
            continue
        sims = embed(sents) @ claim_vec
        chosen = {sents[i]: float(sims[i]) for i in sims.argsort()[::-1][: config.VERIFY_TOP_K].tolist()}
        # A claim listing several facts is most similar to sentences about its main fact; the sentence
        # stating a later fact ("theoretical effects on cell growth") is found by its own words instead.
        extra = [s for s in clause_matches(_source_sentences(by_id[sid]), strip_citations(claim)) if s not in chosen]
        if extra:
            chosen.update(zip(extra, (embed(extra) @ claim_vec).tolist()))
        order = {s: i for i, s in enumerate(_source_sentences(by_id[sid]))}
        for n, s in enumerate(sorted(chosen, key=lambda s: order.get(s, 0)), 1):  # keep source order
            passages.append(_passage(by_id[sid], sid, n, s, chosen[s]))
    result["passages"] = passages
    if not passages:
        return result  # nothing retrieved to check against: not_found
    best = max(passages, key=lambda p: p["similarity"])
    result["similarity"], result["best_match"] = best["similarity"], best["text"]
    if not models:
        result["status"] = "unchecked"  # never mark a claim checked when the LLM could not be asked
        return result

    listing = "\n".join(f"[{p['label']}] {p['text']}" for p in passages)
    raw = _ask(f"Claim: {strip_citations(claim)}\n\nPassages:\n{listing}", models, errors, ",".join(ids))
    obj = parse_json(raw) if raw is not None else None
    parts = obj.get("parts") if isinstance(obj, dict) else None
    if not isinstance(parts, list) or not parts:
        if raw is not None:
            errors.append(f"verify: invalid JSON for claim {strip_citations(claim)[:60]!r}: {raw[:120]!r}")
        result["status"] = "unchecked"  # the verifier gave no usable answer: not a verdict on the claim
        return result

    by_label = {p["label"]: p for p in passages}
    result["parts"] = [_checked_part(part, by_label, by_id) for part in parts[:6] if isinstance(part, dict)]
    result["status"] = claim_status(result["parts"])
    decisive = next((pt for pt in result["parts"] if pt["verdict"] == "supported"), None)
    if decisive:
        result["evidence"], result["best_match"] = decisive["quote"], by_label[decisive["passage"]]["text"]
    return result


_CLAUSE = re.compile(r"[,;]|\b(?:and|alongside|with|while|including)\b", re.I)


def clause_matches(sentences: list[str], claim: str, limit: int = 4) -> list[str]:
    """For each clause of the claim, the source sentence sharing the most of its words (at least 2)."""
    picked: list[str] = []
    for clause in _CLAUSE.split(claim):
        scores = keyword_scores(sentences, clause)
        if not scores:
            continue
        best = max(range(len(sentences)), key=lambda i: (scores[i], -i))
        if scores[best] >= 2 and sentences[best] not in picked:
            picked.append(sentences[best])
    return picked[:limit]


def claim_status(parts: list[dict]) -> str:
    verdicts = [p["verdict"] for p in parts]
    if not verdicts:
        return "unchecked"
    if "contradicted" in verdicts:
        return "contradicted"
    if all(v == "supported" for v in verdicts):
        return "verified"
    if any(v in ("supported", "partial") for v in verdicts):
        return "partial"
    return "not_found"


def _checked_part(part: dict, by_label: dict, by_id: dict) -> dict:
    """One part of a claim as the LLM judged it, kept only as far as the source text bears it out."""
    out = {"text": str(part.get("text", ""))[:300], "verdict": str(part.get("verdict", "")).strip().lower(),
           "passage": str(part.get("passage", "")).strip().strip("[]"), "source_id": "",
           "quote": str(part.get("quote", "")).strip()[:400], "note": ""}
    if out["verdict"] not in VERDICTS:
        out.update(verdict="unsupported", note=f"the verifier gave no valid verdict ({out['verdict'][:20]!r})")
    if out["verdict"] == "unsupported":
        out.update(passage="", quote="")
        return out
    passage = by_label.get(out["passage"])
    if passage is None:
        out.update(verdict="unsupported", note=f"the verifier named an unknown passage ({out['passage'][:12]!r})",
                   passage="", quote="")
        return out
    out["source_id"] = passage["source_id"]
    finding = by_id[passage["source_id"]]
    source_text = finding.get("content") or finding.get("snippet") or ""
    if not out["quote"] or not (quote_in(out["quote"], passage["text"]) or quote_in(out["quote"], source_text)):
        out.update(verdict="unsupported", note=f"the quoted words are not in {passage['source_id']}'s text")
    return out


# Models often write a non-breaking hyphen (U+2011) or an en dash where the source has "-" ("moderate\u2011certainty"),
# or a minus sign (U+2212) for "-": every such dash counts as "-", and a soft hyphen as nothing. Seen live: 3 of 4
# rejected quotes in two runs were real quotes, refused only for their dashes.
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"} | {"\u00ad": None})


def _norm(text: str) -> str:
    text = text.lower().translate(_DASHES)
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", re.sub(r"[^\w%.,;:'\"()-]+", " ", text)).strip()


def quote_in(quote: str, text: str) -> bool:
    """True if every piece of `quote` (split at "..." ellipses) appears in `text`, in order.

    Case, spacing, curly quotes and other punctuation do not matter; the words must."""
    hay, pos = _norm(text), 0
    pieces = [_norm(p).strip(" .,;:'\"") for p in re.split(r"\.\.\.|\u2026", quote)]
    pieces = [p for p in pieces if p]
    if not pieces:
        return False
    for piece in pieces:
        found = hay.find(piece, pos)
        if found < 0:
            return False
        pos = found + len(piece)
    return True


def _passage(finding: dict, sid: str, n: int, sentence: str, similarity: float) -> dict:
    """A numbered passage with where it sits in the source text, for the reader to find it there."""
    sents = _source_sentences(finding)
    text = finding.get("content") or finding.get("snippet") or ""
    i = sents.index(sentence) if sentence in sents else -1
    start = text.find(sentence)
    return {
        "label": f"{sid}.{n}",
        "source_id": sid,
        "text": sentence,
        "before": " ".join(sents[max(0, i - PASSAGE_CONTEXT) : i]) if i > 0 else "",
        "after": " ".join(sents[i + 1 : i + 1 + PASSAGE_CONTEXT]) if i >= 0 else "",
        "similarity": round(similarity, 3),
        "position": round(start / len(text), 2) if start >= 0 and text else None,  # 0 = start of the text
        "heading": _heading_above(text, start) if start >= 0 else "",
    }


def _heading_above(text: str, start: int) -> str:
    """The nearest line above `start` that looks like a heading (short, not ending a sentence)."""
    for line in reversed(text[:start].split("\n")[:-1]):
        line = line.strip()
        if 3 <= len(line) <= 90 and not line.endswith((".", ",", ";")):
            return line
    return ""


_models_lock = threading.Lock()


def _ask(prompt: str, models: list[tuple[str, str]], errors: list[str], sid: str) -> str | None:
    """Ask the first model in `models`; on any failure, try the next one for this claim.

    `models` is shared across the whole report: a model whose daily quota is spent is removed, so it
    is not called again; when the list is empty, verification stops calling the LLM. Any other failure
    (a per-minute limit still hit after the retries, an overloaded or timed-out model) moves only this
    claim to the next model, so one busy model no longer leaves the claim 'unchecked'.
    """
    tried: set[tuple[str, str]] = set()
    while True:
        with _models_lock:
            left = [m for m in models if m not in tried]
            if not left:
                return None
            provider, model = left[0]
        try:
            return llm_chat(VERIFY_SYSTEM, prompt, temperature=0.0, model=model, provider=provider)
        except Exception as exc:
            tried.add((provider, model))
            if is_quota_exhausted(exc):
                with _models_lock:
                    # Several claims run at once: only the first to see this model fail removes it.
                    if (provider, model) in models:
                        models.remove((provider, model))
                        nxt = f"switching to {models[0][0]}:{models[0][1]}" if models else "remaining claims will be 'unchecked'"
                        errors.append(f"verify: quota exhausted for {provider}:{model}; {nxt}")
                continue
            with _models_lock:
                more = any(m not in tried for m in models)
            errors.append(f"verify: LLM call failed for {sid} on {provider}:{model}: {type(exc).__name__}: "
                          f"{str(exc)[:200]}" + ("; trying the next model" if more else ""))
