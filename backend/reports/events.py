"""Turn LangGraph stream updates into the typed events of the frontend contract.

The frontend never sees raw graph state: api.py feeds every
`graph.stream(..., stream_mode="updates")` chunk through EventConverter and
sends only these events. Each is {"type", "data", "ts"}; ts is epoch milliseconds.

  plan             {sub_questions, awaiting_approval}
  search_started   {round, index (1-based), total, query}; index 1 starts a round's batch
  search_done      {round, query, result_count, sources}   result_count = new sources from this query;
                                                 sources = [{id, title, url}] of those new sources
  search_results   {query, results: [{title, url}]}   live, per query, as soon as the search API answers
                                                 (before pages are read and given [S#] ids)
  search_failed    {round, query, reason}
  review           {round, complete, gaps}        Hermes' verdict after a round
  gaps_pending     {round, gaps}                  waiting for human approval of the gaps
  section_draft    {index, text}               text so far while the LLM writes a section (published live by
                                                 the node, not from a graph update); raw, before citation checks
  section_written  {section, index, citations, not_found, partly_not_found, write_failed, text}   text = final section   write_failed = every LLM failed;
                                                     the section is NOT claimed to be "not found"
  report_written   {markdown}                  full report text, before claim verification
  verification     {verified, partial, contradicted, not_found, unchecked}   claim counts
  quality          {overall, checks: [{id, label, status, detail}]}   research-quality checks before completion
  contradictions   {count, reviews, reviews_failed}   reviews = Hermes reviews run; contradictions
                                                     are only looked for in reviews that succeeded
  report_ready     {}
  warning          {message, details}  NON-terminal: a problem the run survived
                                        (unreadable page, rate limit). Shown amber.
  error            {message}            terminal: the run failed
  complete         {status: "complete" | "failed", stop_reason}

Nodes return their FULL `findings` / `errors` lists, so the converter remembers
how many it has already reported and only turns the new items into events.
"""

import time

from tools.text import CLAIM_STATUSES, NOT_FOUND, WRITE_FAILED, find_citations

NODE_VERB = {
    "apollo_plan": "planning",
    "athena_search": "searching",
    "hermes_review": "reviewing",
    "review_plan": "applying your plan",
    "review_gaps": "applying your decision",
    "assemble": "writing",
    "verify_claims": "verifying claims",
}


def make_event(type_: str, data: dict) -> dict:
    return {"type": type_, "data": data, "ts": int(time.time() * 1000)}


def warning_event(messages: list[str], doing: str) -> dict:
    n = len(messages)
    summary = messages[0][:200] if n == 1 else f"{n} warnings while {doing}"
    return make_event("warning", {"message": summary, "details": messages})


def search_started_events(round_no: int, queries: list[str]) -> list[dict]:
    return [
        make_event("search_started", {"round": round_no, "index": i, "total": len(queries), "query": q})
        for i, q in enumerate(queries, 1)
    ]


class EventConverter:
    def __init__(self) -> None:
        self.n_findings = 0
        self.n_errors = 0
        self.n_searched = 0
        self.round = 1
        self.failed_queries: set[str] = set()
        self.last_plan: list[str] = []
        self.plan_awaiting = False

    # ------------------------------------------------------------ public

    def from_update(self, node: str, update: dict | None) -> list[dict]:
        """Events for one node's update (a dict of the state keys it changed, or None)."""
        if not update:
            return []
        handler = getattr(self, f"_{node}", None)
        events = handler(update) if handler else []
        return events + self._warnings(node, update)

    def from_interrupt(self, payload: dict) -> list[dict]:
        if payload.get("type") == "approve_plan":
            self.plan_awaiting = True
            return [make_event("plan", {"sub_questions": payload["plan"], "awaiting_approval": True})]
        if payload.get("type") == "approve_gaps":
            return [make_event("gaps_pending", {"round": payload["round"], "gaps": payload["gaps"]})]
        return []

    # ------------------------------------------------------------ per node

    def _apollo_plan(self, u: dict) -> list[dict]:
        self.last_plan = list(u["plan"])
        events = []
        # The shared video and attached documents are sources before any search: Athena's counting starts after them.
        self.n_findings = len(u.get("findings", []))
        if u.get("video"):
            events.append(make_event("video", u["video"]))
        docs = [{"id": f["id"], "name": f["title"], "pages": f.get("pages", 0), "truncated": f.get("truncated", False)}
                for f in u.get("findings", []) if f.get("kind") == "document"]
        if docs:
            events.append(make_event("documents", {"documents": docs}))
        return events + [make_event("plan", {"sub_questions": u["plan"], "awaiting_approval": False})]

    def _review_plan(self, u: dict) -> list[dict]:
        events = []
        # Re-announce the plan only when it changes something: approval ends a wait, or the plan was edited.
        if self.plan_awaiting or u["plan"] != self.last_plan:
            events.append(make_event("plan", {"sub_questions": u["plan"], "awaiting_approval": False}))
        self.plan_awaiting, self.last_plan = False, list(u["plan"])
        return events + search_started_events(1, [q["query"] for q in u.get("pending_queries", [])])

    def _athena_search(self, u: dict) -> list[dict]:
        findings = u.get("findings", [])
        new = findings[self.n_findings :]
        self.n_findings = len(findings)
        searched = u.get("searched_queries", [])
        queries = searched[self.n_searched :]  # the queries Athena attempted this round
        self.n_searched = len(searched)
        new_errors = u.get("errors", [])[self.n_errors :]
        self.failed_queries = set()
        events = []
        for q in queries:
            got = [f for f in new if f["query"] == q]
            search_errs = [e for e in new_errors if e.startswith("search[") and repr(q) in e]
            if not got and search_errs:
                self.failed_queries.add(q)
                events.append(make_event("search_failed", {"round": self.round, "query": q, "reason": search_errs[-1]}))
            else:  # 0 is possible: results came back, but every page was already collected earlier
                events.append(make_event("search_done", {
                    "round": self.round, "query": q, "result_count": len(got),
                    "sources": [{"id": f["id"], "title": f["title"], "url": f["url"]} for f in got],
                }))
        return events

    def _hermes_review(self, u: dict) -> list[dict]:
        return [
            make_event(
                "review",
                {"round": self.round, "complete": u.get("hermes_complete", False), "gaps": [g["query"] for g in u.get("gaps", [])]},
            )
        ]

    def _decide(self, u: dict) -> list[dict]:
        if "round_no" in u:  # another round: announce its queries; a stop is reported by "complete"
            self.round = u["round_no"]
            return search_started_events(self.round, [q["query"] for q in u.get("pending_queries", [])])
        return []

    def _write_section(self, u: dict) -> list[dict]:
        return [
            make_event(
                "section_written",
                {
                    "section": s["question"],
                    "index": s["index"],
                    "citations": len(find_citations(s["text"])),
                    # not_found: no cited answer at all; partly_not_found: answered, but some point was not in the sources
                    "not_found": not find_citations(s["text"]) and s["text"] != WRITE_FAILED,
                    "partly_not_found": bool(find_citations(s["text"])) and NOT_FOUND in s["text"].lower(),
                    "write_failed": s["text"] == WRITE_FAILED,
                    "text": s["text"],
                },
            )
            for s in u.get("sections", [])
        ]

    def _assemble(self, u: dict) -> list[dict]:
        return [make_event("report_written", {"markdown": u["report"]})] if u.get("report") else []

    def _verify_claims(self, u: dict) -> list[dict]:
        counts = {k: 0 for k in CLAIM_STATUSES}
        for c in u.get("claims", []):
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        return [make_event("verification", counts)]

    def _quality_check(self, u: dict) -> list[dict]:
        return [make_event("quality", {"overall": u["quality_overall"], "checks": u["quality"]})]

    # ------------------------------------------------------------ helpers

    def _warnings(self, node: str, u: dict) -> list[dict]:
        """New non-fatal errors as ONE warning event per node update.

        Search errors for queries already reported as search_failed are left out (shown once).
        """
        if "errors" not in u:
            return []
        new = u["errors"][self.n_errors :]
        self.n_errors = len(u["errors"])
        if node == "athena_search":
            new = [e for e in new if not (e.startswith("search[") and any(repr(q) in e for q in self.failed_queries))]
        return [warning_event(new, NODE_VERB.get(node, node))] if new else []
