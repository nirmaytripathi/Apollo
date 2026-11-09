"""Builds and compiles the Apollo LangGraph StateGraph.

    START -> apollo_plan -> review_plan* -> athena_search -> hermes_review -> review_gaps* -> decide
                                               ^                                                |
                                               |------------- next round -----------------------|
                                                                                                |
          END <- quality_check <- verify_claims <- assemble <- write_section x N (one per plan section, parallel) <-- finish

    * review_plan / review_gaps call LangGraph interrupt() when auto_approve=False.

A MemorySaver checkpointer stores state between an interrupt and its resume.
stream_research() is the single entry point used by the CLI and tests: it
streams state after every node, and when the graph pauses it calls
`on_interrupt(payload)` and resumes with the returned value.
"""

import time
import uuid
from typing import Callable, Iterator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from core import config

from . import nodes
from .state import ResearchState, initial_state


def build_graph(checkpointer=None):
    g = StateGraph(ResearchState)
    g.add_node("apollo_plan", nodes.apollo_plan)
    g.add_node("review_plan", nodes.review_plan)
    g.add_node("athena_search", nodes.athena_search)
    g.add_node("hermes_review", nodes.hermes_review)
    g.add_node("review_gaps", nodes.review_gaps)
    g.add_node("decide", nodes.decide)
    g.add_node("write_section", nodes.write_section)
    g.add_node("assemble", nodes.assemble)
    g.add_node("verify_claims", nodes.verify_claims)
    g.add_node("quality_check", nodes.quality_check)

    g.add_edge(START, "apollo_plan")
    g.add_edge("apollo_plan", "review_plan")
    g.add_edge("review_plan", "athena_search")
    g.add_edge("athena_search", "hermes_review")
    g.add_edge("hermes_review", "review_gaps")
    g.add_edge("review_gaps", "decide")
    g.add_conditional_edges("decide", nodes.route_after_decide, ["athena_search", "write_section"])
    g.add_edge("write_section", "assemble")  # runs once, after every write_section task finishes
    g.add_edge("assemble", "verify_claims")
    g.add_edge("verify_claims", "quality_check")
    g.add_edge("quality_check", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


def stream_research(
    topic: str,
    max_rounds: int | None = None,
    timeout_seconds: int | None = None,
    auto_approve: bool = True,
    review_gaps: bool = True,
    on_interrupt: Callable[[dict], object] | None = None,
    documents: list[dict] | None = None,
) -> Iterator[ResearchState]:
    """Yield the full state after every node. The last yielded state is final.

    With auto_approve=False the graph pauses at review_plan (and review_gaps if
    review_gaps=True); `on_interrupt` receives the interrupt payload and returns
    the resume value.
    """
    if not auto_approve and on_interrupt is None:
        raise ValueError("auto_approve=False needs an on_interrupt handler")
    graph = build_graph()
    run_config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": config.GRAPH_RECURSION_LIMIT}
    payload: object = initial_state(
        topic,
        max_rounds=max_rounds or config.MAX_ROUNDS,
        timeout_seconds=timeout_seconds or config.TIMEOUT_SECONDS,
        started_at=time.time(),
        auto_approve=auto_approve,
        review_gaps=review_gaps,
        documents=documents,
    )
    while True:
        yield from graph.stream(payload, run_config, stream_mode="values")
        snapshot = graph.get_state(run_config)
        if not snapshot.interrupts:
            return
        payload = Command(resume=on_interrupt(snapshot.interrupts[0].value))


def run_research(topic: str, **kwargs) -> ResearchState:
    final = None
    for final in stream_research(topic, **kwargs):
        pass
    return final
