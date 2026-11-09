"""Replay mode (LLM_MODE=replay): fixed, offline answers for every external call.

Searches and source APIs, LLM replies, page downloads, publication dates, embeddings and videos all
come from here instead of the network, so a full run needs no keys, spends no quota and makes no
network request. Replay is only ever switched on by configuration (LLM_MODE=replay in .env or a
test); a live run that fails never falls back to it. The database setting is not changed by it.

REPLAY_SCENARIO picks what the source APIs do:
  "ok"            every primary API answers
  "primary_down"  every primary fails (a 503), the fallbacks answer
  "all_down"      both fail: the cache answers if it holds the query, else the domain's graceful error
Every replayed text says it is a replay fixture, so it can never be mistaken for a real source.
"""

import hashlib
import json
import re
import zlib

import numpy as np

from core import config


def active() -> bool:
    return config.LLM_MODE == "replay"


class ReplayOutage(Exception):
    """A simulated server error (503) for the failure scenarios."""

    status_code = 503


def source(name: str, role: str, query: str) -> list[dict]:
    scenario = config.REPLAY_SCENARIO
    if scenario == "all_down" or (scenario == "primary_down" and role == "primary"):
        raise ReplayOutage(f"replay scenario {scenario!r}: {name} is down")
    digest = hashlib.sha256(f"{name}|{query}".encode()).hexdigest()[:10]
    out = []
    for i in range(1, 4):
        # Each sentence names this fixture's own tag, so fixtures from different APIs never look like one
        # article to Athena's duplicate check (tools/quality.same_content).
        tag = f"{name} {digest} {i}"
        text = (f"Replay record {tag} from {name}. Record {tag} concerns {query}. In record {tag}, the {name} "
                f"fixture states finding {i} on this question. Record {tag} is fixed offline test text, "
                f"not a real document.")
        out.append({"title": f"{query} (replay {name} {i})", "url": f"https://replay.invalid/{name}/{digest}/{i}",
                    # api_text False: the text goes through extract.page_content, which labels it "replay"
                    "snippet": text[:300], "raw_content": text, "published": f"2026-01-0{i}", "api_text": False})
    return out


def embed(texts: list[str]):
    """Bag-of-words vectors: texts that share words are similar. Same input, same output."""
    vecs = np.zeros((len(texts), 256))
    for row, text in enumerate(texts):
        for w in re.findall(r"[a-z]+", text.lower()):
            vecs[row, zlib.crc32(w.encode()) % 256] += 1
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.where(norms == 0, 1, norms)


def chat(system: str, user: str, on_text=None) -> str:
    """A fixed reply for each agent's prompt (planner, Hermes, writer, assembler, verifier)."""
    from graph import nodes  # imported here: nodes imports core.llm, which imports this module
    from verify import claims

    if system in (nodes.PLAN_SYSTEM, nodes.VIDEO_PLAN_SYSTEM):
        topic = user.split("\n", 1)[0].removeprefix("Topic:").strip()[:120] or "the topic"
        reply = json.dumps({"sub_questions": [f"What is known about {topic}?",
                                              f"What evidence exists on {topic}?",
                                              f"What are the limits of the evidence on {topic}?"]})
    elif system == nodes.REVIEW_SYSTEM:
        reply = json.dumps({"complete": True, "gaps": []})
    elif system == nodes.WRITE_SYSTEM:
        first = re.search(r"\[(S\d+)\]", user)
        question = re.search(r"Sub-question: (.*)", user)
        q = question.group(1) if question else "this sub-question"
        reply = (f"The replay source reports a finding on {q} [{first.group(1)}]." if first
                 else f"An answer to {q} was not found in sources.")
    elif system == nodes.ASSEMBLE_SYSTEM:
        first = re.search(r"\[(S\d+)\]", user)
        reply = json.dumps({"intro": f"Replay sources describe the topic [{first.group(1)}]." if first else "",
                            "conclusion": "The sections above summarise the evidence."})
    elif system == claims.VERIFY_SYSTEM:
        passages = dict(re.findall(r"^\[(S\d+\.\d+)\] (.+)$", user, flags=re.M))
        label = next(iter(passages), "")
        claim = user.split("\n", 1)[0].removeprefix("Claim: ")
        quote = " ".join(passages.get(label, "").split()[:5])
        reply = json.dumps({"parts": [{"text": claim, "verdict": "supported" if quote else "unsupported",
                                       "passage": label if quote else "", "quote": quote}]})
    else:
        raise RuntimeError("replay mode has no fixed answer for this prompt")
    if on_text:
        on_text(reply)
    return reply
