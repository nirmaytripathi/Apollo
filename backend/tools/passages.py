"""Pick the passages of a source that are relevant to a question.

relevant_passages(text, question, budget) -> str

Pages often start with boilerplate (cookie notices, contents, navigation), so
"the first N characters" is a poor sample. Instead: split the source text into
sentences, rank them by relevance to the question, take the best ones until
`budget` characters, and return them IN SOURCE ORDER joined by " … ".

Used for the section writer (question = its sub-question) and for Hermes
(question = the sub-question the source was collected for). The text is still
retrieved source text; this only chooses which part of it the LLM sees.

Ranking (config.PASSAGE_RANKING):
  embedding : cosine similarity of sentence and question embeddings (default).
              If embedding fails, falls back to the first `budget` characters.
  keyword   : words shared with the question (tools.text.keyword_scores). No embedding at all:
              for slow hosts (Render free, 0.1 CPU), where embedding every sentence of every source
              before Hermes' review took minutes. Claim verification still uses embeddings.
"""

import numpy as np

from core import config
from tools import embed as embed_module
from tools.embed import embed
from tools.text import keyword_scores, split_sentences

MIN_SENTENCE_CHARS = 30

# text -> (sentences, sentence vectors). A source is embedded once per run, then reused
# by Hermes in later rounds and by the section writers (only the question is new).
_cache: dict[str, tuple[list[str], object]] = {}


def clear_cache() -> None:
    _cache.clear()
    embed_module.clear_cache()


def _sentences(text: str) -> list[str]:
    return [s for s in split_sentences(text) if len(s) >= MIN_SENTENCE_CHARS]


def relevant_passages(text: str, question: str, budget: int, errors: list[str] | None = None) -> str:
    text = text or ""
    if len(text) <= budget:
        return text
    if config.PASSAGE_RANKING == "keyword":
        sentences = _sentences(text)
        if not sentences:
            return text[:budget]
        order = np.argsort(-np.array(keyword_scores(sentences, question)), kind="stable")  # ties: earlier first
    else:
        try:
            if text not in _cache:
                sentences = _sentences(text)
                _cache[text] = (sentences, embed(sentences) if sentences else None)
            sentences, sent_vecs = _cache[text]
            if not sentences:
                return text[:budget]
            q_vec = embed([question])[0]
        except Exception as exc:
            if errors is not None:
                errors.append(f"passages: embedding failed, using first {budget} chars: {type(exc).__name__}: {str(exc)[:150]}")
            return text[:budget]
        order = (sent_vecs @ q_vec).argsort()[::-1]

    chosen, used = [], 0
    for i in order:
        s = sentences[i]
        if used + len(s) > budget:
            if used == 0:  # a single over-long sentence: take its start
                chosen, used = [i], budget
            continue
        chosen.append(i)
        used += len(s) + 3
    return " … ".join(sentences[i] if len(sentences[i]) <= budget else sentences[i][:budget] for i in sorted(chosen))
