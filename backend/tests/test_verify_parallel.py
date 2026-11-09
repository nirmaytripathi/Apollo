"""Claim verification runs several claims at once: same results and order, shared model chain handled safely."""

import threading

from core import config
from verify import claims as claims_mod

from .fakes import fake_embed, verify_reply


class QuotaSpent(Exception):
    status_code = 429

    def __str__(self):
        return "Rate limit reached on tokens per day (TPD)"


FINDINGS = [
    {"id": f"S{i}", "content": f"Fact number {i} says the river is {i} kilometres long. Other text about boats here."}
    for i in range(1, 9)
]
REPORT = "# T\n\n" + " ".join(f"Fact number {i} says the river is {i} kilometres long [S{i}]." for i in range(1, 9))


def test_parallel_checks_keep_order_and_drop_a_spent_model_once(monkeypatch):
    calls, lock = [], threading.Lock()

    def fake_llm(system, prompt, temperature=0.0, model=None, provider=None):
        with lock:
            calls.append(model)
        if model == "spent":
            raise QuotaSpent()
        return verify_reply(prompt)

    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", fake_llm)
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "spent"), ("gemini", "ok")])
    monkeypatch.setattr(config, "VERIFY_WORKERS", 4)
    errors: list[str] = []

    results = claims_mod.verify_report(REPORT, FINDINGS, errors)

    assert [r["source_ids"] for r in results] == [[f"S{i}"] for i in range(1, 9)]  # report order kept
    assert all(r["status"] == "verified" for r in results)
    assert sum("quota exhausted" in e for e in errors) == 1  # removed once, not once per thread
    assert calls.count("ok") == 8


def test_progress_is_published_per_claim(monkeypatch):
    seen = []
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user, "unsupported"))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])
    monkeypatch.setattr(claims_mod.live, "publish", lambda type_, data: seen.append((type_, data)))
    claims_mod.verify_report(REPORT, FINDINGS, [])
    assert [t for t, _ in seen] == ["claim_checked"] * 8
    assert sorted(d["done"] for _, d in seen) == list(range(1, 9)) and all(d["total"] == 8 for _, d in seen)
