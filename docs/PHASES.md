# PHASES (4 days). Times are IST. Deadline: Nov 29, 8 PM.

## Phase 0 - Setup (Nov 26, first 2-3 h)
Tasks: repo skeleton, `docker compose up`, create org/project/environment, enable experimentation, create multivariate flag `result_view`.
Done when: Flagsmith dashboard reachable; flag exists with two options.

### GATE G1 (must pass before Phase 1)
- G1a. A script sends an exposure through the Python SDK experiment call and a metric event; they appear in Flagsmith.
- G1b. Results for the experiment can be read (via MCP, REST or CLI; prefer REST or CLI if MCP lacks experiment tools). Record exact calls, fields, and whether results are on-demand (`as_of`).
- G1c. List MCP tools (`tools/list`) and write the real names + arguments into FLAGSMITH_NOTES.md.
Fallback: if MCP does not expose experiment tools, read results via REST or CLI (`flagsmith api`); only if results cannot be read programmatically at all, compute checks from the quiz's own SQLite log and read intervals from the dashboard manually as a last resort, stating this plainly.

## Phase 1 - Quiz live (Nov 26)
Tasks: scenarios JSON, both views, cookie identity, exposure de-duplication, events, /done page, local SQLite log, `.env.example`.
Done when: 20 test visits (labelled simulated) show correct split and event counts in Flagsmith and SQLite within tolerance.

### GATE G2 (end of Nov 26)
Quiz is publicly reachable for the full collection window. Start sharing ONLY after G1 + G2 pass.
Fallback: deploy the quiz on a free PaaS with a publicly reachable Flagsmith instance, else expose via a stable tunnel on the developer's laptop kept on during collection. Record whichever option is used.

## Phase 2 - Collect + Verdict core (Nov 27)
Tasks: share in Discord/LinkedIn/X/classmates; implement models, FixtureClient, checks, policy, memo, tests (fixtures: ship, hold, reject-SRM, reject-guardrail, peeking).
Done when: `pytest` green; `verdict check` on every fixture gives the expected verdict.

### Phase 2 Test Plan (`data/fixtures/*.json` + `tests/`)
1. **Fixtures & expected verdicts**:
   - `ship.json`: balanced 50/50 split (`srm` PASS), >= 100 exposures per arm, planned sample reached, primary CI strictly favourable (excludes 0), guardrail PASS, looks = 0 -> **`SHIP`**.
   - `hold.json`: balanced split, >= 50 per arm, planned sample not reached OR primary CI crosses 0 (with high win probability > 80% triggering `prob_vs_interval` WARN) -> **`HOLD`**.
   - `hold_insufficient_data.json`: small sample (< ~50 exposures per arm) where Flagsmith returns `inference: {verdict: null}` and `srm_p_value: null` -> handled explicitly as **`HOLD` ("insufficient data")**, NOT an error or `REJECT` (unless an arm has 0 exposures or severe SRM).
   - `reject_srm.json`: observed split skewed with chi-square `p < 0.001` (or missing exposure arm / exposure-event ratio failure) -> **`REJECT`** (invalid data, even if win probability looks high).
   - `reject_guardrail.json`: primary metric favourable and sample reached, but guardrail (`held_clear_win` or guardrail CI excluding 0 in worse direction) regressed -> **`REJECT`**.
   - `peeking.json`: prior `check` entries logged in `looks.jsonl` before planned sample reached -> `looks` check emits **`WARN` (peeking)** while policy still returns **`HOLD`** (if sample/CI unmet).
2. **Additional unit tests**:
   - Cross-check Flagsmith `srm_p_value` (when present) vs Verdict's own chi-square calculation (`p < 0.001`).
   - Verify `verdict check` works completely offline against `FixtureClient` with `VERDICT_LLM=off`.

## Phase 3 - Live wiring + approval (Nov 28)
Tasks: real client (MCP or REST), `verdict check` against live experiment, `apply` flow with dry-run/typed approval/audit log, seeded-fault experiment (simulated, labelled) that returns REJECT.

### GATE G3
A write tool/endpoint that changes allocation or promotes a variant is identified and tested in dev. Fallback: `apply` outputs the exact `flagsmith api` / REST command for the human to run, and the audit log records "proposed".
Done when: end-to-end on dev environment: check -> propose -> approve -> flag changed -> audit entry.

## Phase 4 - Results, docs, video (Nov 29, submit by 4 PM for buffer)
Tasks: freeze data collection at a stated time; run `verdict check`; fill EXPERIMENT.md results (including limitations); finalise README, DEMO.md; record video (<= 3 min); submit both tracks; star the Flagsmith repo if the hackathon page still lists that perk.
Done when: submission confirmations received; repo public; video link works logged out.

## Daily checkpoint questions
1. How many real completions so far? 2. Any exposure/event mismatch? 3. Is anything un-demoable? If yes, cut scope (drop S1-S3 first).
