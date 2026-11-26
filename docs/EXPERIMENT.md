# EXPERIMENT - result_view

## Question
Does a verdict memo, compared with a standard win-probability view, reduce the share of people who choose to ship an inconclusive result?

## Hypothesis (pre-registered before collecting data; commit this file before sharing the link)
H1: `verdict` lowers `shipped_ambiguous` relative to `dashboard`.
Assumed baseline for control: about 60% (assumption, to be replaced by measured control rate).
Minimum effect of interest: 20 percentage points.

## Design
- Variants: `dashboard` (control) vs `verdict`, 50/50, assigned by Flagsmith to a persistent visitor identity.
- Exposure: scenario 1 render.
- Primary metric: `shipped_ambiguous` (lower is better).
- Guardrail: `held_clear_win` (must not rise materially; scenario 2 is a genuinely strong result).
- Planned sample: target at least 200 real completions (about 100 per arm), minimum 100 for a credible reading; fewer is reported honestly as a limitation. Decision time fixed in advance: **Nov 29, 2026, 09:00 IST**. Do not stop early because the result looks good.
- Unit of analysis: visitor. One decision per scenario per visitor.

## Scenarios (fictional numbers)
- S1 (ambiguous): variant +11.2% lift, 86% probability to beat control, interval -9.8% to +34.1%, 140 users per arm of a planned 800. Correct: HOLD.
- S2 (clear): variant +18.4% lift, 99% probability, interval +9.1% to +27.7%, planned sample reached, split matches the allocation. Correct: SHIP.

## Quality controls
- Stable identity; persisted cookie; exposure de-duplicated.
- Exposures in both arms; exposure counts compared with SQLite log.
- SRM check on the experiment's own split.
- Simulated traffic excluded from the headline result and reported separately.

## Known limitations (state in the final report)
- Respondents are mostly developers recruited from hackathon/social channels (selection bias).
- Scenarios are fixed and fictional; effect on real decisions may differ.
- Short collection window; intervals may be wide.
- The tested tool is the one evaluating the experiment (conflict of interest); the policy is deterministic and published.

## Results (fill after freeze; do not edit the hypothesis above)
- Exposures per arm: 
- Control rate / variant rate (primary): 
- Credible interval and probability from Flagsmith: 
- Guardrail: 
- Verdict output (paste memo): 
- Honest reading: 
