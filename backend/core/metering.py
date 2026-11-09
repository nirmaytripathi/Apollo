"""Per-run usage meter: every LLM call and every web search, with timing and tokens.

llm.chat and the API's search wrapper call record(). The run a call belongs to is
found from LangGraph's own run context: api.py uses the run id as the graph's
thread_id, and get_config() returns it inside any node. A call made outside a
graph node (tests, the CLI before a run starts) is filed under "unattributed".

Nothing here prices tokens: providers change prices and free tiers often, so the
UI shows tokens and calls, and a cost only when PRICE_* values are set in .env.
"""

import threading
import time

_lock = threading.Lock()
_calls: dict[str, list[dict]] = {}

UNATTRIBUTED = "unattributed"


def _context() -> tuple[str, str]:
    """(run id, graph node) of the current call, or (UNATTRIBUTED, "") outside a graph node."""
    try:
        from langgraph.config import get_config

        cfg = get_config()
    except Exception:  # not inside a LangGraph runnable
        return UNATTRIBUTED, ""
    run_id = (cfg.get("configurable") or {}).get("thread_id") or UNATTRIBUTED
    node = (cfg.get("metadata") or {}).get("langgraph_node", "")
    return str(run_id), str(node)


def record(kind: str, ok: bool, seconds: float, **fields) -> None:
    """kind: "llm" | "search". fields: provider, model, prompt_tokens, completion_tokens, results, error."""
    run_id, node = _context()
    item = {"kind": kind, "ok": ok, "seconds": round(seconds, 3), "node": node, "ts": int(time.time() * 1000), **fields}
    with _lock:
        _calls.setdefault(run_id, []).append(item)


def calls(run_id: str) -> list[dict]:
    with _lock:
        return list(_calls.get(run_id, []))


def load(run_id: str, items: list[dict]) -> None:
    """Restore a saved run's calls (api.py reloads finished runs from disk)."""
    with _lock:
        _calls[run_id] = list(items)


def forget(run_id: str) -> None:
    with _lock:
        _calls.pop(run_id, None)


def summary(items: list[dict]) -> dict:
    """Totals for one run's calls."""
    llm = [c for c in items if c["kind"] == "llm"]
    search = [c for c in items if c["kind"] == "search"]
    return {
        "llm_calls": len(llm),
        "llm_failed": sum(not c["ok"] for c in llm),
        "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in llm),
        "completion_tokens": sum(c.get("completion_tokens") or 0 for c in llm),
        "llm_seconds": round(sum(c["seconds"] for c in llm), 2),
        "searches": len(search),
        "searches_failed": sum(not c["ok"] for c in search),
        "search_seconds": round(sum(c["seconds"] for c in search), 2),
    }
