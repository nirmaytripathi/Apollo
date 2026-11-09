"""Acceptance check: every [S#] in report.md maps to a real finding with a URL.

Usage:  python backend/scripts/check_citations.py [output_dir]

Checks, and prints every failure:
1. each [S#] cited anywhere in report.md exists in findings.json and has a URL;
2. each cited id is listed in the References section;
3. each id in References is actually cited in the report body (no uncited ids).
Exit code 0 when there are zero failures.
"""

import json
import re
import sys
from pathlib import Path

CITE = re.compile(r"\[(S\d+)\]")
REF_LINE = re.compile(r"^- \[(S\d+)\] .*<(\S+)>$")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
    report = (out / "report.md").read_text(encoding="utf-8")
    findings = {f["id"]: f for f in json.loads((out / "findings.json").read_text(encoding="utf-8"))}

    body, _, rest = report.partition("\n## References\n")
    refs_block = rest.split("\n## Claim verification summary")[0]
    refs = {m.group(1): m.group(2) for line in refs_block.splitlines() if (m := REF_LINE.match(line.strip()))}
    cited = set(CITE.findall(body))
    everywhere = set(CITE.findall(report))

    failures = []
    for sid in sorted(everywhere, key=lambda s: int(s[1:])):
        f = findings.get(sid)
        if f is None:
            failures.append(f"{sid}: cited but not in findings")
        elif not f.get("url", "").startswith("http"):
            failures.append(f"{sid}: finding has no valid URL")
    for sid in cited - refs.keys():
        failures.append(f"{sid}: cited in body but missing from References")
    for sid in refs.keys() - cited:
        failures.append(f"{sid}: in References but never cited in body")
    for sid, url in refs.items():
        if sid in findings and findings[sid]["url"] != url:
            failures.append(f"{sid}: References URL differs from finding URL")

    print(f"Citations in body: {len(cited)} unique ids; References entries: {len(refs)}; findings: {len(findings)}")
    print(f"Uncited findings excluded from References: {len(findings.keys() - cited)}")
    for line in failures:
        print(f"FAIL {line}")
    print(f"Failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
