"""Compare the claim verifier against hand labels: the current verifier (claims split into parts,
quotes checked) and the one before it (one yes/no per source, "partial" = a similar sentence).

    .venv\\Scripts\\python backend\\scripts\\eval_verifier.py output\\eval\\verifier_set.json output\\eval\\verifier_labels.json [provider:model]

verifier_set.json: claims sampled from saved runs, each with the text of the sources it cites
(build it from your own runs; it is not committed). verifier_labels.json: for each claim id a
label and the statuses accepted as correct, items to exclude, and synthetic claims (a real claim
altered to be false) that test whether a verifier catches false statements.

Both verifiers use the same embeddings and the same model (the one given, else the first model of
VERIFY_CHAIN that has an API key), one claim at a time. A claim whose model call failed is
"unchecked"; read the printed errors before the scores. Uses real LLM calls: about 1-3 per claim per verifier.
Results go to output/eval/results-<time>.json and are summarised on screen.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core import config  # noqa: E402
from core.llm import chat as llm_chat  # noqa: E402
from tools.embed import embed  # noqa: E402
from tools.jsonparse import parse_json  # noqa: E402
from tools.text import find_citations, strip_citations, top_by_keywords  # noqa: E402
from verify import claims as current  # noqa: E402

# ---------------------------------------------------------------- the previous verifier, frozen as a baseline

V1_SYSTEM = (
    "You check whether a source text supports a claim. Judge ONLY from the source text given; "
    "do not use outside knowledge. If the text states the claim (or a clear paraphrase), supported=true. "
    "If it only discusses the topic, contradicts it, or gives different numbers, supported=false. "
    'Answer JSON only: {"supported": true|false, "evidence": "<short quote from the source text, or empty>"}'
)
V1_PARTIAL_SIM = 0.5


def verify_v1(claim: str, findings: list[dict], model: tuple[str, str]) -> dict:
    by_id = {f["id"]: f for f in findings}
    ids = [i for i in find_citations(claim) if i in by_id]
    claim_vec = embed([strip_citations(claim)])[0]
    similarity, supported, failed = 0.0, False, 0
    for sid in ids:
        sents = top_by_keywords(current._source_sentences(by_id[sid]), strip_citations(claim), config.VERIFY_MAX_SENTENCES)
        if not sents:
            continue
        sims = embed(sents) @ claim_vec
        similarity = max(similarity, float(sims.max()))
        top = sorted(sims.argsort()[::-1][: config.VERIFY_TOP_K].tolist())
        window = " ... ".join(sents[i] for i in top)
        try:
            raw = llm_chat(V1_SYSTEM, f"Claim: {strip_citations(claim)}\n\nSource text ({sid}): {window}",
                           temperature=0.0, provider=model[0], model=model[1])
        except Exception:
            failed += 1
            continue
        obj = parse_json(raw)
        if obj and obj.get("supported") is True:
            supported = True
            break
    if supported:
        return {"status": "verified"}
    if failed and failed == len(ids):
        return {"status": "unchecked"}
    return {"status": "partial" if similarity >= V1_PARTIAL_SIM else "not_found"}


def verify_v2(claim: str, findings: list[dict], model: tuple[str, str]) -> dict:
    orig = current._available_chain
    current._available_chain = lambda: [model]
    try:
        errors: list[str] = []
        result = current.verify_report("# T\n\n" + claim, findings, errors)[0]
    finally:
        current._available_chain = orig
    return {"status": result["status"], "parts": [(p["verdict"], p["text"], p["note"]) for p in result["parts"]],
            "errors": errors}


# ---------------------------------------------------------------- run and score


def main(set_path: str, labels_path: str, model_arg: str = "") -> None:
    items = {i["id"]: i for i in json.load(open(set_path, encoding="utf-8"))}
    spec = json.load(open(labels_path, encoding="utf-8"))
    model = tuple(model_arg.split(":", 1)) if model_arg else current._available_chain()[0]
    cases = []
    for cid, lab in spec["labels"].items():
        if "exclude" not in lab:
            cases.append({"id": cid, "kind": "real", "claim": items[cid]["claim"], "findings": items[cid]["findings"], **lab})
    for syn in spec["synthetic"]:
        base = items[syn["from"]]
        cases.append({"kind": "synthetic", "findings": base["findings"], **syn})
    print(f"{len(cases)} claims ({sum(c['kind'] == 'real' for c in cases)} real), model {model[0]}:{model[1]}")

    for c in cases:
        # EVAL_SKIP_V1=1 saves quota when only the current verifier changed (the baseline is fixed).
        c["v1"] = "skipped" if os.getenv("EVAL_SKIP_V1") else verify_v1(c["claim"], c["findings"], model)["status"]
        v2 = verify_v2(c["claim"], c["findings"], model)
        c["v2"], c["v2_parts"], c["v2_errors"] = v2["status"], v2["parts"], [e[:160] for e in v2["errors"]]
        print(f"  {c['id']:12s} label {c['label']:12s} v1 {c['v1']:10s} v2 {c['v2']:12s}"
              f"{'' if c['v2'] in c['accept'] else '  <- v2 off'}{'  ' + c['v2_errors'][0] if c['v2_errors'] else ''}")

    def score(rows: list[dict], key: str) -> str:
        ok = sum(r[key] in r["accept"] for r in rows)
        false_ver = sum(r[key] == "verified" and "verified" not in r["accept"] for r in rows)
        neg = sum("verified" not in r["accept"] for r in rows)
        missed = sum(r["label"] == "verified" and r[key] != "verified" for r in rows)
        pos = sum(r["label"] == "verified" for r in rows)
        unchecked = sum(r[key] == "unchecked" for r in rows)
        return (f"correct {ok}/{len(rows)} | said 'verified' for a claim not fully backed: {false_ver}/{neg} | "
                f"backed claim not verified: {missed}/{pos} | unchecked {unchecked}")

    print("\nSummary (a status counts as correct if it is in the claim's 'accept' list)")
    for name, rows in (("real", [c for c in cases if c["kind"] == "real"]),
                       ("synthetic", [c for c in cases if c["kind"] == "synthetic"]), ("all", cases)):
        if not os.getenv("EVAL_SKIP_V1"):
            print(f"  {name:9s} v1: {score(rows, 'v1')}")
        print(f"  {name if os.getenv('EVAL_SKIP_V1') else '':9s} v2: {score(rows, 'v2')}")

    out = os.path.join(os.path.dirname(set_path), f"results-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump([{k: v for k, v in c.items() if k != "findings"} for c in cases], open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\nPer-claim results: {out}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
