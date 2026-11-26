# PRD - Verdict

## 1. Problem
Experiment tools report lift, win probability and credible intervals, but leave the ship decision to a person. Common failure modes: shipping when the interval crosses zero, stopping early after repeatedly peeking, and trusting data with broken assignment (sample ratio mismatch, unstable identity, one-sided exposures). Result: noise gets promoted to a decision.

## 2. Goal
Give a team a trustworthy second opinion on a Flagsmith experiment in seconds, with a human always approving any change.

## 3. Users
- Primary: a developer/PM running a Flagsmith experiment who must decide whether to roll out a variant.
- Secondary: judges evaluating the hackathon submission (they need to grasp the value in under 3 minutes).

## 4. Hypothesis (Track 1)
Showing the Verdict memo instead of a standard win-probability view reduces the share of people who choose to **ship** an inconclusive result.
- Baseline assumption (NOT measured): about 60% ship the ambiguous scenario under the standard view.
- Target effect: drop to about 35% or lower.
- Rough sample need (own estimate, two-proportion test): about 60 per arm for a 25-point drop, about 100 per arm for a 20-point drop. Target at least 200 real completions.

## 5. Experiment spec (summary; details in EXPERIMENT.md)
- Flag `result_view` (multivariate): `dashboard` (control), `verdict` (variant); 50/50.
- Primary metric: `shipped_ambiguous` (expected to decrease with `verdict`).
- Guardrail: `held_clear_win` (should not increase; protects against over-caution).
- Unit: one visitor = one stable UUID identity = one exposure.

## 6. Product requirements
### Must (P0)
- M1. Quiz: landing, notice, scenario 1 (ambiguous), scenario 2 (clear win), thank-you page with a short explanation of the right answers.
- M2. Quiz uses Flagsmith experiment evaluation for `result_view` and sends the exposure and metric events; refresh does not double-count.
- M3. `verdict check <experiment>` pulls exposures/results through `FlagsmithClient` and prints a memo with verdict, reasons, and every check's status.
- M4. Checks: SRM (read from Flagsmith `srm_p_value` and cross-checked with Verdict's chi-square), min exposures per arm, interval crosses zero, planned sample not reached (power), guardrail regression (`held_clear_win` from SQLite log), look count (peeking log — counts only `kind="effect"` looks, ignoring `kind="integrity"` checks), exposure-vs-event sanity.
- M5. `verdict apply` proposes the exact write, is dry-run by default, requires typed approval, logs to `data/audit.jsonl`.
- M6. Fixture mode: whole agent runs offline on `data/fixtures/*.json`.
- M7. Seeded-fault demo experiment (clearly labelled simulated) that Verdict REJECTS.
- M8. Docs: README, EXPERIMENT.md with honest results section, DEMO.md.
### Should (P1)
- S1. Optional LLM-worded memo (feature-flagged OFF by default; template fallback).
- S2. Tiny local web page for the memo + approve button (same audit path as CLI).
- S3. Pre-launch power estimate (`verdict plan`): given baseline rate and traffic, how long to run.
### Won't (this hackathon)
- No prompt-to-experiment generation, no dashboards, no multi-project auth, no enterprise-only Flagsmith features.

## 7. Verdict policy (deterministic)
Evaluate in this order; first match wins.
1. Any integrity check FAIL (SRM p < 0.001 — read from Flagsmith `srm_p_value` and cross-checked by Verdict, missing exposure arm, event with no exposure ratio anomaly) -> **REJECT (invalid data)**.
2. Guardrail metric significantly worse in the variant -> **REJECT**.
3. Planned sample size not reached OR credible interval for primary metric includes zero -> **HOLD** (state how much more data is needed if estimable).
4. Interval entirely on the favourable side and sample reached -> **SHIP** (propose rollout, wait for approval).
Thresholds live in `verdict/config.py` and are documented in the memo.

## 8. Autonomy table (shown in demo)
| Verdict does alone | Verdict never does without a human |
|---|---|
| Read experiments, exposures, results; run checks; write memo; log every look | Change any flag state, allocation or override |

## 9. Success criteria
- S-1: Target 200 real quiz completions with clean exposure data, minimum 100 for a credible reading; fewer is reported honestly as a limitation.
- S-2: Verdict returns correct verdicts on all fixtures (ship, hold, reject-SRM, reject-guardrail).
- S-3: Demo runs end to end in under 3 minutes without live debugging.
- S-4: Zero writes to Flagsmith without a recorded approval.

## 10. Risks
Low traffic, Flagsmith experimentation is Beta, results may refresh on demand only, MCP tool names unverified, overlap with other agent entries (e.g. repos calling themselves "closed-loop"), self-referential-experiment perception. Mitigations in PHASES.md and the README honesty rules.

## 11. Open questions (resolve at gates)
- How are metrics defined in this Flagsmith version, and can an event-count metric express "shipped_ambiguous"?
- Are results computed continuously or on demand, and what is the as-of timestamp?
- Which write operation changes multivariate allocation or promotes a variant?
- How will visitors reach the quiz publicly during the whole collection window?
