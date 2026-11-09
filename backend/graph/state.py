"""State schema shared by every node in the Apollo LangGraph.

LangGraph passes one dict of this shape between nodes. Each node returns only
the keys it changed; list fields are returned as full new lists, EXCEPT
`sections`, which has an add-reducer because write_section runs in parallel
(one task per plan section) and each task appends its own section.
"""

import operator
from typing import Annotated, Literal, TypedDict


class Finding(TypedDict):
    id: str        # stable citation id: "S1", "S2", ...
    section: int   # index into plan: which sub-question this source was collected for
    query: str     # the query that retrieved it
    title: str
    url: str
    snippet: str   # short text from the search API
    content: str   # extracted page text (capped), "" if extraction failed
    extracted_by: str  # "trafilatura" | "tavily_raw" | "youtube_captions" | "gemini_transcript" | "pdf_text" | "plain_text" | ""
    kind: str      # "primary" (original research / official), "secondary" (rule-based, from the URL), "video" or "document"
    published: str  # the page's own publication date (YYYY[-MM[-DD]]), "" = it states none
    retrieved_at: int  # research date: when Apollo read the page (epoch seconds)


# Finding.section for a source that serves every sub-question: the YouTube video or a document the user gave.
ALL_SECTIONS = -1


class Query(TypedDict):
    query: str
    section: int


class Contradiction(TypedDict):
    source_a: str  # finding id, e.g. "S2"
    source_b: str  # finding id, e.g. "S5"
    claim: str


class Section(TypedDict):
    index: int
    question: str
    text: str             # paragraph with [S#] citations, or "Not found in sources."
    source_ids: list[str]  # ids that were given to the writer
    errors: list[str]     # merged into state["errors"] by assemble


class Reference(TypedDict):
    id: str
    title: str
    url: str


class ClaimPart(TypedDict):
    text: str             # one factual part of the claim
    verdict: Literal["supported", "partial", "contradicted", "unsupported"]
    passage: str          # deciding passage label, e.g. "S3.2" ("" when unsupported)
    source_id: str
    quote: str            # exact words from that passage (checked to be in the source text)
    note: str             # why a verdict was lowered by the code, e.g. a quote not in the source


class Passage(TypedDict):
    label: str            # "S3.2": second passage taken from source S3
    source_id: str
    text: str             # the source sentence
    before: str           # neighbouring sentences, for the reader
    after: str
    similarity: float     # embedding similarity to the claim (0-1)
    position: float | None  # where it starts in the source text (0 = start, 1 = end)
    heading: str          # nearest heading above it in the source text, if any


class ClaimCheck(TypedDict):
    claim: str
    source_ids: list[str]
    status: Literal["verified", "partial", "contradicted", "not_found", "unchecked"]
    best_match: str       # best-matching (or deciding) sentence from the retrieved source text
    similarity: float
    evidence: str         # quote of the first supported part
    parts: list[ClaimPart]       # absent in runs verified before Phase 3.18
    passages: list[Passage]


class ResearchState(TypedDict):
    # Required fields
    topic: str
    plan: list[str]                       # Apollo's sub-questions
    round_no: int                         # current research round (1-based)
    max_rounds: int
    findings: list[Finding]
    gaps: list[Query]                     # Hermes' follow-up queries from the last review
    contradictions: list[Contradiction]
    status: Literal["running", "complete", "failed"]
    events: list[str]                     # human-readable progress log
    errors: list[str]                     # non-fatal errors (failed searches, bad JSON, ...)

    # Loop-control bookkeeping
    pending_queries: list[Query]          # queries Athena runs in the current round
    searched_queries: list[str]           # every query already run (avoid repeats)
    hermes_complete: bool                 # Hermes' last verdict
    started_at: float                     # research clock start (shifted forward by human pauses)
    paused_at: float                      # when the last possible human pause began
    timeout_seconds: int
    stop_reason: str                      # why the loop ended (set by decide)

    # Human-in-the-loop
    auto_approve: bool                    # True: never pause
    review_gaps: bool                     # when not auto_approve: also pause to approve gap queries
    gaps_skipped: bool                    # the human skipped the gap queries

    # Writing and verification (Phase 2)
    sections: Annotated[list[Section], operator.add]
    report: str                           # Markdown body: title, intro, sections, conclusion
    references: list[Reference]           # only ids actually cited in `report`
    claims: list[ClaimCheck]

    # Research-quality rules (tools/quality.py)
    duplicates: list[dict]                # pages dropped as the same article as a kept source: {url, duplicate_of}
    irrelevant: list[dict]                # pages dropped as not about their sub-question: {url, relevance}
    quality: list[dict]                   # checks run before completion: {id, label, status, detail}
    quality_overall: str                  # "passed" | "passed_with_warnings" | "failed" ("" before the check)

    # A YouTube link in the topic (tools/video.py): {id, url, title, channel, extracted_by, language, seconds,
    # truncated}; {} when the topic has none. Its transcript is source S1.
    video: dict

    # Documents the user attached (tools/documents.py): [{name, kind, pages, content, truncated, read_to_page}].
    # Each becomes a source shared by every section, numbered after the video.
    documents: list[dict]


def initial_state(
    topic: str,
    max_rounds: int,
    timeout_seconds: int,
    started_at: float,
    auto_approve: bool = True,
    review_gaps: bool = True,
    documents: list[dict] | None = None,
) -> ResearchState:
    return ResearchState(
        topic=topic,
        plan=[],
        round_no=1,
        max_rounds=max_rounds,
        findings=[],
        gaps=[],
        contradictions=[],
        status="running",
        events=[],
        errors=[],
        pending_queries=[],
        searched_queries=[],
        hermes_complete=False,
        started_at=started_at,
        paused_at=started_at,
        timeout_seconds=timeout_seconds,
        stop_reason="",
        auto_approve=auto_approve,
        review_gaps=review_gaps,
        gaps_skipped=False,
        sections=[],
        report="",
        references=[],
        claims=[],
        duplicates=[],
        irrelevant=[],
        quality=[],
        quality_overall="",
        video={},
        documents=list(documents or []),
    )
