"""EMBED_BACKEND: "onnx" uses fastembed (no torch) through the same embed() call. Offline: fastembed is faked."""

import sys
import types

import numpy as np
import pytest

from core import config
from tools import embed as embed_mod


@pytest.fixture(autouse=True)
def fresh_model(monkeypatch):
    monkeypatch.setattr(embed_mod, "_model", None)
    embed_mod.clear_cache()
    yield
    embed_mod._model = None
    embed_mod.clear_cache()


def test_onnx_backend_uses_fastembed_and_normalises(monkeypatch):
    seen = {}

    class FakeTextEmbedding:
        def __init__(self, model_name, threads=None):
            seen["model"], seen["threads"] = model_name, threads

        def embed(self, texts, batch_size=256):
            seen["batch_size"] = batch_size
            return iter([np.array([3.0, 4.0]) for _ in texts])  # length 5: must come back as length 1

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    monkeypatch.setattr(config, "EMBED_BACKEND", "onnx")
    vecs = embed_mod.embed(["a", "b"])
    assert vecs.shape == (2, 2) and np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    assert np.allclose(vecs[0], [0.6, 0.8])
    assert seen == {"model": config.EMBED_MODEL, "threads": 2, "batch_size": 8}  # low-memory settings


def test_onnx_backend_embeds_shortest_first_and_keeps_input_order(monkeypatch):
    sent = []

    class FakeTextEmbedding:
        def __init__(self, model_name, threads=None):
            pass

        def embed(self, texts, batch_size=256):
            sent.extend(texts)
            return iter([np.array([float(len(t)), 1.0]) for t in texts])  # vector tells which text it was

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    monkeypatch.setattr(config, "EMBED_BACKEND", "onnx")
    texts = ["a much longer sentence", "short", "medium text"]
    vecs = embed_mod.embed(texts)

    assert sent == ["short", "medium text", "a much longer sentence"]  # less padding per batch
    expected = np.array([[len(t), 1.0] for t in texts])
    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
    assert np.allclose(vecs, expected)  # results come back in the caller's order


def test_default_backend_is_sentence_transformers(monkeypatch):
    class FakeST:
        def __init__(self, name):
            self.name = name

        def encode(self, texts, normalize_embeddings, show_progress_bar):
            return np.ones((len(texts), 3))

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=FakeST))
    monkeypatch.setitem(sys.modules, "fastembed", None)  # importing it would fail: the default must not need it
    monkeypatch.setattr(config, "EMBED_BACKEND", "torch")
    assert embed_mod.embed(["x"]).shape == (1, 3)
    assert isinstance(embed_mod._model, FakeST)


def test_each_text_is_embedded_once_per_run(monkeypatch):
    encoded = []

    class CountingST:
        def __init__(self, name):
            pass

        def encode(self, texts, normalize_embeddings, show_progress_bar):
            encoded.extend(texts)
            return np.array([[float(len(t)), 1.0] for t in texts])

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=CountingST))
    monkeypatch.setattr(config, "EMBED_BACKEND", "torch")
    first = embed_mod.embed(["alpha", "beta"])
    again = embed_mod.embed(["beta", "gamma", "alpha", "beta"])  # repeats and order are kept
    assert encoded == ["alpha", "beta", "gamma"]  # only the new text was computed
    assert np.array_equal(again[0], first[1]) and np.array_equal(again[2], first[0]) and len(again) == 4
    embed_mod.clear_cache()  # a new run starts clean
    embed_mod.embed(["alpha"])
    assert encoded[-1] == "alpha"


def test_keyword_ranking_picks_passages_without_embedding(monkeypatch):
    from tools import passages

    def no_embedding(texts):
        raise AssertionError("keyword ranking must not embed")

    monkeypatch.setattr(passages, "embed", no_embedding)
    monkeypatch.setattr(config, "PASSAGE_RANKING", "keyword")
    filler = [f"Unrelated sentence number {i} about cooking and gardening today." for i in range(20)]
    text = " ".join(filler[:10] + ["The heuristic function estimates the remaining cost to the goal."] + filler[10:])
    out = passages.relevant_passages(text, "What heuristic function does A* use?", 80)
    assert out == "The heuristic function estimates the remaining cost to the goal."


def test_verification_compares_only_top_keyword_sentences_when_limited(monkeypatch):
    from tools.text import top_by_keywords

    sents = ["Cats sleep a lot during the day.", "Dijkstra finds shortest paths in weighted graphs.",
             "Paths and graphs appear in many puzzles.", "Weather was mild."]
    claim = "Dijkstra's algorithm finds shortest paths in graphs."
    assert top_by_keywords(sents, claim, 0) == sents  # default: every sentence
    assert top_by_keywords(sents, claim, 2) == [sents[1], sents[2]]  # best matches, source order kept
