"""Live progress from inside graph nodes (e.g. a section's text while the LLM is still writing it).

Graph updates reach the browser only when a node FINISHES. For things worth showing earlier, a
node calls publish(type, data); api.py registers a sink per run that turns it into an SSE event.
The run is found from LangGraph's own config (api.py uses the run id as thread_id), the same way
metering.py attributes calls. Outside a registered run (tests, the CLI) publish() does nothing.

Stopping a run: api.py registers a threading.Event per run; LLM and search calls call raise_if_stopped(), so
once the user presses Pause the node in progress fails fast instead of finishing its remaining calls.
"""

import threading
from typing import Callable

_lock = threading.Lock()
_sinks: dict[str, Callable[[str, dict], None]] = {}
_stops: dict[str, threading.Event] = {}
_paused: dict[str, float] = {}  # seconds each run has spent paused by its user (Pause / Resume)


class RunStopped(Exception):
    """The user stopped this run (Pause). Raised by LLM and search calls inside it."""


def _current_run() -> str | None:
    try:
        from langgraph.config import get_config

        run_id = (get_config().get("configurable") or {}).get("thread_id")
    except Exception:  # not inside a graph node
        return None
    return str(run_id) if run_id else None


def register_stop(run_id: str, event: threading.Event) -> None:
    with _lock:
        _stops[run_id] = event


def unregister_stop(run_id: str) -> None:
    with _lock:
        _stops.pop(run_id, None)
        _paused.pop(run_id, None)


def add_paused(run_id: str, seconds: float) -> None:
    """Record time a run spent paused by its user, so it does not count against the research time limit."""
    with _lock:
        _paused[run_id] = _paused.get(run_id, 0.0) + max(0.0, seconds)


def paused_seconds() -> float:
    """Total user-pause time of the run this call belongs to (0 outside a run)."""
    run_id = _current_run()
    if run_id is None:
        return 0.0
    with _lock:
        return _paused.get(run_id, 0.0)


def raise_if_stopped() -> None:
    """Raise RunStopped when the run this call belongs to was stopped. Outside a run it does nothing."""
    run_id = _current_run()
    if run_id is None:
        return
    with _lock:
        event = _stops.get(run_id)
    if event is not None and event.is_set():
        raise RunStopped("stopped by the user")


def register(run_id: str, sink: Callable[[str, dict], None]) -> None:
    with _lock:
        _sinks[run_id] = sink


def unregister(run_id: str) -> None:
    with _lock:
        _sinks.pop(run_id, None)


def publish(type_: str, data: dict) -> None:
    run_id = _current_run()
    if run_id is None:
        return
    with _lock:
        sink = _sinks.get(run_id)
    if sink:
        sink(type_, data)
