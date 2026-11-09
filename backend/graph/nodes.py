"""The nodes of the Apollo graph — one function per agent step.

Research loop (Phase 1):
- apollo_plan    (Apollo, manager): LLM splits the topic into 3-5 sub-questions. If the topic
                 holds a YouTube link, the video's transcript is read first (tools/video.py),
                 becomes source S1 for every section, and the plan covers the video's claims.
                 Documents the user attached (tools/documents.py) follow as sources for every
                 section too, and the planner sees lines from them.
- review_plan    (human): optional interrupt to approve or edit the plan.
- athena_search  (Athena, searcher): web-searches each pending query, picks the
                 top new pages and numbers them S1, S2, ... in order, tags each
                 with its plan section, then extracts page text in parallel.
- hermes_review  (Hermes, reviewer): LLM reads each finding's title plus its most
                 relevant passages (tools/passages.py) and returns
                 {"complete", "gaps", "contradictions"} as JSON.
- review_gaps    (human): optional interrupt to approve, edit, or skip the gaps.
- decide         (Apollo, manager): applies the stop rules IN CODE
                 (Hermes complete / max rounds / timeout / no gaps) and either
                 sets up the next round or finishes the loop.
- route_after_decide: next round, or fan out one write_section per section.

Writing (Phase 2):
- write_section  runs once per plan section (LangGraph Send), sees ONLY that
                 section's findings. Code then removes citations to ids it was
                 not given and sentences with no citation.
- assemble       intro + sections + conclusion, duplicate removal, and a
                 References list built from the ids actually cited.
- verify_claims  checks each cited sentence against retrieved text (verify/claims.py).

Every node catches its own failures, logs them to state["errors"], and returns
a usable state, so the graph always reaches END.
"""

import contextvars
import re
import time
from concurrent.futures import ThreadPoolExecutor

from langgraph.types import Send, interrupt

from core import config, live, llm
from core.llm import chat as llm_chat
from tools.extract import api_content, page_content, page_date
from tools.passages import relevant_passages
from tools.quality import (
    limits_section,
    overall,
    published_iso,
    quality_checks,
    relevance,
    same_content,
    source_kind,
    usable_per_section,
)
from tools.jsonparse import parse_json
from tools.search import search as web_search
from tools.sources.router import classify
from tools.sources.router import search_domain as domain_search
from tools.documents import document_url
from tools.video import find_video, plan_excerpt, read_video
from tools.text import (
    CLAIM_STATUSES,
    NOT_FOUND,
    WRITE_FAILED,
    find_citations,
    int_id,
    mark_absence,
    normalize_citations,
    normalize_not_found,
    sentence_key,
    split_sentences,
)
from verify.claims import verify_report

from .state import ALL_SECTIONS, Query, ResearchState, Section

# ---------------------------------------------------------------- helpers


def _elapsed(state: ResearchState, now: float | None = None) -> float:
    # Approval waits are already taken out by shifting started_at; time paused by the user (Pause) is too.
    return (now or time.time()) - state["started_at"] - live.paused_seconds()


def _timed_out(state: ResearchState, now: float | None = None) -> bool:
    return _elapsed(state, now) > state["timeout_seconds"]


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").split("#")[0].lower()


def _clean_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}


def _best_section(query: str, plan: list[str]) -> int:
    """Fallback section for a gap query: the sub-question sharing the most words."""
    q = _words(query)
    return max(range(len(plan)), key=lambda i: len(q & _words(plan[i])), default=0)


def _to_queries(items, plan: list[str]) -> list[Query]:
    """Accept [{"section": n, "query": "..."}] (1-based section) or plain strings."""
    out: list[Query] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            query, section = item.strip(), None
        elif isinstance(item, dict) and isinstance(item.get("query"), str):
            query, section = item["query"].strip(), item.get("section")
        else:
            continue
        if not query or any(q["query"].lower() == query.lower() for q in out):
            continue
        idx = section - 1 if isinstance(section, int) and 1 <= section <= len(plan) else _best_section(query, plan)
        out.append({"query": query, "section": idx})
    return out


def _stop_reason(state: ResearchState, now: float | None = None) -> str | None:
    """The loop's stop rules, first match wins. None means: run another round."""
    if state["hermes_complete"]:
        return "hermes_complete"
    if state["round_no"] >= state["max_rounds"]:
        return "max_rounds_reached"
    if _timed_out(state, now):
        return "timeout"
    if state.get("gaps_skipped"):
        return "gaps_skipped_by_user"
    if not state["gaps"]:
        return "no_follow_up_queries"
    return None


# ---------------------------------------------------------------- Apollo: plan

PLAN_SYSTEM = (
    "You are Apollo, the research manager. Break the user's research topic into "
    f"{config.MIN_SUBQUESTIONS}-{config.MAX_SUBQUESTIONS} specific, searchable sub-questions "
    "that together cover the topic. Do NOT answer them. "
    'Reply with JSON only: {"sub_questions": ["...", "..."]}'
)


VIDEO_PLAN_SYSTEM = (
    "You are Apollo, the research manager. The user shared a video; you see its title and lines from its "
    f"transcript. List {config.MIN_SUBQUESTIONS}-{config.MAX_SUBQUESTIONS} specific, searchable sub-questions "
    "that together cover the video's main factual claims (and the user's own question, if any), so each claim "
    "can be checked against independent sources. State the claim itself in each sub-question (e.g. \"Does "
    "intermittent fasting lower blood pressure?\"), never \"the video\" or \"the speaker\". Do NOT answer them. "
    'Reply with JSON only: {"sub_questions": ["...", "..."]}'
)


def _video_finding(video: dict) -> dict:
    """The shared video as source S1, available to every section."""
    return {
        "id": "S1",
        "section": ALL_SECTIONS,
        "query": "(the video you shared)",
        "title": video["title"] + (f" ({video['channel']})" if video["channel"] else ""),
        "url": video["url"],
        "snippet": video["content"][:300],
        "content": video["content"],
        "extracted_by": video["extracted_by"],
        "kind": "video",
        "published": "",
        "retrieved_at": int(time.time()),
    }


def _document_finding(doc: dict, source_id: str) -> dict:
    """A document the user attached, as a source available to every section."""
    return {
        "id": source_id,
        "section": ALL_SECTIONS,
        "query": "(a document you attached)",
        "title": doc["name"],
        "url": document_url(doc["name"]),
        "snippet": doc["content"][:300],
        "content": doc["content"],
        "extracted_by": "pdf_text" if doc["kind"] == "pdf" else "plain_text",
        "kind": "document",
        "published": "",
        "retrieved_at": int(time.time()),
        "pages": doc.get("pages", 0),
        "truncated": doc.get("truncated", False),
    }


def _validate_plan(obj) -> list[str] | None:
    if not obj:
        return None
    qs = _clean_str_list(obj.get("sub_questions"))
    if len(qs) < config.MIN_SUBQUESTIONS:
        return None
    return qs[: config.MAX_SUBQUESTIONS]


def apollo_plan(state: ResearchState) -> dict:
    events, errors = list(state["events"]), list(state["errors"])
    topic = state["topic"]
    # A YouTube link is read before planning. VideoUnreadable is not caught: a run about a video that
    # could not be read stops with the reason instead of researching a guess.
    video = read_video(topic, errors) if find_video(topic) else None
    system, prompt = PLAN_SYSTEM, f"Topic: {topic}"
    if video:
        question = topic.replace(video["link"], " ").strip() or "(none: check the video's main claims)"
        system = VIDEO_PLAN_SYSTEM
        prompt = (f"User's question: {question}\nVideo: {video['title']}"
                  + (f" by {video['channel']}" if video["channel"] else "")
                  + f"\nTranscript lines:\n{plan_excerpt(video['content'], config.VIDEO_PLAN_CHARS)}")
        how = "its captions" if video["extracted_by"] == "youtube_captions" else "a Gemini transcription"
        events.append(f"[Apollo] Read the video {video['title']!r} from {how}: {len(video['content'])} characters"
                      + (" (cut at the transcript limit)" if video["truncated"] else ""))
    documents = state.get("documents") or []
    if documents:
        share = config.DOCUMENT_PLAN_CHARS // len(documents)
        prompt += "\nAttached documents (lines spread over each; cover what they say about the topic):\n" + "\n".join(
            f"--- {d['name']}\n{plan_excerpt(d['content'], share)}" for d in documents)
        events.append(f"[Apollo] Read {len(documents)} attached document(s): "
                      + ", ".join(f"{d['name']!r}" + (f" ({d['pages']} pages)" if d.get("pages") else "") for d in documents))
    plan = None
    for attempt in (1, 2):  # first try + one retry
        try:
            raw = llm_chat(system, prompt, temperature=config.PLAN_TEMPERATURE)
        except Exception as exc:
            errors.append(f"apollo_plan LLM call failed (attempt {attempt}): {type(exc).__name__}: {str(exc)[:200]}")
            continue
        plan = _validate_plan(parse_json(raw))
        if plan:
            break
        errors.append(f"apollo_plan got invalid plan JSON (attempt {attempt}): {raw[:200]!r}")

    if not plan and llm.all_exhausted():
        # Every model is out of quota: searching would only lead to a report no model can write.
        raise llm.LLMUnavailable(llm.quota_message())
    if not plan:
        plan = [f"What does the video {video['title']!r} claim, and is it accurate?" if video else topic]
        events.append("[Apollo] Planning failed; falling back to the topic itself as the only sub-question.")
    events.append(f"[Apollo] Plan ({len(plan)} sub-questions): " + " | ".join(plan))
    update = {"plan": plan, "paused_at": time.time(), "events": events, "errors": errors}
    findings = [_video_finding(video)] if video else []
    findings += [_document_finding(d, f"S{len(findings) + i + 1}") for i, d in enumerate(documents)]
    if findings:
        update["findings"] = findings
    if video:
        update["video"] = {k: v for k, v in video.items() if k not in ("content", "link")}
    return update


# ---------------------------------------------------------------- human: approve plan


def review_plan(state: ResearchState) -> dict:
    """Interrupt so a human can approve or edit the plan.

    Resume with Command(resume=...) where the value is True / {"approved": True}
    to accept, or {"plan": ["q1", "q2", ...]} to replace the plan.
    LangGraph re-runs this node from the top on resume, so nothing before
    interrupt() may have side effects.
    """
    events, errors = list(state["events"]), list(state["errors"])
    plan = state["plan"]
    if not state["auto_approve"]:
        answer = interrupt({"type": "approve_plan", "topic": state["topic"], "plan": plan})
        edited = _clean_str_list(answer.get("plan")) if isinstance(answer, dict) else []
        if edited:
            plan = edited[: config.MAX_SUBQUESTIONS]
            events.append("[Human] Plan edited: " + " | ".join(plan))
        elif isinstance(answer, dict) and "plan" in answer:
            errors.append("review_plan: edited plan was empty or invalid; keeping the original plan")
            events.append("[Human] Plan approved (edit was invalid).")
        else:
            events.append("[Human] Plan approved.")
    # Research clock starts now: human waiting time does not count against the timeout.
    pause = time.time() - state["paused_at"]
    return {
        "plan": plan,
        "pending_queries": [{"query": q, "section": i} for i, q in enumerate(plan)],
        "started_at": state["started_at"] + pause,
        "events": events,
        "errors": errors,
    }


# ---------------------------------------------------------------- Athena: search


def athena_search(state: ResearchState) -> dict:
    events, errors = list(state["events"]), list(state["errors"])
    findings = list(state["findings"])
    searched = list(state["searched_queries"])
    # Pages dropped earlier (duplicates, off-topic) are not read again in a later round.
    dropped = [d["url"] for d in state.get("duplicates", []) + state.get("irrelevant", [])]
    seen = {_norm_url(u) for u in [f["url"] for f in findings] + dropped}
    rnd = state["round_no"]

    # Step 1: search all queries at once (each mostly waits on the search API), then pick new pages
    # and assign ids in query order, so the numbering does not depend on which search finished first.
    new: list[tuple[dict, str, bool]] = []  # (finding without content, provider raw text, raw is the API's own text)
    items = list(state["pending_queries"])
    events.append(f"[Athena] Round {rnd}: searching {len(items)} queries")
    if items and _timed_out(state):
        errors.append(f"athena_search: timeout reached, skipping remaining queries from {items[0]['query']!r}")
        events.append("[Athena] Timeout reached; stopping this round early.")
        items = []

    def _search(item):
        errs: list[str] = []
        web = web_search(item["query"], errs)
        # A specialist domain (papers, medical literature) is searched too, on top of the web search.
        domain = classify(item["query"])
        extra = domain_search(domain, item["query"], errs) if domain in config.DOMAIN_SOURCES else []
        return web, extra, errs

    with ThreadPoolExecutor(max_workers=max(1, min(config.SEARCH_WORKERS, len(items) or 1))) as pool:
        # Each search runs in its own copy of this thread's context (copied here, not in the worker), so the
        # run id still reaches the usage meter and the live "search_results" events.
        contexts = [contextvars.copy_context() for _ in items]
        found = list(pool.map(lambda ctx, item: ctx.run(_search, item), contexts, items))

    for item, (web, extra, errs) in zip(items, found):
        query = item["query"]
        errors.extend(errs)
        searched.append(query)
        if not web and not extra:
            events.append(f"[Athena] No results for {query!r}; continuing.")
            continue

        added = 0
        for results, limit in ((web, config.PAGES_PER_QUERY), (extra, config.DOMAIN_PAGES_PER_QUERY)):
            taken = 0
            # Original research and official publications are read first; the search order breaks ties.
            for r in sorted(results, key=lambda r: source_kind(r["url"]) != "primary"):
                if taken >= limit:
                    break
                key = _norm_url(r["url"])
                if key in seen:
                    continue
                seen.add(key)
                finding = {
                    "id": "",  # numbered after duplicates are removed (step 3)
                    "section": item["section"],
                    "query": query,
                    "title": r.get("title", ""),
                    "url": r["url"],
                    "snippet": r.get("snippet", ""),
                    "content": "",
                    "extracted_by": "",
                    "kind": source_kind(r["url"]),
                    "published": published_iso(r.get("published", "")),  # the page's own date, "" = unknown
                    # the research date: when the result was retrieved (for a cached result, when it was cached)
                    "retrieved_at": int(r.get("retrieved_at") or time.time()),
                    "source_api": r.get("source", ""),
                    "freshness": r.get("freshness", "live"),  # "live" | "cached" | "stale"
                }
                new.append((finding, r.get("raw_content", ""), bool(r.get("api_text"))))
                taken += 1
            added += taken
        events.append(f"[Athena] {query!r}: {len(web) + len(extra)} results, {added} new sources")

    # Step 2 (parallel): extract page text and the page's stated publication date.
    def _extract(entry):
        finding, raw, from_api = entry
        errs: list[str] = []
        text = api_content(raw) if from_api else ""
        if text:  # the API's own text (an abstract): no landing-page download
            finding["content"], finding["extracted_by"] = text, f"{finding['source_api']}_text"
        else:
            finding["content"], finding["extracted_by"] = page_content(finding["url"], raw, errs)
        finding["published"] = finding["published"] or page_date(finding["url"])
        return errs

    with ThreadPoolExecutor(max_workers=config.EXTRACT_WORKERS) as pool:
        for errs in pool.map(_extract, new):  # map keeps input order, so error order is stable
            errors.extend(errs)

    # Step 3: drop pages that are not about their sub-question (e.g. a dictionary entry for one of its words)
    # or carry the same article as one already kept (one story at two URLs), then number the rest in query
    # order. Ids are given only now, so the numbering has no gaps.
    duplicates, irrelevant = list(state.get("duplicates", [])), list(state.get("irrelevant", []))
    kept: list[dict] = []
    for f, _, _ in new:
        text = f["content"] or f["snippet"]
        score = relevance(text, state["plan"][f["section"]]) if f["section"] < len(state["plan"]) else 1.0
        if text and score < config.MIN_RELEVANCE:
            irrelevant.append({"url": f["url"], "relevance": round(score, 2)})
            events.append(f"[Athena] Dropped {f['url']}: not about sub-question {f['section'] + 1} "
                          f"(shares {score:.0%} of its key words)")
            continue
        twin = next((k for k in findings + kept if f["content"] and k["content"] and same_content(f["content"], k["content"])), None)
        if twin is not None:
            duplicates.append({"url": f["url"], "duplicate_of": twin["id"]})
            events.append(f"[Athena] Dropped {f['url']}: same text as {twin['id']}")
            continue
        f["id"] = f"S{len(findings) + len(kept) + 1}"
        kept.append(f)
    findings.extend(kept)

    with_text = sum(bool(f["content"]) for f in kept)
    events.append(
        f"[Athena] Round {rnd} done; {len(kept)} new sources ({with_text} with page text"
        + (f", {len(new) - len(kept)} duplicate or off-topic page(s) dropped" if len(new) > len(kept) else "")
        + f"), total {len(findings)}"
    )
    return {"findings": findings, "searched_queries": searched, "pending_queries": [], "duplicates": duplicates,
            "irrelevant": irrelevant, "events": events, "errors": errors}


# ---------------------------------------------------------------- Hermes: review

REVIEW_SYSTEM = (
    "You are Hermes, a strict research reviewer. You see ONLY the sources listed by the user "
    "(id, title, relevant excerpt). Judge coverage using these sources alone; do not use outside knowledge "
    "and never invent sources.\n"
    "1. If every sub-question is reasonably supported by the sources, set complete=true.\n"
    f"2. Otherwise list up to {config.MAX_GAP_QUERIES} short web search queries that would fill the gaps, "
    "each with the number of the sub-question it serves.\n"
    "3. Compare the sources pairwise for contradictions: two listed sources that reach opposite conclusions "
    "about the same question (e.g. one says X lowers a risk, another says X raises it or has no benefit) "
    "or give conflicting numbers for the same quantity. Describe the disputed claim neutrally in one "
    "sentence. Do NOT report differences in scope, emphasis or detail, and report none if there are none.\n"
    'Reply with JSON only: {"complete": true|false, "gaps": [{"section": 1, "query": "..."}], '
    '"contradictions": [{"source_a": "S1", "source_b": "S4", "claim": "..."}]}'
)


def _review_prompt(state: ResearchState, errors: list[str]) -> str:
    """Each source is shown as its title plus the passages most relevant to its sub-question
    (not the search snippet, which is often boilerplate, and not the full text)."""
    lines = [f"Topic: {state['topic']}", "Sub-questions:"]
    lines += [f"{i}. {q}" for i, q in enumerate(state["plan"], 1)]
    lines.append("Sources:")
    for f in state["findings"]:
        question = state["plan"][f["section"]] if 0 <= f["section"] < len(state["plan"]) else state["topic"]
        excerpt = relevant_passages(f["content"] or f["snippet"], question, config.HERMES_CHARS_PER_SOURCE, errors)
        where = (f"sub-question {f['section'] + 1}" if f["section"] != ALL_SECTIONS
                 else "a document the user attached" if f.get("kind") == "document" else "the user's video, transcript")
        lines.append(f"[{f['id']}] ({where}) {f['title']} — {' '.join(excerpt.split())}")
    if not state["findings"]:
        lines.append("(no sources were found)")
    return "\n".join(lines)


def _coverage_queries(state: ResearchState, events: list[str]) -> list[Query]:
    """One follow-up search for each sub-question below MIN_SOURCES_PER_QUESTION readable sources.

    Written by code, not Hermes: the sub-question itself asks for research studies, which also
    favours original research. A sub-question whose follow-up was already searched gets none."""
    already = {q.lower() for q in state["searched_queries"]}
    out: list[Query] = []
    for i, n in enumerate(usable_per_section(state)):
        if n >= config.MIN_SOURCES_PER_QUESTION:
            continue
        query = f"{state['plan'][i]} research study"
        if query.lower() not in already:
            out.append({"query": query, "section": i})
            events.append(f"[Apollo] Sub-question {i + 1} has {n} readable source(s), target "
                          f"{config.MIN_SOURCES_PER_QUESTION}: searching {query!r}")
    return out


def hermes_review(state: ResearchState) -> dict:
    events, errors = list(state["events"]), list(state["errors"])
    valid_ids = {f["id"] for f in state["findings"]}
    done = {"paused_at": time.time()}  # a human may pause after this node

    raw = ""
    try:
        raw = llm_chat(REVIEW_SYSTEM, _review_prompt(state, errors), temperature=config.REVIEW_TEMPERATURE)
    except Exception as exc:
        errors.append(f"hermes_review LLM call failed: {type(exc).__name__}: {str(exc)[:200]}")
    obj = parse_json(raw)

    coverage = _coverage_queries(state, events)
    if obj is None or not isinstance(obj.get("complete"), bool):
        if raw:
            errors.append(f"hermes_review got invalid JSON: {raw[:200]!r}")
        events.append("[Hermes] Review output unusable; treating as incomplete with no gaps.")
        return {**done, "hermes_complete": False, "gaps": coverage[: config.MAX_GAP_QUERIES], "events": events, "errors": errors}

    already = {q.lower() for q in state["searched_queries"]}
    gaps = [g for g in _to_queries(obj.get("gaps"), state["plan"]) if g["query"].lower() not in already]
    # The coverage target comes first; Hermes' own follow-up queries fill the remaining places.
    gaps = (coverage + [g for g in gaps if g["section"] not in {c["section"] for c in coverage}])[: config.MAX_GAP_QUERIES]

    contradictions = list(state["contradictions"])
    for c in obj.get("contradictions") or []:
        if not isinstance(c, dict):
            continue
        a, b, claim = c.get("source_a"), c.get("source_b"), c.get("claim")
        # Hermes must not invent sources: keep only contradictions between real ids.
        if a in valid_ids and b in valid_ids and a != b and isinstance(claim, str) and claim.strip():
            # One entry per pair of sources: a later review often restates the same conflict in other words.
            if not any({x["source_a"], x["source_b"]} == {a, b} for x in contradictions):
                contradictions.append({"source_a": a, "source_b": b, "claim": claim.strip()})
        else:
            errors.append(f"hermes_review dropped contradiction with unknown/invalid sources: {c!r}"[:300])

    # "Complete" also needs the coverage target: Hermes cannot call a thinly sourced sub-question done.
    complete = obj["complete"] and not coverage
    events.append(
        f"[Hermes] complete={complete}, gaps={[g['query'] for g in gaps]}, contradictions={len(contradictions)}"
    )
    return {
        **done,
        "hermes_complete": complete,
        "gaps": gaps,
        "contradictions": contradictions,
        "events": events,
        "errors": errors,
    }


# ---------------------------------------------------------------- human: approve gaps


def review_gaps(state: ResearchState) -> dict:
    """Optional interrupt to approve, edit, or skip Hermes' gap queries.

    Pauses only when another round would actually run. Resume value: True /
    {"approved": True} to accept, {"skip": True} to skip, or
    {"gaps": ["query", ...]} to replace them.
    """
    if state["auto_approve"] or not state["review_gaps"]:
        return {}
    # Evaluate the stop rules at paused_at (not "now") so the answer is the same
    # when LangGraph re-runs this node on resume.
    if _stop_reason(state, now=state["paused_at"]) is not None:
        return {}

    events, errors = list(state["events"]), list(state["errors"])
    answer = interrupt({"type": "approve_gaps", "round": state["round_no"], "gaps": [g["query"] for g in state["gaps"]]})
    update: dict = {}
    if isinstance(answer, dict) and answer.get("skip"):
        update = {"gaps": [], "gaps_skipped": True}
        events.append("[Human] Gap queries skipped.")
    elif isinstance(answer, dict) and "gaps" in answer:
        edited = _to_queries(_clean_str_list(answer.get("gaps")), state["plan"])[: config.MAX_GAP_QUERIES]
        if edited:
            update = {"gaps": edited}
            events.append(f"[Human] Gap queries edited: {[g['query'] for g in edited]}")
        else:
            errors.append("review_gaps: edited gap list was empty or invalid; keeping Hermes' gaps")
    else:
        events.append("[Human] Gap queries approved.")
    pause = time.time() - state["paused_at"]
    return {**update, "started_at": state["started_at"] + pause, "events": events, "errors": errors}


# ---------------------------------------------------------------- Apollo: decide


def decide(state: ResearchState) -> dict:
    """Stop rules live in _stop_reason, in code."""
    events = list(state["events"])
    reason = _stop_reason(state)
    if reason is None:
        nxt = state["round_no"] + 1
        events.append(f"[Apollo] Starting round {nxt} with gap queries: {[g['query'] for g in state['gaps']]}")
        return {"round_no": nxt, "pending_queries": list(state["gaps"]), "events": events}

    status = "complete" if state["findings"] else "failed"
    events.append(f"[Apollo] Stopping: {reason} (round {state['round_no']}, {_elapsed(state):.1f}s). Status: {status}")
    return {"status": status, "stop_reason": reason, "events": events}


def route_after_decide(state: ResearchState):
    """Another research round, or one write_section task per plan section (run in parallel)."""
    if state["status"] == "running":
        return "athena_search"
    return [
        Send(
            "write_section",
            {
                "index": i,
                "question": q,
                "topic": state["topic"],
                "findings": [f for f in state["findings"] if f["section"] in (i, ALL_SECTIONS)],
            },
        )
        for i, q in enumerate(state["plan"])
    ]


# ---------------------------------------------------------------- Apollo: write

WRITE_SYSTEM = (
    "You are Apollo's writer. Write ONE short paragraph (3-5 sentences, no headings, no bullet points) "
    "that answers the sub-question using ONLY the numbered sources the user gives you.\n"
    "Rules:\n"
    "- Use only facts stated in these sources, even if you know other facts about the topic.\n"
    "- Paraphrase closely. Do not add names, descriptions, numbers, examples or qualifiers that the "
    "cited source text does not contain, and do not merge facts from different sources into one claim "
    "unless you cite each of them.\n"
    "- End every sentence with the citation(s) of the source(s) that state it, like [S3] or [S3][S5].\n"
    "- Never cite an id that is not in the list.\n"
    "- Sources marked (original research or official) are primary evidence: where sources overlap, state "
    "the fact from them and cite them first.\n"
    "- If sources disagree, say so in one sentence and cite each side; never pick one side silently.\n"
    "- A source marked (video transcript) records what the user's video says, not whether it is true: "
    "attribute it (\"The video says ...\") and use the other sources to judge it.\n"
    "- A source marked (stale cached result ...) was retrieved earlier because live search failed: when you "
    "use it, say the information may be out of date.\n"
    f"- If the sources do not support a point the sub-question asks about, write a sentence saying it was "
    f"\"{NOT_FOUND}\" (with no citation).\n"
    "Reply with the paragraph only."
)


def _source_block(findings: list[dict], question: str, errors: list[str]) -> str:
    """Each source's passages most relevant to the sub-question (not just its first N chars).

    Original research and official sources come first and are marked, so the writer leans on them."""
    parts = []
    for f in sorted(findings, key=lambda f: f.get("kind") != "primary"):
        text = relevant_passages(f["content"] or f["snippet"], question, config.WRITE_CHARS_PER_SOURCE, errors)
        mark = {"primary": " (original research or official)", "video": " (video transcript)",
                "document": " (document the user attached)"}.get(f.get("kind"), "")
        if f.get("freshness") in ("cached", "stale"):
            when = time.strftime("%Y-%m-%d", time.gmtime(f.get("retrieved_at") or 0))
            mark += (f" (stale cached result from {when}, may be out of date)" if f["freshness"] == "stale"
                     else f" (cached result from {when})")
        parts.append(f"[{f['id']}]{mark} {f['title']}\n{text}")
    return "\n\n".join(parts)


def enforce_citations(
    text: str, allowed_ids: set[str], errors: list[str], where: str, allow=(), dropped: list[str] | None = None
) -> list[str]:
    """Keep only sentences whose citations are all allowed and that cite something.

    Unknown ids are stripped from a sentence. A sentence with no valid citation is
    dropped unless it says "not found in sources" or matches one of `allow` (regexes).
    Dropped sentences are also appended to `dropped` when given.
    """
    kept = []
    for sentence in split_sentences(normalize_not_found(normalize_citations(text))):
        bad = [i for i in find_citations(sentence) if i not in allowed_ids]
        for i in bad:
            sentence = sentence.replace(f"[{i}]", "")
        if bad:
            errors.append(f"{where}: removed citation(s) to ids not in its sources: {bad}")
        sentence = re.sub(r"\s+([.,;:])", r"\1", re.sub(r"\s{2,}", " ", sentence)).strip()
        if not sentence:
            continue
        sentence = mark_absence(sentence[0].upper() + sentence[1:])
        if find_citations(sentence) or NOT_FOUND in sentence.lower() or any(re.search(p, sentence, re.I) for p in allow):
            kept.append(sentence)
        elif sentence:
            errors.append(f"{where}: dropped uncited sentence: {sentence[:120]!r}")
            if dropped is not None:
                dropped.append(sentence)
    return kept


DRAFT_INTERVAL_SECONDS = 0.25  # how often a section's in-progress text is pushed to the browser


def _draft_publisher(index: int):
    """on_text callback: publish the section's text-so-far as a `section_draft` event, throttled.
    Drafts are raw model output; the final text (after citation enforcement) arrives with
    `section_written` and replaces it."""
    last = [0.0]

    def on_text(text: str) -> None:
        now = time.monotonic()
        if now - last[0] >= DRAFT_INTERVAL_SECONDS:
            last[0] = now
            live.publish("section_draft", {"index": index, "text": text})

    return on_text


def write_section(task: dict) -> dict:
    """Runs once per section (via Send). Writes to `sections` only (it has a reducer)."""
    index, question, findings = task["index"], task["question"], task["findings"]
    errors: list[str] = []
    ids = [f["id"] for f in findings]
    usable = [f for f in findings if f["content"] or f["snippet"]]

    text = ""
    if usable:
        allowed = {f["id"] for f in usable}
        user = f"Sub-question: {question}\n\nSources:\n{_source_block(usable, question, errors)}"
        best: list[str] = []
        replied = False  # did any model answer? If not, the section was never written
        for attempt in (1, 2):  # first try + one retry with feedback if any sentence had to be dropped
            dropped: list[str] = []
            try:
                raw = llm_chat(WRITE_SYSTEM, user, temperature=config.WRITE_TEMPERATURE, on_text=_draft_publisher(index))
            except Exception as exc:
                errors.append(f"write_section {index + 1} LLM call failed: {type(exc).__name__}: {str(exc)[:200]}")
                break
            replied = True
            kept = enforce_citations(raw, allowed, errors, f"write_section {index + 1} (attempt {attempt})", dropped=dropped)
            if sum(bool(find_citations(s)) for s in kept) > sum(bool(find_citations(s)) for s in best) or not best:
                best = kept
            if not dropped:
                break
            user += (
                "\n\nYour previous answer had sentences without a valid citation, which were removed:\n"
                + "\n".join(f"- {s}" for s in dropped)
                + f"\nRewrite the paragraph. Every sentence must end with [S#] ids from the list above, "
                f"or say it was \"{NOT_FOUND}\"."
            )
        text = " ".join(best)
        if not text and not replied:
            text = WRITE_FAILED  # an LLM outage is not evidence that the sources are silent
    # Code, not the model, guarantees the fallback wording.
    if not text:
        text = "Not found in sources."
    section: Section = {"index": index, "question": question, "text": text, "source_ids": ids, "errors": errors}
    return {"sections": [section]}


# ---------------------------------------------------------------- Apollo: assemble

ASSEMBLE_SYSTEM = (
    "You are Apollo, finishing a research report. You get the report's sections, which already carry "
    "citations like [S3]. Write a 2-3 sentence introduction and a 2-3 sentence conclusion using ONLY "
    "what the sections say. Every sentence must either reuse the [S#] citations from the sections it "
    "draws on, or explicitly say it summarises the sections (e.g. 'The sections below summarise ...'). "
    "Add no new facts. "
    'Reply with JSON only: {"intro": "...", "conclusion": "..."}'
)
_SUMMARY_PHRASE = (r"\bsummari[sz]e", r"\bsummary\b")


def assemble(state: ResearchState) -> dict:
    events, errors = list(state["events"]), list(state["errors"])
    sections = sorted(state["sections"], key=lambda s: s["index"])
    for s in sections:
        errors.extend(s["errors"])

    # 1. Section bodies, with duplicate sentences removed (first occurrence wins).
    seen: set[str] = set()

    def dedupe(sentences: list[str]) -> list[str]:
        out = []
        for s in sentences:
            key = sentence_key(s)
            if key and key in seen and NOT_FOUND not in s.lower() and s != WRITE_FAILED:
                errors.append(f"assemble: removed duplicate sentence: {s[:100]!r}")
                continue
            seen.add(key)
            out.append(s)
        return out

    bodies = [" ".join(dedupe(split_sentences(s["text"]))) or "Not found in sources." for s in sections]
    section_ids = {i for body in bodies for i in find_citations(body)}

    # 2. Intro and conclusion, limited to ids already cited in the sections.
    intro = conclusion = []
    if section_ids:
        joined = "\n\n".join(f"Section {s['index'] + 1}: {s['question']}\n{b}" for s, b in zip(sections, bodies))
        try:
            obj = parse_json(llm_chat(ASSEMBLE_SYSTEM, f"Topic: {state['topic']}\n\n{joined}", config.WRITE_TEMPERATURE)) or {}
        except Exception as exc:
            obj = {}
            errors.append(f"assemble LLM call failed: {type(exc).__name__}: {str(exc)[:200]}")
        intro = dedupe(enforce_citations(str(obj.get("intro", "")), section_ids, errors, "intro", _SUMMARY_PHRASE))
        conclusion = dedupe(
            enforce_citations(str(obj.get("conclusion", "")), section_ids, errors, "conclusion", _SUMMARY_PHRASE)
        )
    intro_text = " ".join(intro) or "This report summarises what the retrieved sources say about each sub-question below."
    concl_text = " ".join(conclusion) or "In summary, see the sections above; each claim cites the source it comes from."

    # 3. Report body and References (only ids actually cited).
    video = state.get("video") or {}
    title = f"Fact check: {video['title']}" if video.get("title") else state["topic"]  # not a bare link as the title
    parts = [f"# {title}", "## Introduction", intro_text]
    for s, body in zip(sections, bodies):
        parts += [f"## {s['index'] + 1}. {s['question']}", body]
    parts += ["## Conclusion", concl_text]
    report = "\n\n".join(parts)

    by_id = {f["id"]: f for f in state["findings"]}
    cited = sorted({i for i in find_citations(report) if i in by_id}, key=int_id)
    references = [{"id": i, "title": by_id[i]["title"], "url": by_id[i]["url"]} for i in cited]
    unused = len(by_id) - len(cited)
    events.append(f"[Apollo] Report assembled: {len(sections)} sections, {len(cited)} sources cited, {unused} uncited excluded")
    return {"report": report, "references": references, "events": events, "errors": errors}


# ---------------------------------------------------------------- verify


def verify_claims(state: ResearchState) -> dict:
    events, errors = list(state["events"]), list(state["errors"])
    claims = verify_report(state["report"], state["findings"], errors)
    counts = {k: sum(c["status"] == k for c in claims) for k in CLAIM_STATUSES}
    events.append(f"[Verify] {len(claims)} cited claims: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return {"claims": claims, "events": events, "errors": errors}


# ---------------------------------------------------------------- quality checks


def quality_check(state: ResearchState) -> dict:
    """The research-quality rules (tools/quality.py), run before the report is final.

    Checks never block the report: each result is recorded, and the report gains a section that
    states its limits (thin coverage, failed searches, unsupported claims) and every conflict."""
    events = list(state["events"])
    checks = quality_checks(state)
    verdict = overall(checks)
    events.append(f"[Quality] {verdict}: " + ", ".join(f"{c['id']}={c['status']}" for c in checks))
    return {
        "quality": checks,
        "quality_overall": verdict,
        "report": state["report"] + "\n\n" + limits_section(state, checks),
        "events": events,
    }
