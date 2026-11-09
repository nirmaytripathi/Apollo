"""Fakes shared by the test files: LLM, web search, page extraction.

fake_llm routes each call by its system prompt, so one fake serves every agent:
Apollo's planner, Hermes, the section writer, the assembler, and the verifier.
"""

import json
import re
import zlib

import numpy as np

from graph import nodes
from verify import claims as claims_mod


def fake_embed(texts):
    """Bag-of-words vectors: texts sharing words get high cosine similarity. Stable across runs."""
    vecs = np.zeros((len(texts), 256))
    for row, text in enumerate(texts):
        for w in re.findall(r"[a-z]+", text.lower()):
            vecs[row, zlib.crc32(w.encode()) % 256] += 1
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.where(norms == 0, 1, norms)


PLAN = ["q one", "q two", "q three"]


def default_writer(user: str) -> str:
    """Cite the first source given; also try an invented id and an uncited sentence."""
    first = re.search(r"\[(S\d+)\]", user).group(1)
    question = re.search(r"Sub-question: (.*)", user).group(1)
    return f"The source reports a finding about {question} [{first}]. A made-up fact [S99]. An uncited opinion."


def default_assembler(user: str) -> str:
    first = re.search(r"\[(S\d+)\]", user).group(1)
    return json.dumps({"intro": f"Sources describe the topic [{first}].", "conclusion": "The sections above summarise the evidence."})


def verify_reply(user: str, verdict: str = "supported", label: str | None = None) -> str:
    """The verifier's answer: the whole claim is one part, judged on a passage it was shown.

    The quote is the first words of that passage, so it passes the code's quote check."""
    passages = dict(re.findall(r"^\[(S\d+\.\d+)\] (.+)$", user, flags=re.M))
    label = label or next(iter(passages), "")
    claim = user.split("\n", 1)[0].removeprefix("Claim: ")
    quote = " ".join(passages.get(label, "").split()[:5]) if verdict != "unsupported" else ""
    return json.dumps({"parts": [{"text": claim, "verdict": verdict, "passage": label if quote else "", "quote": quote}]})


def fake_llm(review_reply, writer=default_writer, assembler=default_assembler, verifier=None, plan=PLAN):
    """`review_reply(call_no)` returns Hermes' raw text for its n-th call."""
    calls = {"review": 0}

    def _chat(system, user, temperature=0.2, model=None, on_text=None):
        if system in (nodes.PLAN_SYSTEM, nodes.VIDEO_PLAN_SYSTEM):
            return "```json\n" + json.dumps({"sub_questions": plan}) + "\n```"
        if system == nodes.REVIEW_SYSTEM:
            calls["review"] += 1
            return review_reply(calls["review"])
        if system == nodes.WRITE_SYSTEM:
            reply = writer(user)
            if on_text:  # stream like a real model: a partial text, then the whole reply
                on_text(reply[: len(reply) // 2])
                on_text(reply)
            return reply
        if system == nodes.ASSEMBLE_SYSTEM:
            return assembler(user)
        if system == claims_mod.VERIFY_SYSTEM:
            return verifier(user) if verifier else verify_reply(user)
        raise AssertionError(f"unexpected system prompt: {system[:60]}")

    return _chat


def fake_search(empty_for=()):
    def _search(query, errors):
        if query in empty_for:
            errors.append(f"search returned no results for {query!r}")
            return []
        slug = query.replace(" ", "-")
        return [{"title": f"{query} {i}", "url": f"https://ex.com/{slug}/{i}", "snippet": "s"} for i in range(4)]

    return _search
