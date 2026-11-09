"""Markdown export.

write_report(state, out_dir) writes:
- report.md     : the report body, a References list (only cited ids), and a
                  "Claim verification summary" table with each claim's status.
- findings.json : every collected finding, including the retrieved text, so
                  scripts/check_citations.py can confirm each [S#] maps to a real URL
                  and anyone can audit the text a claim was verified against.
"""

import json
from pathlib import Path

from tools.text import CLAIM_STATUSES


def _cell(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def render_markdown(state: dict) -> str:
    lines = [state["report"], "", "## References", ""]
    for r in state["references"]:
        lines.append(f"- [{r['id']}] {r['title'] or r['url']}. <{r['url']}>")
    if not state["references"]:
        lines.append("_No sources were cited._")

    claims = state.get("claims", [])
    counts = {k: sum(c["status"] == k for c in claims) for k in CLAIM_STATUSES}
    lines += [
        "",
        "## Claim verification summary",
        "",
        "Each cited sentence was checked against the retrieved text of the source(s) it cites. "
        f"verified: {counts['verified']}, partial: {counts['partial']}, not_found: {counts['not_found']}"
        + (f", contradicted: {counts['contradicted']}" if counts["contradicted"] else "")
        + (f", unchecked: {counts['unchecked']}" if counts["unchecked"] else "")
        + ".",
        "",
        "| # | Claim | Sources | Status | Best-matching source text |",
        "|---|---|---|---|---|",
    ]
    for n, c in enumerate(claims, 1):
        lines.append(
            f"| {n} | {_cell(c['claim'])} | {', '.join(c['source_ids'])} | {c['status']} | {_cell(c['best_match'], 120)} |"
        )
    return "\n".join(lines) + "\n"


def write_report(state: dict, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text(render_markdown(state), encoding="utf-8")
    keys = ("id", "section", "query", "title", "url", "extracted_by", "snippet", "content")
    findings = [{k: f[k] for k in keys} for f in state["findings"]]
    (out / "findings.json").write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
