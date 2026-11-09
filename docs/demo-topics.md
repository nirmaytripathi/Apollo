# Demo topics

The web app's demo buttons read this file (`GET /api/demo-topics`): each `## Topic <id>: <title>` heading is one
topic, its `**Topic:**` line is the text, and its one-line `Settings:` line is shown as a note. Keep this format.

Before each demo run, click **Clear cache** in the app (or `curl -X POST http://127.0.0.1:8000/api/cache/clear`).

## Topic A: two research rounds

**Topic:** Measured yield impact of AI crop advisory apps for smallholder farmers in Odisha and Bihar

Settings: max rounds **2**, HITL on or off.

What to expect:
- Round 1: Apollo plans 4–5 sub-questions; Athena finds general sources on AI advisory apps.
- Hermes finds a **gap**: published yield numbers *specific to Odisha and Bihar* are scarce. Its follow-up
  queries look like "quantitative study … yield change … Odisha" / "… Bihar".
- Round 2 searches those gap queries, then Apollo stops with "maximum number of rounds reached"
  (or "Hermes judged the sources sufficient").
- Several sections honestly say "not found in sources": the web has little district-level data.

Observed in live runs: round 2 was reached in every recorded run (see PHASE_LOG.md). The LLM is not
deterministic, so in a rare run Hermes may accept round 1. Run it again if that happens.

## Topic B: a search fails, the run continues

**Topic:** How are AI chatbots used to give farming advice to Indian farmers?

Settings: max rounds **1**, HITL off. Needs the backend demo switch `APOLLO_DEMO_FAIL_SUBQUESTION=2` (see DEMO.md).

Why a switch: live web search (Tavily, then Serper) practically never comes back empty. Probes with
nonsense and 1,000+ character queries still returned 5 results. To *show* the failure path, the switch
makes the search for the **2nd sub-question** of this topic fail through Athena's normal error path.
It affects only this exact topic and is off unless the variable is set.

What to expect:
- The progress feed shows an **amber** warning: "No results for “<sub-question 2>”", with the reason
  "simulated outage (demo switch …)".
- The other sub-questions are searched normally and the run **completes**.
- In the report, section 2 reads **"Not found in sources."**; it has no citations and was not written by the LLM.
- Other amber warnings may appear too (pages that refused to be read). These are real, not simulated.
