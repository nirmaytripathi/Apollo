# 2-minute demo script

A live run takes 2–4 minutes, so Topic A and Topic B are started a few minutes **before** you present.
During the 2 minutes you show one live run (with your approval) and the two finished runs.
Everything on screen comes from real runs; nothing is pre-written.

## Prep (about 10 minutes before presenting)

1. **Terminal 1: backend, with the Topic B demo switch.** In the project folder:
   ```powershell
   $env:APOLLO_DEMO_FAIL_SUBQUESTION = "2"
   .venv\Scripts\python -m uvicorn api:app --app-dir backend --port 8000
   ```
   If Groq's daily quota is used up (warnings say "tokens per day"), run this first, in the same terminal:
   ```powershell
   $env:LLM_PROVIDER = "gemini"
   ```
2. **Terminal 2: frontend.**
   ```powershell
   cd frontend
   npm run dev
   ```
3. **Clean cache** (terminal 3):
   ```powershell
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/cache/clear
   ```
4. **Tab 1, Topic A:** open http://localhost:5173. Click **DEMO A**, keep Rounds **2**, leave **Human approval**
   unticked, click **LAUNCH**. Wait for the event log's last line, `DONE complete`. Check the log has a `GAP`
   line and `SEARCH [r2 …]` lines (the second round).
5. **Tab 2, Topic B:** open http://localhost:5173 in a second tab. Click **DEMO B**, set Rounds **1**, click **LAUNCH**.
   Wait for `DONE`.
6. **Tab 3:** open http://localhost:5173 in a third tab and leave it. This is the live part.

Do not clear the cache again after step 4: it forgets finished runs.

## The 2 minutes

**0:00 · Tab 3: the idea (15 s)**
> "Apollo is a research team of three agents. Apollo plans, Athena searches the web, Hermes reviews for gaps
> and conflicts. The rule: every claim must cite a source we actually retrieved, and we check it."

Click **DEMO A**, tick **Human approval**, click **LAUNCH**. Point at the agent strip: the active agent's card pulses.

**0:15 · Tab 3: human in the loop (30 s)**
When the **APPROVE PLAN** panel appears:
> "Before anything is searched, I can check the plan."

Click **Edit**, change one sub-question (e.g. add "in Odia or Hindi"), click **APPROVE PLAN**.
> "My edited question is what Athena searches now."

Point at the event log: the `SEARCH [r1 …]` line shows your edited question.
(Shortcuts when no field is focused: **Enter** approves, **E** toggles edit.)

**0:45 · Tab 1: two rounds and a gap (25 s)**
> "Here is the same topic, finished. Hermes found gaps after round 1, so Apollo ran a second round with
> Hermes' follow-up queries, then stopped at the 2-round limit, a rule enforced in code."

Point at the cyan `GAP` lines, the `SEARCH [r2 …]` lines, and `DONE complete · round limit reached`.
The agent strip shows ROUND 2/2.

**1:10 · Tab 1: the report (25 s)**
Hover a citation chip such as **S2**:
> "Every claim links to its source. Hover shows the title, the text and the URL."

Click the **Verification** tab:
> "Each cited sentence was checked against the source's text: verified, partial, or not found."

Click **Sources**, then on the Report tab click **DOWNLOAD REPORT.MD**.

**1:35 · Tab 2: failure is handled (20 s)**
> "Here a search failed. We simulate an outage with a demo switch, because live search almost never returns
> nothing. The failure is an amber warning, the run still completes, and section 2 honestly says
> 'Not found in sources' instead of letting the AI guess."

Point at the amber `WARN no results · …` line and section 2 of the report.

**1:55 · Close (5 s)**
> "Plans you can steer, sources you can click, claims you can check."

After the demo, go back to tab 3. When the **APPROVE GAP QUERIES** panel appears, press **Enter** (or click
**APPROVE ALL** / **Skip**) so the run can finish.

## If something goes wrong

- **Topic A stopped after round 1.** Hermes judged the sources sufficient (the LLM is not deterministic), or a rate
  limit hit Hermes (amber `WARN` mentioning 429). Run Topic A again in tab 1.
- **Amber "Rate limit … tokens per day".** The Groq quota is spent. Restart terminal 1 with `LLM_PROVIDER = "gemini"`.
- **Amber "503 UNAVAILABLE".** Google's model is overloaded; also set `$env:GEMINI_MODEL = "gemini-flash-lite-latest"`.
- **Tab 3 is still waiting at the end.** Approve or skip the gap queries. Unanswered approvals auto-approve after 15 minutes.
