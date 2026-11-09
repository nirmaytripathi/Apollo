# Apollo — Phase Log

## Phase 1 — CLI research loop (2026-10-31)

### Built
| File | Purpose |
|---|---|
| `config.py` | Loads `.env`; keys, LLM provider, `MAX_ROUNDS=2`, `TIMEOUT_SECONDS=180`, retry limits, caps |
| `llm.py` | One `chat()` wrapper; Groq or Gemini via `LLM_PROVIDER`; HTTP timeout + bounded retries |
| `graph/state.py` | `ResearchState` TypedDict (all required fields + loop bookkeeping) |
| `graph/nodes.py` | `apollo_plan`, `athena_search`, `hermes_review`, `decide`, `route_after_decide` |
| `graph/workflow.py` | StateGraph: START → plan → search → review → decide → (search \| END) |
| `tools/search.py` | Tavily → Serper fallback; never raises, logs to `errors` |
| `tools/extract.py` | requests + trafilatura, capped at 4000 chars, `""` on failure |
| `tools/jsonparse.py` | Safe JSON parser (strips code fences, finds `{...}`) |
| `tools/retry_policy.py` | tenacity policy: retry transient errors only (timeouts, 429, 5xx) |
| `run_cli.py` | Streams events, prints final summary |
| `tests/test_phase1.py` | 14 offline tests (LLM/search/extract faked) |

### Loop termination (enforced in code)
`decide` stops on, in order: Hermes `complete=true` → `round_no >= max_rounds` → wall-clock timeout →
no new gap queries. Athena also checks the timeout between queries. LangGraph `recursion_limit=25` is a
hard backstop. Every network call has an HTTP timeout and at most N retries.

### Verified
- `pytest tests/test_phase1.py` → **14 passed**. Covers: stops at max_rounds with round-2 gap queries
  searched; invalid Hermes JSON; all searches empty; one sub-question empty while others continue; search
  exception logged; retry limit (transient 3 attempts, permanent 1); timeout; plan fallback; Hermes
  contradictions citing unknown source ids are dropped; JSON parser.
- **Check 3 (live):** `TAVILY_API_KEY=invalid-key-for-test python run_cli.py "Impact of AI on Indian agriculture"`
  hit the real Tavily API, got `InvalidAPIKeyError`, logged it, and finished cleanly in 1.3s with `status=failed`.
- **Check 4:** verified with a fake search (`test_empty_result_for_one_subquestion_continues`).
- **Change after the first run:** a missing or invalid key was being retried with backoff (14.8s wasted).
  Added `retry_policy.py` so permanent errors fail at once. The same run now takes 1.3s.
- **Check 1 (live):** `python run_cli.py "Impact of AI on Indian agriculture"` → 5 sub-questions, 14 sources,
  Hermes `complete=true` after round 1, `status=complete`, `stop_reason=hermes_complete`, 111s, exit 0.
- **Check 2 (live):** `python run_cli.py "Measured yield impact of AI crop advisory apps for smallholder farmers in Odisha and Bihar"`
  → Hermes returned 3 gap queries, `[Apollo] Starting round 2 with gap queries: [...]`, all 3 searched (+3
  sources S10–S12), stopped `max_rounds_reached` at round 2/2, 55.8s, exit 0.

### Fixes made during live checks
- `llama-3.3-70b-versatile` returned 404 (not available to this Groq key). Default changed to
  `openai/gpt-oss-120b`, after listing the models the key can access.
- The CLI crashed printing a title containing `ﬁ` on the Windows cp1252 console. `run_cli.py` now writes UTF-8.
  The pipeline itself was not affected; the crash was in printing only.
- Real keys had been typed into `.env.example`. They were moved to `.env` (gitignored) and the placeholders
  restored. Nothing was committed.

### Known gaps / decisions
- `status` is `complete` or `failed` (no findings at all). The reason is in `stop_reason`
  (`hermes_complete`, `max_rounds_reached`, `timeout`, `no_follow_up_queries`).
- If Hermes returns invalid JSON, the run ends with `no_follow_up_queries`: there are no gaps to search.
- Hermes reviews snippets, not full page text. It may mark a sub-question as covered when the full page
  says something different.
- Extraction was weak in the first live runs (3/14 and 7/12 pages with text). It was fixed in Phase 1.1 below.
- Hermes judged check 1 "complete" although most of its sources had snippets only. Its verdict is only as
  good as the snippets it sees.
- LLM output is nondeterministic: the same topic can stop after round 1 on one run and reach round 2 on another.
- No report writing yet (Phase 2).

## Phase 1.1: extraction fix (2026-10-31)

### Root causes (measured on 17 live pages)
- Our trafilatura fetch got text from 8/17 pages. The failures were PDFs (trafilatura reads HTML only),
  publisher 403s, and social-media pages.
- One PDF download took 21.7s with `REQUEST_TIMEOUT_SECONDS=15`: `requests`' timeout limits the gap between
  packets, not the whole download.
- Pages were extracted one after another (47s for 17 pages).
- Tavily's own `raw_content` had text for 11/17 pages, including PDFs and some sites that refused us.
  Our fetch or Tavily together covered 13/17.

### Changes
| File | Change |
|---|---|
| `tools/search.py` | Tavily called with `include_raw_content="text"`; results carry `raw_content` (Serper: `""`) |
| `tools/extract.py` | Check Content-Type and skip non-HTML before downloading; stream with total deadline + 3 MB cap; extracts under 200 chars count as failed; new `page_content()` falls back to Tavily text |
| `graph/nodes.py` | `athena_search` assigns ids S1.. in order, then extracts the round's pages in parallel (6 threads) |
| `graph/state.py` | Finding has `extracted_by`: `trafilatura` / `tavily_raw` / `""` (snippet only) |
| `config.py` | `MIN_CONTENT_CHARS`, `MAX_PAGE_BYTES`, `EXTRACT_WORKERS` |
| `run_cli.py` | Prints the extraction method per source |

Still not done: no browser User-Agent spoofing, no getting around 403s.

### Verified
- `pytest tests/test_phase1.py` → **17 passed**. New tests: fallback to search-provider text, non-HTML skipped
  without downloading the body, IDs stay S1..S9 in order when extraction finishes in random order.
- Live: the PDF that took 21.7s is now skipped in 1.4s.
- Live check 1 again: 20 sources, **16 with page text** (6 trafilatura, 10 tavily_raw), 2 rounds,
  `hermes_complete`, **53.4s** (was 111s for one round), exit 0.
- Live check 2 again: 15 sources, **12 with page text**, round 2 reached with 3 gap queries,
  `max_rounds_reached`, 49.4s, exit 0.

### Known gaps
- About 20% of sources are still snippet-only (ResearchGate, some Facebook posts, some PDFs).
- **Source quality, not extraction:** Facebook posts and videos are getting in. One had 387 chars of
  `tavily_raw` text, which is probably page clutter rather than content. Some results are off-topic
  (a CB Insights company page, an app demo page). Phase 2 should filter or down-rank these before writing.
- `tavily_raw` text is collapsed plain text and may include navigation junk; trafilatura text is cleaner.

## Phase 2: cited report, verification, contradiction map, HITL, export (2026-10-31)

### Built
| File | Purpose |
|---|---|
| `graph/state.py` | Finding gets `section`; new `sections` (add-reducer, for parallel writers), `report`, `references`, `claims`, HITL flags |
| `graph/nodes.py` | Hermes gaps carry a section number; `review_plan` / `review_gaps` interrupts; `write_section` (one per section via `Send`); `assemble`; `verify_claims` |
| `graph/workflow.py` | MemorySaver checkpointer; `stream_research(..., auto_approve, review_gaps, on_interrupt)` drives pause/resume |
| `tools/text.py` | Sentence splitting, citation normalising (`[S1, S3]` → `[S1][S3]`), "not found in sources" handling |
| `verify/claims.py` | all-MiniLM-L6-v2 picks the best-matching source sentences; LLM yes/no → verified / partial / not_found |
| `viz/contradictions.py` | NetworkX + pyvis HTML (JS inlined); "No conflicts found" page when empty |
| `export.py` | `report.md` (report, References, claim table) + `findings.json` (incl. source text for auditing) |
| `scripts/check_citations.py` | Acceptance check 2 |
| `llm.py`, `tools/retry_policy.py` | Per-call model; `reasoning_effort=low`; 429 waits for `retry-after` (cap 30s) |

### Rules enforced in code (not only prompts)
- A writer gets only its section's findings. Citation ids not in that input are removed. Sentences with no
  citation are dropped, except "not found in sources" sentences. The writer gets one retry with feedback when
  a sentence was dropped. A section with no sources gets "Not found in sources." without calling the LLM.
- "The sources do not specify X" style sentences get the exact phrase appended. The pattern requires the
  sources to be the subject, so an uncited factual negative ("AI apps do not raise yields") is still dropped (tested).
- References are built only from ids cited in the final text. Duplicate sentences are removed across the report.
- Intro and conclusion may cite only ids already cited in the sections, or must say they summarise.
- Verification reads retrieved text only, using `VERIFY_MODEL=openai/gpt-oss-20b` (separate Groq
  rate-limit bucket from the writer).
- Human pause time is added back to the research clock, so approving slowly does not trigger the timeout (tested).

### Acceptance checks
1. **Pass.** Live `run_cli.py "Impact of AI on Indian agriculture"` → `output/check1/report.md` with intro,
   5 sections, conclusion, inline [S#], References. Exit 0 in every run (5 runs, 84–132s).
2. **Pass.** `scripts/check_citations.py` → `Failures: 0` on every live run; it also catches an injected
   `[S42]` (tested).
3. **Pass (mechanically).** Counts printed every run, e.g. verified=4 / partial=8 / not_found=0, and
   9 / 3 / 0 on the alcohol topic.
4. **Half done.** "No conflicts found" page generated (`output/check1/contradictions.html`). The conflict
   graph works with a contradiction (unit test checks the vis-network HTML), but **no live run produced a
   contradiction**: 0 across intermittent fasting, alcohol & heart, and India population peak. Inspection
   showed the snippets Hermes sees contain no head-on conflicts (cookie banners, reference lists, table
   headers). Hermes' prompt was sharpened once; it still found none, which is correct for its input.
5. **Pass.** `printf '\n\n\n' | python run_cli.py "..." --hitl` paused after the plan, resumed on approval,
   paused for gap queries, resumed, completed (status complete, 2 rounds).
6. **Pass.** `pytest tests/` → **39 passed** (17 Phase 1 + 22 Phase 2).

### Bugs found and fixed while running the live checks
- Tavily text was flattened to one line (Phase 1.1 bug), so sentence splitting failed on those pages. Line breaks are now kept.
- Honest "not found in the provided sources" sentences were being dropped. Variants are now normalised.
- Verifier saw only best match ±1 sentence and missed support ranked 4th–5th. It now sees the top 5 matches.
- Writer often omitted citations. Added one retry with feedback.

### Known gaps (most important first)
- **Report quality varies between runs** (3–11 sources cited, verified share 11–75%). Root cause: the writer
  sees only the first 1200 chars of each source, often boilerplate. Proposed fix: send each source's most
  relevant passages to the writer, chosen with the embedding model already in use.
- **Many claims are "partial".** The diagnosis showed the writer adding detail that is not in its sources
  (e.g. "a private-sector driven program"). The verifier is right to flag it.
- **Contradiction detection is limited by snippets** (see check 4). Proposed fix: give Hermes a short relevant
  excerpt per source. This changes Phase 1's "titles + snippets only" rule, so it needs a decision.
- Social-media and junk sources are not filtered yet.
- Groq free tier: 8k tokens/min and 1000 requests/day per model. One run is about 30–50 requests.

## Phase 2.1: relevant passages for writer and Hermes; stability fixes (2026-10-31)

### Changed
| File | Change |
|---|---|
| `tools/embed.py` (new) | Shared MiniLM model, owned by ONE dedicated worker thread (see "Bugs" below); 120s timeout per call |
| `tools/passages.py` (new) | `relevant_passages(text, question, budget)`: rank a source's sentences by similarity to the question, keep the best in source order; cached per source text; falls back to first N chars if embedding fails |
| `graph/nodes.py` | Writer gets each source's most relevant 1200 chars for its sub-question; Hermes gets the most relevant 400 chars per source instead of the search snippet (approved change to the Phase 1 "snippets only" rule) |
| `config.py` | `EXTRACT_MAX_CHARS` 4000 → 12000 (LLMs now see selected passages, not the page start); `HERMES_CHARS_PER_SOURCE`, `EMBED_TIMEOUT_SECONDS` |
| `tools/retry_policy.py` | A 429 whose `retry-after` exceeds 30s (a daily quota) is not retried; `is_quota_exhausted()` |
| `verify/claims.py` | Falls back from `gpt-oss-20b` to `gpt-oss-120b` when a daily quota is spent; if both are spent, the remaining claims are `unchecked` and no further LLM calls are made |
| `tests/conftest.py` (new) | Blanks all API keys during tests and clears the passage cache |

### Bugs found and fixed
1. **The tests were calling the real Groq API.** `verify/claims.py` holds its own `llm_chat` reference, which the
   test fixture did not replace. Each test run sent hundreds of real verification calls. This used up
   gpt-oss-20b's **200,000 tokens/day** limit and made the suite take 80–110s. Fix: the fake now covers it, and
   conftest blanks the keys. The suite now takes **~10s** and makes no network calls.
2. **Segmentation fault / deadlock.** The parallel section writers ran the torch model from several threads at
   once. A lock around encode() stopped the crash, but the run still hung later when the verify thread used the
   model. Confirmed with faulthandler stack dumps. Fix: a single worker thread owns the model. Stress-tested with
   the real model: 5 writer threads, then a separate thread, no hang.
3. **A spent daily quota looked like a hang.** Each verify call waited out 2 × 30s of retries and then failed
   anyway (40+ min per run). Fixed by points 2 and 3 of "Changed" above.

### Verified (live)
- `pytest tests/` → **45 passed in ~10s**, all offline.
- **Check 4 (conflict half): pass.** "Is moderate alcohol consumption good or bad for heart health?" → Hermes
  found 2 conflicts between 3 sources; `output/check4/contradictions.html` written (701 KB, JS inlined). Checked by
  hand against the source text:
  - **S4 vs S5 is a genuine conflict** (both quoted verbatim): S4 says moderate drinking's effect "as cardiovascular protector is
    considerable"; S5 says "even low consumption of beer, cider, or distilled spirits was associated with a 9%
    higher risk of cardiovascular death".
  - **S4 vs S11 is a false positive.** S11's "can directly damage heart muscle cells" comes from a passage about
    heavier drinking (a scope difference).
- Same run: 15 sources cited, **16 of 17 claims verified**, 1 partial (before passages: 1–5 verified out of 9–15).
- Check 2 on `output/check1` and `output/check4`: `Failures: 0`.
- Detection varies between runs: on the same topic, Hermes found contradictions in 3 of 5 runs with passages
  (0 of 4 runs with snippets only).
- Final check-1 run (after both Groq daily token quotas ran out): exit 0 in 233s; all 12 claims `unchecked`, with
  the reason logged. The circuit breaker worked as designed.

### Known gaps
- **Groq free-tier daily token limit (200k per model)** is the real constraint. One full run uses roughly
  30–60k tokens. Today's quota is spent, mostly by the test leak. For the demo: wait for the reset, or add
  Gemini (key already in .env) as a third verification fallback.
- **The research timeout can be overrun:** 209.6s against 180s in one run. The timeout is checked between steps,
  so a slow LLM call (rate limits) can run past it. The overrun is bounded by request timeouts × retries.
- Hermes still produces false positives (scope differences) and misses some conflicts. It is still one LLM
  judgement over excerpts.
- Junk and social-media sources are still not filtered.

## Phase 2.2: Gemini as a verification fallback (2026-10-31)

### Changed
| File | Change |
|---|---|
| `llm.py` | `chat(..., provider=None)`: provider per call, one cached client per provider |
| `config.py`, `.env.example` | `VERIFY_CHAIN` (default `groq:openai/gpt-oss-20b,groq:openai/gpt-oss-120b,gemini:gemini-3.1-flash-lite`); `GEMINI_MODEL` default → `gemini-3.1-flash-lite` |
| `verify/claims.py` | Walks `VERIFY_CHAIN` (providers without a key are skipped). A model with a spent daily quota is dropped for the rest of the report; when none are left, the remaining claims are `unchecked` |
| `tools/retry_policy.py` | Reads the HTTP status for all SDKs (Gemini `.code`, Groq `.status_code`, requests). Every 4xx except 429 is permanent, so a Gemini 404 is no longer retried. Reads Gemini's `retryDelay` from the error body. A daily quota = the error names a per-day quota, or the wait is > 120s. Per-minute waits are honoured up to 60s |

### Why gemini-3.1-flash-lite
`gemini-2.5-flash` (the old default) now returns 404 "no longer available to new users". Probed live:

| Model | Result |
|---|---|
| `gemini-flash-latest` | 504 timeout |
| `gemini-3-flash-preview` | answered "Red wine is red ⇒ wine is red" as **unsupported** (wrong) |
| `gemini-3.1-flash-lite` | 1.1s, correct |

A pinned name was chosen over the `-latest` aliases, which can change silently.

### Verified
- `pytest tests/` → **47 passed** (new: fallback through all three models then `unchecked`; providers without a key
  skipped; Gemini per-minute 429 retried with its 37s delay; per-day 429 not retried; 404 not retried).
- Live, with both Groq daily quotas spent and `LLM_PROVIDER=gemini` set as an environment override for the run:
  the log shows `quota exhausted for groq:openai/gpt-oss-20b; switching to groq:openai/gpt-oss-120b`, then
  `…; switching to gemini:gemini-3.1-flash-lite`. Result: **verified 16 / partial 1 / not_found 1 / unchecked 0**;
  15 sources cited; 183s; check 2 `Failures: 0`.

### Known gaps
- **Self-checking bias.** In that run, Gemini both wrote and verified the report. A spot check found one lenient
  "verified" (the best match was only a heading). Even the default setup uses two gpt-oss models (120b writes,
  20b verifies). An independent verifier from a different model family would be stronger.
- To demo while Groq's quota is spent, set `LLM_PROVIDER=gemini` in `.env`. Gemini's free tier has its own
  per-minute and per-day limits; a 429 is waited out (up to 60s), or the model is dropped if its daily quota is spent.

## Phase 3: web app (FastAPI + SSE, React + Vite + TypeScript) (2026-11-01)

### Built
| File | Purpose |
|---|---|
| `api.py` | FastAPI backend. In-memory run registry with a lock; the graph runs in a background thread; SSE events with ids (resume via `Last-Event-ID`) and a 15 s heartbeat; resume endpoint for plan/gap decisions; report, report.md download, contradiction map, demo topics, cache clear. A run failure becomes an `error` event + `complete`, never a 500 |
| `events.py` | `EventConverter`: LangGraph `updates` chunks → 12 typed events. The UI never sees graph state |
| `frontend/` | Vite 8 + React 19 + TS 7 + Tailwind 4 + react-markdown. 7 components: TopicForm, ProgressFeed, ApprovalPanel (plan + gaps), ReportView (citations, hover cards, verification markers, download), SourcesTable, VerificationTable, ContradictionsView |
| `demo/topics.md`, `DEMO.md`, `README.md` | Demo topics, 2-minute script, setup |
| `tests/test_phase3_api.py` | 9 offline API tests: event order over real SSE, failed search, HITL edit + skip, reject, graph exception → error event, validation, Last-Event-ID resume, demo topics + cache clear, demo switch |

### Decisions (agreed or flagged)
- **Max rounds 1–2, not 1–3.** The hard constraint "max 2 research rounds" wins over the Phase 3 spec; `max_rounds=3` gets a 422.
- **7 components instead of 9.** PlanApproval + GapApproval became one ApprovalPanel; DownloadButton is inside
  ReportView. All the specified behaviour is kept.
- **Topic B demo switch** (agreed with the user). Live search never returned empty, even for nonsense or 1,188-character
  queries. `APOLLO_DEMO_FAIL_SUBQUESTION=N` makes the N-th sub-question's search fail, **only for the exact Topic B
  text**, through Athena's normal error path, with the warning "simulated outage (demo switch …)". Off by default.
- **Phases 1–2 frozen.** `git diff 76c5d46 -- graph tools verify viz export.py llm.py config.py run_cli.py` is empty.
  The API only calls `build_graph`, `initial_state`, `write_report`, `write_contradiction_map`; the demo switch wraps
  `nodes.web_search` at runtime.

### Acceptance checks
1. **Pass.** `uvicorn api:app` started; `curl POST /api/runs` (Topic A) → `{"run_id": …}`; `curl -N …/events` → 25 events,
   ids 0–24 strictly in order, 15 heartbeats, the stream closed after `complete`, and the run reached round 2. (That
   run's LLM calls hit Groq's daily token limit; it degraded honestly, with amber warnings and "not found in sources".)
2. **Pass.** `npm run build` (`tsc -b && vite build`): no TypeScript errors; also after a clean `npm ci` from the lockfile.
3. **Pass.** Browser, Topic A: feed shows "Round 1…", "Hermes found 3 gap(s) in round 1", "Round 2…", "Apollo stops:
   maximum number of rounds reached"; report rendered with 8 clickable citations and hover cards; Sources tab has 21 rows.
4. **Pass.** Browser, HITL: plan pending → edited sub-question 2 → `search_started` round 1 contained the edited text →
   `gaps_pending` → Approve all → round 2 searched exactly those gaps → complete.
5. **Pass.** Browser, Topic B: amber "No results for <sub-question 2>" (simulated outage), run complete, section 2
   = "Not found in sources.", other sections cited normally.
6. **Pass.** Download: 200, `content-disposition: attachment; filename="apollo-report-….md"`; the citation checker
   reports `Failures: 0` on the downloaded file.
7. **Pass.** `pytest tests/` → 56 passed (47 Phase 1–2 + 9 Phase 3); `npm run build` passes.
8. **Pass after fixing the doc.** Followed DEMO.md from fresh PowerShell terminals. Two wrong steps were fixed: (a) the
   edited question was not visible in the feed when the script says so, so the feed now lists each round's queries;
   (b) tab 3 waits for gap approval after the demo, so the script now says to approve it.

### Bugs found by running it (fixed)
- Starting a new run briefly paired the new run id with the previous run's events and fetched a report too early
  (amber "report not ready"). Events are now stored with their run id.
- Hover cards overflowed the page (horizontal scroll); they now open leftwards.
- Some search results use a 215-character URL as their "title", which stretched the Sources table to 1,384 px. Fixed
  with a fixed table layout and `overflow-wrap:anywhere`.
- The demo buttons' accessible name was the long topic; they now have `aria-label` "Demo: Topic A/B".
- The Verification tab implied that all cited sources matched; it now says "best match among S1, S2" (Phase 2 does not record which).

### Known gaps
- **LLM quotas decide what a demo looks like.** Groq's 200k tokens/day per model ran out during testing; the live
  checks ran with `LLM_PROVIDER=gemini GEMINI_MODEL=gemini-flash-lite-latest` (env override), and
  `gemini-3.1-flash-lite` returned 503 "high demand" for part of the session.
- Round 1 page downloads (15 s timeouts) can use up most of the 180 s budget; one run skipped some round-2 queries at
  the timeout. Raise `TIMEOUT_SECONDS` for demos.
- The contradiction-map iframe was not exercised live in Phase 3: no live Phase 3 run produced a conflict. The
  "no conflicts" path was seen live; the map HTML itself was verified in Phase 2.1.
- The Phase 2 verifier marked a "…not found in sources [S3]…" sentence as verified (lenient); frozen logic, recorded here.
- Runs live in memory (lost on restart); localhost only; no auth, no database (as specified).


## Phase 3.1: 13-page navigation with real data (2026-10-31)

**Asked:** sidebar navigation with 13 working sections: Dashboard, New Research, Analytics, Agent Orchestration,
Research Planning, Claims & Evidence, Contradictions, Source Intelligence, Research Gaps, Security, Cost & Execution,
Verification, Final Reports.

**Data behind each page (nothing is sample data):**
- New backend endpoints: `GET /api/runs` (run list with counts), `GET /api/runs/{id}/metrics` (usage meter and stage
  timings), `GET /api/system` (key presence, .env checks, secret scan of tracked files, limits). `/report` now also
  returns `plan`, `rounds`, `errors`, `sections` and `citation_check`.
- `metering.py`: `llm.chat` records every attempt (provider, model, tokens as the provider reports them, time, error);
  the API's search wrapper records every query. Calls are filed by run and graph node using LangGraph's own config
  (`thread_id` = run id), so concurrent runs do not mix.
- Finished runs are saved to `output/runs/<id>/run.json` and reloaded at startup, so history, analytics and reports
  survive a restart.
- Change to Phase 1 code: `llm.py` only (metering hook; behaviour unchanged). Graph, tools and verifier untouched.

**Checks**
1. `pytest` → 62 passed (3 new: meter attribution per run/node with no invented cost; run list + reload from disk;
   `/api/system` never returns key values).
2. `npm run build` (tsc strict + vite) passes.
3. Live run, Topic B, Groq, demo switch 2: 21 sources, 17 claims (6 verified, 10 partial, 1 not found), 50 LLM calls /
   28.4k tokens metered, 8 searches with the 1 simulated failure; 30 inline citations, 0 dangling. All 13 pages
   opened in the browser with this run; 375 px width has no horizontal scroll.
4. Restarting the server kept both runs (list, report, metrics).

**Bugs found while checking (fixed)**
- Stage timings treated events as stage starts; events fire when a node finishes, so planning showed 0 s. Now each
  event closes the stage it names; stages add up to the wall time (150 s run: 2.2 + 25.4 + 30.8 + 16.5 + 4.7 + 15.6 + 54.7).
- "Inline citations" showed distinct ids (`find_citations` de-duplicates); it now counts every occurrence.
- Long topics overflowed into the next column in the Dashboard and Analytics tables.
- A run-list error from a backend restart stayed on screen after the backend came back.

**Known gaps**
- A Gemini run during testing got 503 "high demand" on 15 of 16 calls; it still completed, with 0 claims. The Cost
  page made the cause visible. Groq was used for the check above.
- No live run produced a contradiction, so the Contradictions page was seen only in its empty state live.
- Runs made before metering show 0 tokens/searches.

## Phase 3.2: APOLLO light UI migration (2026-10-31)

**Asked:** replace the presentation layer with the APOLLO interface (light scientific theme, token-based design
system, sidebar with status/AI core/user, top bar with live monitor, richer modules per section) without changing
backend behaviour or inventing data. No target files were attached, so the written spec was the source of truth.

**What changed**
- `index.css`: design tokens (#ECEEF1 / #F5F6F7 / #FFFFFF, ink #111318, muted #68717D, blue #4C9AFF / #3B82F6,
  warning #F0B74B, error #EF6673, line/hair borders). Blue text uses `accent-ink` #1F5FC8 because the spec blues are
  below 4.5:1 on white. Print stylesheet for "PDF (print)".
- `ui.tsx` primitives: Panel, PageHeader, Stat, Pill (status always written out), StackedBar, Legend, Meter,
  BarChart (+ screen-reader table), Segmented, Confirm, Unavailable, NeedsRun, `bucketize`, `download`.
- `insights.ts`: classifies the run's own error lines (blocked/paywalled, PDF skipped, no main text, uncited sentence
  removed, invalid citation removed, quota switch...) and a domain-name heuristic for source category.
- Router: `#/<page>?run=<id>` deep links (share link); existing `#/<page>` URLs unchanged.
- Backend: one additive field, `similarity` on each `/report` verification row (already in graph state).
- Rejecting the plan (HITL panel) and Terminate now ask for confirmation.

**Shown as unavailable instead of invented:** academic search, credibility/authority/originality scores, publication
dates, AI-generated-content and prompt-injection detection, conflict classification, false-premise detection, pause /
mid-run cancel, per-run source/agent/priority/strictness settings, document upload, category field, platform
benchmarks, DOCX export.

**Checks**
1. `npm run build` (tsc strict + vite) passes; `pytest` 62 passed.
2. Every page opened with the saved Topic B run; 375 px: no horizontal scroll on all 13 pages; 768 px drawer works.
3. Deep link `#/reports?run=<older id>` opened that run.
4. Live HITL run: plan appeared on Research Planning, Terminate → confirm → run `rejected / plan_rejected_by_user`.

**Bugs found while checking (fixed):** floated legend broke the depth selector; report section numbers started at 2
(StrictMode double render with a counter); session selector collapsed on phones; nav drawer ignored Escape;
live badge truncated the nav label.

## Phase 3.3: "0 contradictions" vs "not checked" (2026-10-31)

**Question:** most runs show 0 contradictions: broken or expected?
**Findings:** detection works: Hermes found a planted conflict (18% yield gain vs no change) on the first call; no saved
run dropped a contradiction. 0 is mostly expected on descriptive topics (sources complement each other); Hermes also
sees only ~400 chars per source, chosen per sub-question, with a strict prompt, so recall is limited and varies.
**Bug fixed:** when every Hermes review failed (run b4caa31b: Gemini 503), the UI still said "No conflicts found".
- Backend: `contradictions` event, `/report` (`review_health`) and `/api/runs` rows now carry `reviews` and
  `reviews_failed` (counted from Hermes failure lines in state errors; works for saved runs too).
- Frontend: `conflictCheck()` → checked / partial / not_checked. Contradictions page, event feed, report section,
  Agents, Research Gaps and the Verification checklist show "Not checked" instead of 0.
**Checks:** `pytest` 64 passed (2 new); `npm run build` passes; b4caa31b shows "Not checked (1 of 1 review failed)",
73181002 shows "No conflicts found … 2 of 2 reviews ran".

## Phase 3.4: LLM fallback chain; "not written" is not "not found" (2026-10-31)

**Reported:** "it runs only 1 round and the answer says not found, but the references are fine" (run d8b0c604,
"what is A star algorithm").
**Root cause (from the usage meter):** plan and Hermes calls succeeded, then all 5 parallel `write_section` calls got
Groq 429 for gpt-oss-120b (quota nearly spent; small requests still passed). Planning, review and writing had no model
fallback (only verification did), and `write_section` wrote "Not found in sources." when the LLM failed, which is
false: the 14 sources were fine. The single round was Hermes judging the sources sufficient (`hermes_complete`):
`max_rounds` is a cap, not a target.
**Fix**
- `config.LLM_CHAIN` (default LLM_PROVIDER's model first, then the others; `LLM_CHAIN` in .env overrides) and
  `llm.chat` walks it when no model is pinned; a model whose daily quota is spent cools down for
  `LLM_COOLDOWN_SECONDS` (600). Pinned calls (verification's own chain) are unchanged. Meter keeps 400 error chars.
- `tools.text.WRITE_FAILED`: a section no model could write says so; NOT_FOUND stays reserved for "sources do not
  answer". `section_written` events and `/report` sections carry `write_failed`; feed, Planning, Verification
  (new checklist item) and Gaps show "not written".
**Checks:** `pytest` 70 passed (6 new in `tests/test_llm_chain.py`); `npm run build` passes. Live rerun of the same
query: Groq 120b and 20b both 429 → Gemini wrote all 5 sections; 2 rounds; 21 claims, 19 verified, 2 partial.
**Note:** saved run d8b0c604 still says "Not found in sources" (recorded before the fix).

## Phase 3.5: Live Report: the report builds on screen while the agents work (2026-10-31)

**Asked:** do not make the user wait for a separate finished report; open a new page that streams it.
**Backend (additive events, frozen contract fields unchanged)**
- `live.py`: nodes publish mid-step events to the run's SSE stream (run found via LangGraph's thread_id).
- `llm.chat(on_text=...)` streams replies (Groq `stream=True`, Gemini `generate_content_stream`; token usage from
  the final chunk). `write_section` publishes `section_draft {index, text}` at most every 0.25 s.
- New events: `search_results {query, results}` (raw hits per query, from the API's search wrapper),
  `report_written {markdown}` (full text before claim verification). `section_written` gains `text`;
  `search_done` gains `sources [{id,title,url}]`. Drafts are not saved to run.json.
- Default chains gain `gemini-flash-lite-latest` (`GEMINI_FALLBACK_MODEL`), a second Gemini model with its own
  capacity, after `gemini-3.1-flash-lite` returned 503 "high demand" during testing.
**Frontend:** `#/live` (Live Report). Launching from Dashboard / New Research opens it; the top-bar Live button
opens it (it replaces the live-monitor drawer). It shows: plan headings → web hits per sub-question → numbered
sources → each section streaming as a labelled draft, then its final cited text → full report while claims are
verified → the verified report with counts, in place. Stage strip, timer, sources column, optional event log.
**Checks:** `pytest` 73 passed (3 new: drafts precede final sections and full text precedes verification; drafts not
saved; search hits precede search_done). `npm run build` passes. Live (Focused, "binary search…"): first web hits at
3.3 s (previously the first sources appeared at ~26 s), sections at ~53 s, full text at ~59 s while verification ran
until ~2m48s; 18 of 20 claims verified; no horizontal scroll at 375 px.
**Known:** Groq's daily quota was spent (~198.8k/200k) during testing, so these runs used Gemini, which streams in
few large chunks: drafts appear in bursts rather than word by word.

## Phase 3.6: contradiction map fix; accounts with JWT (2026-10-31)

**Graph not showing (fixed).** The pyvis map rendered its own "Contradiction map" heading twice and a fixed 650 px
canvas inside a 520 px iframe, so the nodes sat off-screen (run 4f27084c showed one node's edge). Now: no in-map
heading, the canvas fills the frame (100vh, no margins/scroll), larger nodes, red 3 px edges with readable labels and
a 320 px edge length. `/api/runs/{id}/contradictions` rebuilds the map from the saved state, so old runs get the fix.
**Accounts.** `auth.py`: register / login with scrypt-hashed passwords (stdlib) in SQLite (`AUTH_DB`), JWT HS256
sessions (`PyJWT==2.10.1`, `JWT_SECRET`, `JWT_TTL_HOURS`=12) in an HttpOnly SameSite=Strict cookie (`Bearer` also
accepted), generic "Incorrect email or password", 5 failed logins per email per 15 min → 429. Endpoints
`/api/auth/register|login|logout|me`; every other route requires a session. Runs carry an `owner`; other users get 404
for them; ownerless (pre-accounts) runs stay visible to all. Frontend: sign-in / create-account screen, 401 anywhere
returns to it, sidebar shows the user with Sign out. A random `JWT_SECRET` was added to the local `.env`.
**Checks:** `pytest` 85 passed (12 in `tests/test_auth.py`; API tests now use a signed-in client); `npm run build`
passes. In the browser: unauthenticated `/api/runs` → 401; short-password error; register → dashboard; sign out →
sign-in screen; wrong password → generic error; sign in; reload keeps the session; the map iframe loads with the
cookie; `document.cookie` is empty (token not readable by scripts). The test account was deleted afterwards.
**Not done:** email verification, password reset, account deletion UI, admin roles.

## Phase 3.7: database (Neon Postgres or local SQLite); simpler sidebar footer (2026-10-31)

**Asked:** add a database for deploying on Neon; remove the sidebar's system status / backend lines and the line
under the user's name.
**Database (`db.py`, SQLAlchemy 2.0.36 Core + psycopg 3.2.3):** `DATABASE_URL` (Neon `postgresql://…?sslmode=require`;
the scheme is pointed at psycopg 3) or, when empty, `output/apollo.db` (SQLite). Tables `users` and `runs` are created
on first use; `runs.doc` (JSONB on Postgres) holds events, final state and usage calls. NUL characters are stripped
before saving (Postgres JSON rejects them). Pooled Postgres connections are pre-pinged and recycled every 5 min (Neon
closes idle connections). `auth.py` and api.py persistence now use the database; `report.md` is rendered from the stored
state, and the contradiction map was already rebuilt from it, so a redeploy loses nothing. At startup the older
`output/users.db` and `output/runs/*/run.json` are imported once (password hashes kept; run owners remapped to the new
user ids). The Security page shows the database type (never the URL).
**Sidebar:** removed "System status" and "AI core"; the footer shows initials, the user's name (email on hover) and
Sign out.
**Checks:** `pytest` 91 passed (6 new in `tests/test_database.py`: Neon URL handling, round trip without drafts or NUL,
report download with no local files, one-time idempotent import with the old password still valid); `npm run build`
passes. Local start imported 1 account and 10 runs into `output/apollo.db`; the dashboard lists them after sign-in.
**Not verified:** a live Neon database (no `DATABASE_URL` was provided); the Postgres path is covered by SQLAlchemy and
the URL test only.

## Phase 3.8: a new account no longer sees other people's (imported) runs (2026-10-31)

**Reported:** after signing in or signing up, the dashboard showed runs imported from before accounts existed.
**Cause:** `_visible()` let every signed-in user see ownerless runs (10 imported runs had `owner IS NULL`).
**Fix:** a run is visible only to its owner; ownerless runs are hidden from everyone (404 like a missing run).
`scripts/claim_runs.py` lists ownerless runs and, with `--email`, assigns them all to that account (restart the
backend afterwards). No new dependencies.
**Checks:** `pytest` 92 passed (ownerless runs hidden from two accounts; claim script assigns only ownerless runs and
rejects an unknown email).

## Phase 3.9: no sample content that looks like another user's history (2026-10-31)

**Reported:** a user should see only their own searches, nothing that looks like someone else's recent history.
**Change:** removed the dashboard's "Suggested research topics" panel and the New Research DEMO A/B buttons (demo
fixtures from `demo/topics.md`, with developer notes), and replaced the objective placeholder (an old demo topic that
read like earlier input) with "Type your research question". With Phase 3.8 (runs visible only to their owner), a
fresh account now shows 0 sessions and an empty form. `/api/demo-topics` still exists for the demo script but is not
used by the UI.
**Checks:** `npm run build` passes; `pytest` 92 passed; in the browser the dashboard text shows 0 sessions and no
suggestions, and New Research has no DEMO buttons.

## Phase 3.10: agent strip and research form fit the space they are given (2026-10-31)

**Reported:** the agent strip in the Dashboard's narrow "Live intelligence" column was cramped (APOL… / ATHE… / HERM…,
labels cut off). It chose its columns from the window width, so a wide window forced four columns into ~340 px.
**Fix:** container queries (Tailwind 4 `@container`): below 36rem one full-width row per agent and Round | Elapsed as
two equal cells; from 36rem three equal agent cards with the stats below; from 56rem three cards plus a joined stats
column. The research form's settings grid now also switches to two columns by its own width (the Deep option was cut
off in the narrow New Research column).
**Checks:** `npm run build` passes; checked at 1440 px (Dashboard side column: three rows + two equal stats; New
Research: wide layout) and in the ~640 px pane (medium layout).

## Phase 3.11: free deployment (Hugging Face Spaces + Neon) (2026-10-31)

**Added (nothing existing changed in behaviour):** `Dockerfile` (Node 22 builds the frontend; Python 3.11 with
CPU-only torch; embedding model downloaded at build time; uvicorn on 7860 as user 1000), `.dockerignore` (`.env`,
`output/`, `*.db` excluded), README front matter for Spaces, `DEPLOY.md` (step-by-step), `scripts/migrate_to_db.py`
(copies the local SQLite accounts and runs into `DATABASE_URL`, password hashes kept, owners remapped, re-runnable),
and in `api.py` a static mount of `frontend/dist` on `/`, registered after every `/api` route and only when the build
exists. Local development is unchanged (Vite on 5173 proxies `/api`).
**Checks:** `pytest` 93 passed (new: migration into a target that already has another account, run twice); with the
backend on 8000, `/` serves the app (sign-in screen), `/assets/*.js` 200, `/api/auth/me` and `/api/runs` 401, Vite
dev on 5173 still works.
**Not verified:** the Docker build itself (Docker is not installed on this machine) and a live Neon database.

## Phase 3.12: torch-free build for Render's free plan (2026-10-31)

**Why:** Hugging Face now charges for Docker Spaces; Oracle's free VM needs a card. Render's free web service needs no
card but has 512 MB RAM, too little for PyTorch.
**Tried and rejected:** Gemini embeddings. The free tier allows 100 embedded texts per minute
(`EmbedContentRequestsPerMinute…FreeTier = 100`, each text in a batch counts); a run embeds about 2,000 sentences.
**Change (default behaviour unchanged):** `EMBED_BACKEND` in config (`torch` default; `onnx` = fastembed 0.9.1 running the
same all-MiniLM-L6-v2 through ONNX Runtime, batch 32, 2 threads). `requirements-render.txt` (no torch /
sentence-transformers / pytest), `Dockerfile.render` (sets `EMBED_BACKEND=onnx`, `MALLOC_ARENA_MAX=2`, pre-downloads the
ONNX model, listens on `$PORT`), `EXTRACT_WORKERS` now overridable by env (default 6). DEPLOY.md: Render (A) and
Oracle (B).
**Checks:** `pytest` 95 passed (2 new: onnx backend uses fastembed with low-memory settings and normalises; the default
backend never imports fastembed). In a separate torch-free venv: ONNX vs torch on a saved run's 15 claims × 150
sentences: vector cosine 1.0000, identical best-matching sentences and partial decisions; app + 2,000 sentences peak
301 MB; a real Focused run completed (15 sources, 5/5 sections, 17 claims: 6 verified, 11 partial).
**Risk:** full-run peaks of 457–540 MB on Windows (working set). Not measured on Linux (no Docker/WSL here); Render's
512 MB may be too small. A second test run was slow only because Groq's daily quota was spent and Gemini returned 503s.

## Phase 3.13: runs no longer stall in Review on Render (2026-10-31)

**Reported:** on Render the run gets stuck at the Review stage.
**Cause (from saved runs' events and meter):** Hermes' LLM call takes 1–18 s, but 8–54 s passed between the last
search and the review event on a laptop: building Hermes' prompt embeds every sentence of every source (~1,100) to pick
passages. On Render's 0.1 CPU that is minutes. Verification then re-embedded most of the same sentences.
**Fix:** (1) `tools/embed.py` caches each text's vector for the run (cleared at run start; thread-safe), so no sentence is
embedded twice; same vectors, everywhere. (2) `PASSAGE_RANKING=keyword` (passages chosen by shared words, no embedding)
and (3) `VERIFY_MAX_SENTENCES=N` (each claim compared with its N best keyword matches per source); both default off and
are set only in `Dockerfile.render` (keyword, 30).
**Measured on a saved 21-source run (ONNX):** local settings embed 1,124 sentences before the review and 1,179 in total
(the cache removed verification's repeats); Render settings embed 0 before the review and 398 in total. Verification:
same partial/not-found decision for 17/17 claims, same best-matching sentence for 15/17. Hermes' excerpts differ for
18/21 sources (keyword vs embedding pick different sentences), so Render's coverage verdicts can differ from local ones.
**Checks:** `pytest` 98 passed (new: embed cache computes each text once and keeps order; keyword ranking never embeds;
verification keyword limit keeps the best matches in source order).

## Phase 3.14: brief outages and server restarts no longer break the page (2026-10-31)

**Reported (Render):** "run list request failed (502)" and "Lost the live connection to the backend". The backend
process restarted mid-run (out of memory on 512 MB, a redeploy, or waking from sleep); the page gave up at once and the
running run vanished (it was only saved when finished).
**Backend:** a run is saved as "running" when it starts; at startup a saved run still "running" becomes failed with an
`error` ("interrupted because the server restarted… start it again") and `complete {stop_reason: "interrupted"}`,
saved so it is reported once. The event stream also resumes from `?last_event_id=`. The keep-alive is now a named
`ping` event every 15 s (an SSE comment is invisible to page scripts).
**Frontend:** `useRunEvents` reconnects by itself (1 s doubling to 10 s) from the last event id; before each attempt it
checks the run: fewer events on the server than received means the server restarted, so the stored ending is replayed;
404 means interrupted. A watchdog treats 40 s without any message or ping as a dead connection (a proxy can keep a dead
socket open). It reports a lost connection only after 2 minutes of failures. The run list ignores up to 2 failed polls
in a row; then it says "Can't reach the server right now. Retrying automatically." `ApiError` carries the HTTP status;
502/503/504/unreachable are retried quietly. On load, a 502 while the free server wakes shows "Waking up the server…"
for up to 90 s instead of the sign-in screen. The top bar shows "reconnecting…"; the live report says "Reconnecting to
the server…".
**Checks:** `pytest` 100 passed (new: an interrupted run is reported once across two restarts; the stream resumes after
`?last_event_id=` with nothing repeated or missed). Simulated on a temporary database: killing the backend mid-run showed
"reconnecting…" after ~45 s with no error; restarting it showed the interrupted message. Not yet seen on Render.
**Root cause on Render still to confirm:** its Events tab shows whether the restart was "Ran out of memory".

## Phase 3.15: faster claim verification with live progress (2026-10-31)

**Reported:** writing is fine now, but verification takes very long.
**Measured (Render run "What is AI?"):** report text at 57 s, verification done at 258 s. 33 LLM calls ran one after
another (~102 s, Gemini ~3 s each, a few 27–30 s); the rest was embedding on 0.1 CPU.
**Change:** `verify_report` checks `VERIFY_WORKERS` (4) claims at a time in copied contexts (so the usage meter and
live events still know the run); one embedding batch up front; the shared model chain drops a spent model once even
with concurrent claims (lock). Each finished claim publishes `claim_checked {done, total, status}`; the Live Report
shows "Verifying 7/17" with a progress bar. Render: `VERIFY_MAX_SENTENCES` 30 → 12.
**Measured:** same saved 17-claim report, real LLM (Groq spent, so Gemini): 98.2 s sequential vs 46.6 s with 4 workers,
same status for 17/17 claims. Over 115 saved claims, 12 candidate sentences per source instead of 30: 2,688 vs 6,594
sentences embedded (21,397 with no limit), same partial/not-found decision for 115/115.
**Checks:** `pytest` 103 passed (new: parallel checks keep order and drop a spent model once; progress per claim;
progress reaches the SSE stream between report_written and verification).

## Phase 3.16: runs no longer killed for memory on Render; faster search (2026-10-31)

**Reported (Render):** some runs end with "This run was interrupted because the server restarted" (0 sources at 1:39)
on a stable connection; processing should also be faster.
**Measured (Render settings, no-torch env, Windows):** one full run peaked at 584 MB, over Render's 512 MB, and the
jump came in verification (246 → 584 MB). Cause: ONNX Runtime keeps memory for every batch shape it embeds;
1,100 source sentences in batches of 32 added 327 MB (kept) and 25.9 CPU-s. Page extraction (download + trafilatura)
cost 6.6–9.5 CPU-s for 15 pages, about a minute on 0.1 CPU, while the page still showed 0 sources. Loading the
embedding model at the start of verification cost 2.9 CPU-s (~30 s there). Searches ran one after another.
**Changes:**
- `tools/embed.py` (onnx): embed shortest text first in batches of 8, results put back in order. Same 1,100 sentences:
  +47 MB, 12.1 CPU-s, identical vectors (min cosine 1.000000). `EMBED_WARM_UP=true` (Render) loads the model in the
  background when a run starts.
- `EXTRACT_ORDER=provider_first` (Render; default stays `own`): use Tavily's page text when it has enough, and download
  + trafilatura only for the rest (2 at a time, at most 1 MB: `EXTRACT_WORKERS`, `MAX_PAGE_BYTES`). 15 pages: ~0 CPU-s.
  Provider text is cleaned of repeated lines and short menu lines ("Patient & Visitor Guide", "Make a Donation");
  Tavily text is capped at 4 × `EXTRACT_MAX_CHARS` when it arrives.
- `athena_search`: a round's queries are searched at once (`SEARCH_WORKERS` 5), each in its own copy of the run's
  context (usage meter, live hits); ids are still assigned in query order. The timeout is checked before the round.
**Measured after (same settings, a different topic):** peak 296 MB; 14.0 CPU-s for the whole run vs 25.3. Wall time
was dominated by LLM waits: Groq's daily quota was spent and Gemini returned 503/429, so 10 of 14 claims ended
"partial" because the verifier could not be asked, not because of these changes.
**Checks:** `pytest` 106 passed (new: searches run at once and no search starts after the deadline; provider text goes
first only when configured and drops menu lines; onnx embeds shortest first and returns the caller's order).
**Not verified on Render (Linux).** Its Events tab would show "Ran out of memory" if a restart is still memory; a
redeploy during a run (e.g. auto-deploy after a push) also interrupts it.

## Phase 3.17: three hard-to-read pages removed (2026-10-31)

**Asked:** remove Research Planning, Research Gaps and Security (too much information, hard to understand).
**Removed:** `pages/Planning.tsx`, `pages/Gaps.tsx`, `pages/Security.tsx`, their routes and sidebar icons. The sidebar
has 10 pages, renumbered 01–10. Old links (`#/planning`, `#/gaps`, `#/security`) open the Dashboard. The buttons that
opened Gaps (Dashboard, Live Report, New Research) now open Verification. Nothing else is lost: plan and follow-up
approval stay on Live Report and New Research; `/api/system` stays (the Cost page uses it).
**Checks:** `tsc` and `vite build` clean; in the browser (throwaway local database) the sidebar lists the 10 pages and
`#/security` falls back to the Dashboard.

## Phase 3.18: Evidence Explorer and part-by-part claim verification (2026-10-31)

**Asked:** make claim-level evidence traceable: from a report statement to the exact passage, how strongly it
supports the claim, what is missing, and the original source. A new page (Claims & Evidence stays), a
`contradicted` status, source type + relevance instead of an authority score, and an accuracy check.
**Found before building:** the verifier stopped at the first source that said "yes", kept one sentence per claim,
never checked that the LLM's quote existed, could not say "contradicted", and called a claim "partial" when a
similar sentence merely existed (similarity >= 0.5). Source text was already stored per run, so no schema change.
**Verifier (`verify/claims.py`):** passages per cited source = the 5 most similar sentences plus, per clause of the
claim, the sentence sharing most of its words (`clause_matches`). One LLM call per claim over all its sources splits
the claim into parts, each with a verdict (supported / partial / contradicted / unsupported), the deciding passage
and an exact quote. Code keeps a verdict only if the quote is in that source (`quote_in`: case, spacing, curly quotes
and "..." gaps ignored); otherwise the part becomes unsupported with a note. Status from parts: any contradicted ->
contradicted; all supported -> verified; some backed -> partial; else not_found. No usable LLM answer -> unchecked
(it used to fall back to a similarity guess). Passages keep neighbouring sentences, position and nearest heading.
`CLAIM_STATUSES` (tools/text.py) is shared by the API, events, export, CLI and run list; `VERIFY_PARTIAL_SIM` removed.
**API:** `GET /api/runs/{id}/evidence` (claims with parts and passages, sources, Hermes' contradictions; `legacy`
for runs verified before this). **Frontend:** page 06 "Evidence Explorer" (claims -> evidence with per-part verdicts,
highlighted quotes and context, a tab per cited source -> source panel with original URL, a text-fragment link that
opens the page at the passage, heading/position, relevance, rule-based source type, sub-question, extraction
method). `contradicted` added to every status list, pill, bar and filter. Later pages renumbered 07-11.
**Accuracy check (`scripts/eval_verifier.py`, data in output/eval, not committed):** 32 claims sampled from 8 saved
runs; 3 excluded (not factual: "The sections below summarise...", "Not found in sources."). Labelled by Claude from
the cited sources before either verifier ran (one annotator, not a domain expert). 8 synthetic claims: real claims
with a wrong number or an added unsupported clause. Same model for both (Gemini flash-lite-latest; Groq's daily
quota was spent), one run each.
- Old verifier: 33/37 correct; "verified" for 1 of 10 claims not fully backed; 2 of 26 backed claims not verified;
  wrong-number claims came out "partial" (it cannot say contradicted).
- New verifier, first version: 33/37; 0 of 10 wrongly verified; 7 of 26 backed claims not verified (it judged
  "The sections below summarise" as a fact, and the 5 most similar sentences missed later facts of long claims).
- After the prompt rule and clause passages: 36/37; 0 of 10 wrongly verified; 2 of 26 backed claims not verified;
  all 6 wrong-number claims "contradicted". Tuned on the same 37 claims, so this is optimistic; a fresh labelled set
  is needed for an unbiased number.
**Checks:** `pytest` 110 passed (new: part verdicts, invented quotes rejected, contradictions, unusable answers
unchecked, clause passages, evidence endpoint and legacy flag). `tsc` and `vite build` clean. In the browser (local
SQLite copy of a saved run, re-verified): claims list and filters, part verdicts with quotes, passages with
headings, source panel; phone width without horizontal scroll, selecting a claim scrolls to its evidence.
**Not done / limits:** saved runs keep the old verification (shown as legacy) until re-run. No page numbers (web text).
Source type is a domain heuristic, not credibility. LLM verdicts still vary run to run (one run each above).

## Phase 3.19: research-quality rules (2026-10-31)

**Asked (10 rules):** a minimum relevant-source coverage target; original research before secondary articles; a
citation on every claim; verify each citation's exact snippet; remove duplicate sources; keep publication date apart
from research date; never label unsupported claims verified; say clearly when search results are insufficient; never
hide contradictory evidence; run quality checks before research completes.
**Already in place:** a citation on every claim (`enforce_citations`), exact-quote checks and never-verified-unsupported
(Phase 3.18 verifier), exact-URL de-duplication, contradictions on their own page.
**Added (`tools/quality.py`, used by `graph/nodes.py`):**
- Coverage: each sub-question needs `MIN_SOURCES_PER_QUESTION` (2) readable, relevant sources. Below that, code adds
  a "<sub-question> research study" search and Hermes cannot call the research complete (round limit still applies).
- Relevance: a page must contain `MIN_RELEVANCE` (30%) of its sub-question's key words. On a real run, dictionary
  pages returned for a long sub-question scored 8-23% and every on-topic page 38% or more.
- Original research first: `source_kind` (journals, PubMed/PMC, arXiv, `.gov`, WHO ...; rule-based from the URL) puts
  primary sources first when choosing pages to read and in the writer's source list, marked "(original research or
  official)". The writer must state disagreements and cite each side.
- Duplicates: pages with the same article text (word 5-gram overlap >= 0.8) are dropped; ids are numbered after
  dropping, so they have no gaps. Dropped pages are not read again in a later round.
- Dates: each source keeps `published` (from the page's own metadata, or Tavily's date; "" = unknown, never guessed)
  and `retrieved_at` (research date). Downloaded pages are reused; otherwise `FETCH_PUBLISH_DATES` reads the first
  64 KB of the page.
- `quality_check` node after verification: 9 checks (coverage, search results, original research, citations,
  quotes behind verified claims, unsupported claims, contradictions, duplicates/off-topic, dates), each pass / warn /
  fail with a reason. Checks never block the report; the report ends with "Evidence limits and quality checks",
  listing every warning, every conflict between sources and every contradicted claim with its quote (no [S#], so the
  section is never verified as a claim). `quality` event; `/report` gains `quality`, `quality_overall`,
  `duplicates`, `irrelevant`; sources gain `kind`, `published`, `retrieved_at`.
- Section flags: `not_found` now means "no cited answer at all"; `partly_not_found` marks answered sections with a
  missing point (both used to be "not found", so Verification showed answered sections as not found).
**UI:** Verification lists the backend checks with reasons (older runs keep the old checklist), source-kind, date
and removal counts; Sources and Evidence Explorer show "Published" and "Researched" separately and the source kind;
the live feed and the finished Live Report show the quality result.
**Real runs ("Does intermittent fasting improve blood sugar control in people with type 2 diabetes?"):** first run:
3 dictionary pages counted as coverage, which led to the relevance rule. Second run: 3 off-topic and the duplicate
copies of 2 articles removed; 7 of 12 cited sources original research; 18 claims (17 verified, 1 contradicted, quoted
in the report); 1 source-vs-source conflict listed; 8 of 15 sources state a publication date. Overall: passed with
warnings (parts of 3 sub-questions not found; conflict; dates).
**Checks:** `pytest` 120 passed (new `tests/test_quality.py`: source kind, dates, original-first + duplicate drop +
numbering, coverage follow-up, off-topic drop, no re-read, checks and report section, writer order, results check).
`tsc`/`vite build` clean; Verification, Sources and the report checked in the browser on a local copy of that run.
**Limits:** source kind and relevance are word/URL rules, not judgements of quality; publication dates are unknown
when a page states none; more searches per run (coverage follow-ups, date requests) use more search quota and time.
**Follow-up (live run in the app, "Is drinking coffee good or bad for heart health?", 2:55, 19 sources, 22 claims:
19 verified, 3 partial; 5/9 checks passed):** two misses fixed. One Semantic Scholar paper on two URLs was kept twice:
its copies shared 71% of word 5-grams (one carried a cookie banner), under the 0.8 threshold; `DUPLICATE_OVERLAP` is
now 0.5 (across the 34 sources of two real runs, different articles overlapped 24% at most). Hermes restated the same
S1-vs-S3 conflict in round 2, so it was listed twice; contradictions are now kept once per pair of sources.
`pytest` 122 passed.

## Phase 3.20: motion and hover polish (2026-10-31)

**Asked:** hover effects, micro-interactions, consistent spacing, smooth transitions; refined active-agent animation
and research progress indicators.
**Motion system (`index.css`):** one decelerating curve (`--ease-out`), 120 ms feedback, 200 ms state changes, 500 ms
progress fills. Links, buttons, selects and inputs fade their hover/focus colours; buttons settle 1px when pressed;
status pills cross-fade (`state-fade`); progress fills grow by `transform: scaleX` (`grow-x`), not width.
**Active agent:** the working agent's card (Dashboard/New Research strip and the Agents page) carries a light that
sweeps along its foot and a dot that sends out a soft ring; a finished agent's dot settles in once; each new
activity line fades in. Replaces the pulsing border.
**Progress:** the Live Report status strip has a rail that fills stage by stage (by claim during Verify) and turns
green when done or red if stopped; the current stage carries a slow sheen; the verify bar fills smoothly with a
sheen while running. Stacked status bars ease to their new shares.
**Micro-interactions:** sidebar icons lean toward the label on hover and the active marker draws in; the "Initiate
research" arrow nudges forward; the Live button's dot rings while a run is busy; pages fade in (no movement).
**Spacing:** already consistent (20 px between page sections everywhere), so unchanged.
**Accessibility:** every movement and loop is inside `prefers-reduced-motion: no-preference` (checked in the built
CSS); reduced-motion users keep the colour and state changes. Hover effects are only on things that can be clicked.
**Checks:** `tsc` and `vite build` clean; desktop and 375 px mobile checked in the browser (no horizontal scroll on
Live Report, Dashboard, Evidence Explorer).

## Phase 3.21: YouTube links are read, not guessed (2026-11-01)

**Problem:** chatbots given a YouTube link usually see only its title and description (the transcript is not in the
page, and YouTube often blocks bots), then guess the content.
**Now:** a YouTube link in the topic (watch, shorts, live, embed, youtu.be; a question may follow it) is read before
planning by `tools/video.py`: the video's own captions first (`youtube-transcript-api`; manual before auto, English
first, other languages translated by YouTube when possible), else Gemini watches the video by URL and transcribes it
(`llm.transcribe_video`, labelled `gemini_transcript`, not presented as captions). If neither works, the run stops
with the reasons (`stop_reason: video_unreadable`); nothing is summarised from the title.
**In the run:** the transcript ("[mm:ss] words" lines, up to `VIDEO_MAX_CHARS`, marked when cut) is source S1 for
every section (`section = ALL_SECTIONS`). The planner sees lines spread over the whole video and turns its factual
claims into searchable sub-questions; the writer must attribute the transcript ("The video says …") and judge it with
the web sources; Hermes can flag video-vs-source conflicts. The report is titled "Fact check: <video title>".
**UI:** the topic form says when it detects a link; a `video` event opens the event log; Evidence Explorer shows when
each video passage is said (▶ 03:14) and links to that moment; Sources marks the video.
**Checked:** a 14-minute TED talk on localhost: captions read (833 s), five sub-questions about the talk's claims, 16
web sources, passages linked to 03:14, 03:21, 03:48, 10:53. 9 of 19 claims were "unchecked" because Groq's
per-minute token limit (429) hit the verifier, not because of the video. Not checked on Render: YouTube often blocks
cloud servers' caption requests, so there the Gemini fallback (needs `GEMINI_API_KEY`) is expected to do the work.
`pytest` 142 passed (20 new in `tests/test_video.py`).

## Phase 3.22: runs that came back empty (2026-11-01)

**Audit of the last 40 runs:** search worked (most read 12-24 sources); runs failed afterwards. (1) LLM quota: Groq
gpt-oss-120b hit its tokens-per-day limit and Gemini's free quota ran out, so both "G20" runs read 14 sources and wrote
0 of 5 sections; the verifier hit rate limits in almost every run, leaving claims "unchecked". (2) Correctly cited
sentences were dropped as uncited because models wrote 【S6】, [S6†L1-L4], (S6) or [S1-S3]. (3) Topics with little on
the web (private people, a GitHub URL) say "not found", which is correct. (4) 7 runs were "interrupted" by restarts.
**Fixes:** `normalize_citations` rewrites those shapes to [S#] (ranges up to 5 ids); the ids still pass the allowed-id
check. Spare models with their own quotas join both chains after the main ones (`SPARE_MODELS`: Groq
qwen/qwen3.8-27b; Gemini gemini-3-flash-preview, gemini-3.6-flash, gemini-3.5-flash, gemma-4-26b-a4b-it; each answered
a JSON prompt on this project's keys; gemini-2.5-* are retired for new users). The verifier now moves a claim to the
next model on any failure (per-minute limit, 503, timeout), not only on a spent daily quota. When every model is out of
quota, planning raises `LLMUnavailable` and the run stops before searching (`stop_reason: llm_quota`); a new run is
refused with 503 and the same message while that is known.
**Checked:** "Research on G20" again on localhost: 10 sources, 4 of 5 sections answered (was 0 of 5), 19 claims checked
(7 verified, 9 partial, 2 contradicted, 1 not found, 0 unchecked) although the verifier met 15 rate limits; the
assembler fell back to qwen/qwen3.8-27b when gpt-oss-20b was rate-limited. `pytest` 158 passed.
**Found, not fixed:** every server start marks every "running" run in the shared database as interrupted, including
runs still alive on another server (Render waking up, or a local server sharing Neon). The G20 run was labelled
interrupted in the database while it was still running locally.

## Phase 3.23: a live run is no longer marked "interrupted" by another server (2026-11-01)

**Bug:** at startup a server marked every run its database listed as "running" as interrupted. Render (which sleeps
and wakes) and local servers share Neon, so a server waking up mislabelled runs still working on another server.
**Fix:** each server process has an id (`SERVER_ID`); run rows gained nullable `server` and `heartbeat` columns
(added to an existing table automatically, so older rows and older servers keep working). A process refreshes the
heartbeat of its active runs every 20 s (`_beat`). A run is treated as cut off only when its server has been silent
for 90 s (`STALE_SECONDS`), or, for rows from servers without heartbeats, when it is over 30 minutes old. A server
watches runs it loaded that another server is running, and records them when they finish or their server stops.
**Checked:** two real servers on one throwaway SQLite database. Server A started a run that waited for plan
approval; server B started and left it "running" (the old code marked it interrupted at this point); after A was
stopped, B recorded the run as interrupted about 85 s later. `pytest` 164 passed.
**Needs:** Render redeployed with this version; until then its startup still marks running runs interrupted.

## Phase 3.24: attached documents; Agent Orchestration page removed (2026-11-01)

**Asked:** "Attach document" did not work; remove the Agent Orchestration page.
**Why attach did nothing:** the button was disabled by design ("the API accepts a topic only"); upload had never
been built.
**Now:** `POST /api/documents` reads one file at once (`tools/documents.py`): a PDF's text layer page by page, as
lines starting "[p. N]", or a .txt / .md file as written; at most 10 MB per file and 40,000 characters kept. A
scanned PDF (no text layer), a password-protected or broken PDF, or another file type is refused with the reason;
nothing is guessed (no OCR). `POST /api/runs` takes up to 3 document ids. Each document becomes a source for every
section, numbered after the YouTube video if there is one; the planner sees lines from each; the writer sees it
marked "(document the user attached)"; claims citing it are verified like any source, and Evidence Explorer shows
the page of each passage. Uploads stay in memory for an hour, per user. The CLI takes `--doc FILE`. New packages:
pypdf 6.21.4 and python-multipart 0.0.33.
**UI:** "Attach document" opens a file picker (PDF, .txt, .md); each file shows a chip with its pages and characters
read, or the reason it could not be read, and can be removed. Sources and the report show attached files as text,
not links. The Agent Orchestration page, its sidebar entry and the Dashboard's "Agents" button are gone; pages are
renumbered 01-10.
**Checked:** `pytest` 173 passed (9 new in `test_documents.py`, with PDFs built in memory, including an image-only
page as a scan); in the browser, a 3-page test PDF (labelled inside as a test document) read as "3 pages" and a
.pptx was refused with the reason. A run with that PDF attached ("Does regular walking lower blood pressure?")
completed: the PDF was S1 and cited, 10 claims cited it (7 verified, 3 partial) with page numbers on their
passages, 13 web sources were added, and the report kept the document's own caveats (not randomised, small groups).
**Not checked:** real-world PDFs with complex layouts (columns, tables); text extraction quality depends on pypdf.

## Phase 3.25: specialist source APIs with primary -> fallback -> cache -> graceful error (2026-11-03)

**Built:** `backend/tools/sources/` — `router.py` (domains, keyword routing `classify`, `fetch`: primary, then
fallback, then cache, then a graceful error; common result shape; one log line per fetch with ids and statuses
only), `adapters.py` (OpenAlex, Semantic Scholar, PubMed, Europe PMC; Tavily and Serper reused from
`tools/search.py`), `cache.py` (SQLite, one connection per call, WAL, busy timeout, short transactions; TTL per
domain; expired entries kept and marked stale). `core/replay.py`: `LLM_MODE=replay` answers every search, source
API, LLM, page, date, embedding and video call offline (scenarios ok / primary_down / all_down). General web search
now goes through the same router (Tavily -> Serper -> cache). Athena adds up to `DOMAIN_PAGES_PER_QUERY` specialist
results per query on top of the web results and uses the API's abstract as the page text. Cached and stale sources
are marked for the writer, and the report's limits section names them. Relevance uses the existing
`quality.relevance` and `MIN_RELEVANCE` (now configurable), with per-domain overrides; a result with too little
text to judge is kept for Athena's later full-page check, as before.
**Checked:** `pytest` 201 passed (28 new in `test_sources.py`, network blocked at socket level in that file; a
replay-mode run made zero connection attempts). Live, once: OpenAlex, PubMed and Europe PMC each returned 5 results
with abstracts for "walking blood pressure randomized trial"; Semantic Scholar answered 429 without a key.
**Not done:** statistics, news and tech domains (World Bank, IMF, GDELT, GitHub, Stack Exchange) and Brave;
routing is keyword rules, not semantic; the UI does not badge cached or stale sources (the report text does).

## Phase 3.26: statistics, news and tech domains (2026-11-03)

**Built:** statistics = World Bank Documents & Reports (abstracts) -> IMF DataMapper (indicator definitions and
world series; the current year and later are labelled "IMF projection"); news = GDELT DOC 2.0 -> Tavily with
topic "news"; tech = GitHub repository search -> Stack Overflow (both read as pages, so answers and READMEs are
used). Keyword routing order: health, tech, statistics, news, academic, else general. `DOMAIN_SOURCES` now
defaults to all five domains. Optional keys: GITHUB_TOKEN, STACKEXCHANGE_KEY (plus the earlier three).
**Checked:** `pytest` 205 passed (offline parsing tests for each new adapter). Live, once: World Bank, IMF, GitHub
and Stack Exchange each returned 5 results; GDELT answered 429 (about one request per 5 s), so news usually falls
back to Tavily news. Brave (the spec's general fallback) is not added: Serper already fills that role.

## Phase 3.27: Crossref replaces Semantic Scholar as the academic fallback (2026-11-03)

Semantic Scholar answered 429 without a key and its key was not available, so the academic fallback is now
Crossref (no key; DOI registry). Checked live once: 5 results, 3 with abstracts, for "walking blood pressure
randomized trial". The Semantic Scholar adapter stays in `ADAPTERS` for anyone with a key.

## Phase 3.28: live check with the team's keys; GDELT, IMF and PubMed fixes (2026-11-04)

**Checked live, with keys:** all 12 adapters answered except GDELT (429); all 6 domains succeeded through the
router. One full run, "Does regular walking lower blood pressure?", 1 round: complete, 25 sources (Tavily, Europe
PMC, PubMed), 18 claims: 11 verified, 6 partial, 0 contradicted, 1 not found, 0 unchecked; Groq rate limits hit
the verifier and the fallback chain absorbed them.
**Fixed from that run:** (1) GDELT through retries took 46 s: now one attempt with an 8 s timeout, then Tavily news
(news domain ~13 s). (2) IMF downloaded its whole indicator list per call (26 s): the list is kept in memory for an
hour and only DOMAIN_PAGES_PER_QUERY + 1 series are fetched. (3) PubMed found nothing for every full-sentence
sub-question (it requires every term): it now ANDs the key words minus generic research words, dropping words
from the front until it finds papers. Re-checked live: on-topic papers for all four sub-questions.
**Still weak:** some domain results pass the 0.30 relevance threshold while being off-topic (a beetroot study for a
walking question), since the scorer counts shared key words; raise DOMAIN_RELEVANCE for health if this matters.
`pytest` 209 passed.

## Phase 3.29: The Guardian replaces GDELT as the news primary; keys removed from error messages (2026-11-04)

News is now The Guardian Open Platform (full article body text; free non-commercial developer key,
GUARDIAN_API_KEY) -> Tavily news. Without the key the Guardian is skipped and Tavily news answers. GDELT stays in
ADAPTERS but is unused (it answered 429 to most requests). Security fix: a failed request's message carries its URL,
and some APIs take their key in the URL (Guardian api-key, NCBI api_key, Stack Exchange key, OpenAlex/Crossref
mailto); `router.redact` now removes those values before a message reaches `errors` (UI and saved runs).
`pytest` 212 passed.

## Phase 3.30: YouTube links failed when captions were blocked (2026-11-05)

**Found live:** YouTube now refuses caption requests from this connection (`IpBlocked`), and the Gemini fallback
returned one empty reply, after which the run stopped with `video_unreadable`. Asked again, the same model
transcribed the 14-minute test talk in 21 s; `gemini-3-flash-preview` answered 503 at the time.
**Fix:** `VIDEO_GEMINI_MODELS` (default: VIDEO_GEMINI_MODEL, GEMINI_FALLBACK_MODEL, gemini-3.5-flash) are tried in
order until one gives a timestamped transcript; "NO_SPEECH" stops at once.
**Checked live:** the TED talk link with captions blocked: Gemini transcript (13,778 chars), report "Fact check:
Inside the Mind of a Master Procrastinator", 18 sources, 13 claims (4 verified, 8 partial, 1 contradicted,
0 unchecked), 451 s; OpenAlex 429s and Groq rate limits took most of that time. `pytest` 213 passed.

## Phase 3.31: OpenAlex 429s (2026-11-05)

**Cause, from OpenAlex's own reply:** keyless use has a $0.10/day budget (headers: 10 credits per search of 1,000,
so about 100 searches) and "anonymous search is temporarily rate-limited while the search cluster is under elevated
load"; the 429 asks to wait ~34 s, which the retry policy honoured, adding minutes to a run.
**Fix:** OPENALEX_API_KEY (free; $1/day budget) is sent as an Authorization header, never in the URL; OpenAlex gets
one attempt with no retries, so a 429 goes straight to Crossref. `pytest` 214 passed.
**Checked live with the team's key:** /rate-limit reports a $1/day budget (search costs $0.001); the long questions
that got 429 without a key returned from OpenAlex in 1.7-4.3 s through the router; no error message carried the key.

## Phase 3.32: keyless APIs checked live, one at a time and five at once (2026-11-05)

Crossref, Europe PMC, World Bank and IMF: 5/5 at once, slowest 2.9-12.4 s; YouTube oEmbed and captions worked.
PubMed's search (esearch) answered 500 to most requests (4 of 6 plain curl requests), while its other endpoints
answered: an NCBI-side fault, not our request. Health fell back to Europe PMC, but PubMed's two retries at 15 s
cost 15-25 s per query first, so PubMed now gets one retry and a 10 s timeout. `pytest` 214 passed.
**NCBI key checked (same day):** PubMed accepts it (X-RateLimit-Limit 10 with the key, 3 for an invalid one, which
is refused with 400). PubMed's search backend was still failing: 500s, and also HTTP 200 with
`"ERROR": "Search Backend failed"`, which the adapter read as "no papers" and kept re-searching with fewer words.
That answer now raises PubMedBackendError (treated as a 502: one retry, then Europe PMC). Two bursts of 5 health
searches: 10/10 answered by Europe PMC in 10-18 s, no 429 (one 429 before the key), key in no error message.
PubMed results with the key are not yet confirmed: its backend never answered during the check. `pytest` 215 passed.

## Phase 3.33: full end-to-end check of every feature (2026-11-05)

Server on a throwaway SQLite database, driven like the UI. **Run 1**, "Does regular walking lower blood
pressure?", Deep, human approval on, test PDF attached: upload read 3 pages, a .pptx refused with the reason;
the plan paused and resumed on approval; Hermes judged coverage complete after round 1 (stop_reason
hermes_complete, so no gap approval); 220 s, 15 sources (PDF S1, 7 primary), 19 claims (11 verified, 7 partial,
1 not found, 0 unchecked), 16 passages carry PDF page numbers; PubMed (500) and Europe PMC (503) were both down,
so health added nothing and the run went on with web sources. **Run 2**, the TED talk link: captions read,
161 s, 22 sources, 15 claims (5 verified, 8 partial, 1 contradicted, 1 not found, 0 unchecked), 41 passages carry
video times. All 9 endpoints answered 200 for both runs; all 10 pages rendered with data and no errors.
**Verifier bug found and fixed:** 3 of the 4 quotes rejected in these runs were real quotes written with a
non-breaking hyphen (U+2011) where the source has "-"; `quote_in` now treats every dash variant as "-". Rechecked
on the same runs: those 3 quotes are now found (run 1's C5 would be verified, not "not found"); the fourth is
still, correctly, not found. **UI text fixed:** the topic form and the report methodology no longer say web search
is the only source. `pytest` 216 passed.
**Seen, not fixed:** the test PDF says on its first line that it is not a real study, and the report cited it as a
trial without saying so; one meta sentence ("The sections below summarise ...") was checked as a claim.

## Phase 3.34: Render deploy checked; stricter tech routing (2026-11-05)

Render (apollo-innovista.onrender.com) served the same build files as commit a76d469. Signed in by the owner:
/api/system reported Gemini, Groq, Serper and Tavily keys set and a Postgres database. One live run (Focused, a UN
climate summit question): complete in 119 s, 20 sources (14 Tavily, 6 Guardian with full text, so GUARDIAN_API_KEY
is set there), 11 claims (7 verified, 3 partial, 1 not found, 0 unchecked). A sub-question with "frameworks" was
routed to the tech domain and found nothing; tech routing now needs software-specific words ("framework",
"library", "code" and "bug" dropped). `pytest` 217 passed. Academic and health keys on Render not yet exercised.

## Phase 3.35: Pause & edit topic; real progress (2026-11-06)

**Built:** POST /api/runs/{id}/stop sets a per-run stop event; the run ends between nodes, and every LLM call,
domain search and retry wait checks it (core/live.raise_if_stopped), so a node in progress fails fast instead of
finishing. A run waiting for approval is woken. It ends "rejected" with stop_reason "stopped_by_user". POST
/api/runs/{id}/restart {topic} starts a new run with the edited topic and the old run's depth, approval setting and
attached documents (kept in memory while this server has the old run; documents_lost says when they are not).
UI: "Pause & edit topic" on the Live Report and in the Apollo Flight game; after the stop, an editor with the old
topic and "Update & restart research". Progress (derive.ts) is now computed from the events: plan, each query's
results and pages, Hermes' review, sections written of sub-questions, claims checked of claims to check; the game
and the Live Report show the same number.
**Checked live (test database):** paused from the game while Hermes waited out a Groq rate limit: stopped, but only
after about 25-30 s, so retry waits now check the stop event in 0.25 s steps; re-checked: stopped in 1.7 s. Edited
the topic and restarted: new run with the new topic and the same settings, game back, completed at 100% with
progress moving 84 -> 90 -> 93% during claim checks (the old table sat at 76%). Search progress moved 8 -> 24 -> 28%
as query results arrived (before: 8 then 48). `pytest` 224 passed (7 new in test_stop.py).
**Seen:** on a narrow window the floating game covered the Live Report's Pause button; that button is now on the left.

## Phase 3.36: real Pause and Resume (2026-11-06)

**Built:** POST /pause sets a per-run flag; after the step in progress finishes (never cut short), the run thread
waits (status "paused", event "paused") until POST /continue (event "resumed") or POST /stop. Resume carries the
SAME run on: its LangGraph state, plan, sources and progress are kept, nothing restarts. Time spent paused is
recorded per run (core/live.add_paused) and taken out of the research clock (nodes._elapsed), like approval
waits already were. A run left paused PAUSE_MAX_SECONDS (30 min) ends as stopped. "paused" is an active status
(heartbeats, live stream). Pause is refused while a run waits for approval. UI: Pause / Resume / Stop & edit topic
on the Live Report, Pause/Resume in the game; "stopped_by_user" now reads "Stopped by you".
**Checked live (test database):** paused from the game while Hermes was reviewing; it waited for Hermes to finish
(slow because the embedding model was loading after the server restart), then paused at 48%; no progress for 15 s;
Resume continued the same run at "writing 2/5 sections" (58%) and it completed: 1 plan event, paused -> resumed
(39 s) -> report_ready, 5 sections, 19 sources, 23 claims. `pytest` 229 passed (5 new).

## Phase 3.37: DOCX export and public share links (2026-11-06)

**Built:** Final Reports' DOCX button was a disabled placeholder; GET /api/runs/{id}/report.docx now builds a Word
file from the stored state (reports/docx_export.py, python-docx 1.2.0): report headings, paragraphs and lists,
references, and the claim verification table. The Share link only opened for the report's owner (runs are
private). It now makes a public read-only link: POST /api/runs/{id}/share stores a random token
(secrets.token_urlsafe(32)) in the new runs.share_token column, DELETE turns it off, and #/shared/<token> opens
the report with no sign-in, through GET /api/shared/{token} (plus /report.md and /report.docx). The shared view
has only the report, claims and citations: no errors, logs, usage or other runs. The run's own saves never write
share_token, so a later save cannot turn a link off. When the browser refuses the clipboard, the copy falls back to
the older copy command, and the link also shows in a read-only text box.
**Checked live (test database):** DOCX download 200 (41 KB, 42 paragraphs, 1 table); shared link opened in a
signed-out browser; an unknown token shows "This link is not valid or has been turned off"; Turn off link made
the old link return 404; the pane refused navigator.clipboard and the fallback copied the link. `pytest` 234 passed
(5 new in test_share_docx.py).

## Phase 3.38: YouTube videos up to 35 minutes (2026-11-06)

**Seen on Render:** a CS50 lecture (1 h 46 min) failed. YouTube blocks Render's IP for captions (IpBlocked), and
Gemini, given the whole video, timed out (504 DEADLINE_EXCEEDED) or was overloaded (503).
**Built:** VIDEO_MAX_SECONDS (2100 = 35 min) is the longest video Apollo reads. Gemini is sent only that much, at
VIDEO_GEMINI_FPS (0.2) frames a second (VideoMetadata end_offset and fps; the words are in the audio); a transcript
that reaches the end of that window is marked as cut. VIDEO_MAX_CHARS rose from 40k to 48k, since a fast 35-minute
lecture is about 40k characters. The research form reads a pasted video's length in the user's browser (YouTube's
IFrame player API, no-cookie host, hidden, never plays) and refuses a longer one with a pop-up ("This video is too
long ... must be under 35 minutes"); when that length cannot be read the run starts and the backend decides.
Captions that show a longer video stop the run with VideoTooLong and that reason. /api/system's limits carry
video_max_seconds.
**Checked live:** the same lecture with captions forced to fail: Gemini on the first 20 min took 27 s (22.7k
characters), 30 min 38 s (33.8k), 35 min 43 s (39.9k), 45 min 329 s. In the browser the lecture's link opened the
pop-up (1 h 46 min) on desktop and phone without starting a run; a short video went straight to the start request.

## Phase 3.39: research time limit 300 s (2026-11-06)

**Changed:** TIMEOUT_SECONDS 180 -> 300 (config default, .env.example, README). The limit only decides whether another
search round starts; writing and claim verification always run. In local Deep runs the first review came 45-131 s
after the start, so 180 s was enough for plain topics; a YouTube topic spends about 45 s reading the video first,
and slower Render runs (rate-limit waits) could pass 180 s before the first review, losing the second round.
Topics that finish in one round are unchanged: Hermes' "complete" still stops the run.

## Phase 3.40: dark theme (2026-11-06)

**Built:** a bulb toggle in the top bar (SVG, aria-pressed, label and tooltip "Switch to dark/light theme") sets
html data-theme and saves it as localStorage apollo-theme; a small script in index.html applies the saved theme
before the first paint, so there is no light flash. frontend/src/theme-dark.css remaps the existing tokens
(--color-surface, --color-ink, --color-line, ...) under :root[data-theme=dark], for screens only (printing stays
light). Selected states (active nav, selected depth card, enabled primary button, active filters, selected rows,
agent number strips, inverted chips) use deep teal #1F5B5D with #E9F7F5 text; disabled primary buttons stay a
neutral blue-grey with no shadow. Dark navy workspace with the same wave composition. The claim-evidence graph
reads its colours from CSS variables (light values unchanged). Apollo Flight's panel follows the theme; its canvas
keeps the light sky. The light theme's rules are untouched.
**Checked (production build, local server, replay mode for a busy run):** toggle switches and survives a reload;
computed colours in dark: textarea rgba(7,19,31,.78), cards #0C1B29, selected depth card and active nav #1F5B5D
with #E9F7F5 text, disabled Initiate research #15293A with #7F92A3 text; Dashboard, Claims (graph, filters, list),
Reports, Analytics, Live Report with the approval panel and the mini-game (start, flap, minimize) all readable;
375 px phone: no horizontal scroll. Element positions are the same in both themes.

## Phase 3.41: clearer theme toggle (2026-11-06)

**Changed:** the 16 px bulb icon was easy to miss. The toggle now shows the theme it switches to: a moon and "Dark"
in the light theme, an amber sun and "Light" in the dark theme. Phones show the icon alone, so the top bar keeps
its rows. aria-label, aria-pressed and the tooltip are unchanged.
**Checked (production build):** 1024 px: the toggle is 80 px wide on the session row, no overflow; 375 px: icon
only (42 px), no horizontal scroll; both icons render and the theme still switches and is saved.

## Phase 3.42: redesigned sign-in screen; sign-out waits for the server (2026-11-06)

**Built:** the sign-in / create-account screen now matches the dashboard: the workspace's wave background, a
two-column layout above 900 px (Apollo mark, "Evidence, not guesswork.", three benefits; a divider; the card) and
one column below it, a 470 px card with a 2 px border and 8 px hard shadow, Sign in / Create account tabs with a
mint (light) or deep-teal (dark) selected state, and the theme toggle top right. The toggle is now one shared
component (components/ThemeToggle.tsx) used by the top bar and this screen, so the theme survives sign-out and
sign-in. The form keeps the same fields, API calls, validation and errors; it adds aria-busy, 44 px fields, and a
ref guard so a double click or Enter + click sends one request (the busy state alone let three through when
fired in the same moment). Sign out now waits for POST /api/auth/logout: success shows the sign-in screen, a
failure keeps the user signed in and shows "Could not sign out: ..." in the top bar (a 401 counts as signed out).
Dark theme: light card borders, deep-teal tab, readable browser autofill.
**Checked (production build, local test database):** failed sign-out (forced 500) kept the session; real sign-out
showed the sign-in screen at once, theme kept; wrong password showed "Incorrect email or password."; three
submits in one moment sent one request; registration (short password refused, then created) signed in; the
session came back after a refresh; sign-out and sign-in again worked; 1366, 820 and 375 px: no horizontal scroll.
