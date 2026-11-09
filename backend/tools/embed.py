"""Shared sentence-embedding model (all-MiniLM-L6-v2).

embed(texts) -> L2-normalised numpy array, so a dot product is cosine similarity.
Used by tools/passages.py (choosing relevant source passages) and
verify/claims.py (claim matching).

Two backends run the SAME model (config.EMBED_BACKEND):
  torch : sentence-transformers + PyTorch (default; what local development uses)
  onnx  : fastembed + ONNX Runtime, no PyTorch. For small servers (e.g. Render free, 512 MB).
          Measured on a saved run: identical vectors (cosine 1.0000), same best-matching
          sentences and the same VERIFY_PARTIAL_SIM decisions as the torch backend.

Threading: the section writers run in parallel threads (LangGraph Send), and
verification runs on yet another thread. Using one torch model from several
threads crashed the process (segmentation fault) and, even when serialised
with a lock, later hung when a different thread called it (torch's internal
thread pool on Windows). So the model lives on ONE dedicated worker thread:
embed() hands the work to that thread and waits for the result. The slow LLM
calls in the writers still run in parallel; only embedding is single-threaded.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from core import config, replay

_model = None
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")


class _OnnxModel:
    """fastembed's TextEmbedding behind the same encode() call as SentenceTransformer.

    Texts are embedded shortest first in batches of 8, then put back in order. A batch is padded to
    its longest text, and ONNX Runtime keeps memory for every batch shape it has seen, so mixed
    lengths in big batches cost both. Measured on 1,100 source sentences (after loading the model):
    batch 32 in input order: +327 MB peak (kept), 25.9 CPU-s; sorted, batch 8: +47 MB, 12.1 CPU-s;
    identical vectors (cosine 1.000000)."""

    BATCH_SIZE = 8
    THREADS = 2

    def __init__(self, name: str):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=name, threads=self.THREADS)

    def encode(self, texts: list[str], normalize_embeddings: bool = True, show_progress_bar: bool = False):
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.array(list(self._model.embed([texts[i] for i in order], batch_size=self.BATCH_SIZE)), dtype=np.float32)
        vecs = np.empty_like(out)
        vecs[order] = out
        if normalize_embeddings and len(vecs):
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs


def _encode(texts: list[str]):
    global _model
    if _model is None:  # only ever touched on the worker thread
        if config.EMBED_BACKEND == "onnx":
            _model = _OnnxModel(config.EMBED_MODEL)
        else:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(config.EMBED_MODEL)
    return _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


# text -> its vector. The same sentences are embedded for Hermes' passages, the section writers and
# claim verification; each is now computed once per run. Same model, same vectors: only the repeat
# work goes away (it dominates on small servers such as Render's 0.1 CPU).
_vectors: dict[str, np.ndarray] = {}
_cache_lock = threading.Lock()
MAX_CACHED = 50_000  # about 75 MB of float32 vectors; cleared at the start of every run anyway


def warm_up() -> None:
    """Load the model in the background (no-op once loaded). Called when a run starts, so the
    2.9 CPU-seconds of loading (about 30 s on Render's 0.1 CPU) overlap planning and search, which
    mostly wait on the network, instead of delaying claim verification."""
    if replay.active():  # replay never loads (or downloads) the model
        return
    _worker.submit(_encode, ["warm up"])


def clear_cache() -> None:
    with _cache_lock:
        _vectors.clear()


def embed(texts: list[str]):
    if replay.active():
        return replay.embed(texts)
    with _cache_lock:
        have = {t: _vectors[t] for t in texts if t in _vectors}
    missing = [t for t in dict.fromkeys(texts) if t not in have]
    if missing:
        new = dict(zip(missing, _worker.submit(_encode, missing).result(timeout=config.EMBED_TIMEOUT_SECONDS)))
        have.update(new)
        with _cache_lock:
            if len(_vectors) + len(new) > MAX_CACHED:
                _vectors.clear()
            _vectors.update(new)
    return np.array([have[t] for t in texts])
