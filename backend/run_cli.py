"""Command-line entry point.

Usage:
    python backend/run_cli.py "Impact of AI on Indian agriculture"
    python backend/run_cli.py "topic" --hitl                 # pause to approve the plan and gap queries
    python backend/run_cli.py "topic" --hitl --no-gap-review # pause only for the plan
    python backend/run_cli.py "topic" --max-rounds 2 --timeout 180 --out-dir output
    python backend/run_cli.py "topic" --doc study.pdf --doc notes.md  # attach documents as sources

Streams the graph and prints each new event. In --hitl mode it asks on the
terminal at each interrupt (Enter = approve). At the end it writes
<out-dir>/report.md, findings.json and contradictions.html, and prints a summary.
"""

import argparse
import sys
from pathlib import Path

from core import config
from graph.workflow import stream_research
from reports.contradictions import write_contradiction_map
from reports.export import write_report
from tools.documents import DocumentUnreadable, read_document
from tools.text import CLAIM_STATUSES


def ask_human(payload: dict):
    """Terminal handler for interrupts. Returns the resume value for the graph."""
    if payload["type"] == "approve_plan":
        print("\n--- Approve the research plan ---")
        for i, q in enumerate(payload["plan"], 1):
            print(f"  {i}. {q}")
        answer = input("Enter = approve, or type replacement sub-questions separated by ' | ': ").strip()
        return {"plan": [q.strip() for q in answer.split("|")]} if answer else {"approved": True}

    print(f"\n--- Approve gap queries for round {payload['round'] + 1} ---")
    for q in payload["gaps"]:
        print(f"  - {q}")
    answer = input("Enter = approve, 's' = skip, or type replacement queries separated by ' | ': ").strip()
    if not answer:
        return {"approved": True}
    if answer.lower() == "s":
        return {"skip": True}
    return {"gaps": [q.strip() for q in answer.split("|")]}


def main() -> int:
    # Windows consoles default to cp1252, which cannot print many characters found in web titles.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Apollo deep research")
    parser.add_argument("topic")
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None, help="research-loop wall-clock timeout in seconds")
    parser.add_argument("--hitl", action="store_true", help="human-in-the-loop: auto_approve=False")
    parser.add_argument("--no-gap-review", action="store_true", help="with --hitl, do not pause for gap queries")
    parser.add_argument("--out-dir", default=config.OUTPUT_DIR)
    parser.add_argument("--doc", action="append", default=[], metavar="FILE",
                        help="attach a PDF, .txt or .md file as a source (repeat for up to 3)")
    args = parser.parse_args()

    docs = []
    for path in args.doc[: config.DOCUMENT_MAX_FILES]:
        try:
            docs.append(read_document(Path(path).name, Path(path).read_bytes()))
        except (OSError, DocumentUnreadable) as exc:
            print(f"Cannot attach {path}: {exc}", file=sys.stderr)
            return 2

    printed = 0
    final = None
    for state in stream_research(
        args.topic,
        max_rounds=args.max_rounds,
        timeout_seconds=args.timeout,
        auto_approve=not args.hitl,
        review_gaps=not args.no_gap_review,
        on_interrupt=ask_human,
        documents=docs,
    ):
        for event in state["events"][printed:]:
            print(event, flush=True)
        printed = len(state["events"])
        final = state

    report_path = write_report(final, args.out_dir)
    conflict_msg = write_contradiction_map(final["contradictions"], final["findings"], Path(args.out_dir) / "contradictions.html")

    print("\n========== SUMMARY ==========")
    print(f"Topic:        {final['topic']}")
    print(f"Status:       {final['status']}")
    print(f"Stop reason:  {final['stop_reason']}")
    print(f"Rounds run:   {final['round_no']} / {final['max_rounds']}")
    print(f"Queries run:  {len(final['searched_queries'])}")
    print(f"Sources:      {len(final['findings'])} collected, {len(final['references'])} cited in report")
    for f in final["findings"]:
        method = f["extracted_by"] or "snippet only"
        print(f"   [{f['id']}] s{f['section'] + 1} {f['title'][:60]} ({len(f['content'])} chars, {method})")
    counts = {k: sum(c["status"] == k for c in final["claims"]) for k in CLAIM_STATUSES}
    print(f"Claims:       {len(final['claims'])} cited sentences -> " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"Conflicts:    {conflict_msg}")
    print(f"Report:       {report_path}")
    print(f"Errors ({len(final['errors'])}):")
    for e in final["errors"]:
        print(f"   ! {e}")
    return 0 if final["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
