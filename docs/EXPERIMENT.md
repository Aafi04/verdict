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

## Deviations
- **2026-11-26 (pre-launch):** Experiment `1` (`result_view_experiment`) was ended (`completed`) before opening to participants because a single simulated test exposure (`sim-466fd99f-5c0d-4045-a4ac-3c3d228ed10a`) from the Nov 26 phone test reached Production via a `QuizSettings.from_env()` fallback to `FLAGSMITH_ENV_KEY` when `FLAGSMITH_SIM_ENV_KEY` was unset. The fallback was removed in commit `01e5d47` (`01e5d471ee9aeed29bba2de696a21b984d509d4c`), multivariate variant option `id=1` key was renamed from `Variant_1` to `verdict`, and Experiment `2` (`result_view_experiment_v2`) was created and started cleanly (`0` pre-launch exposures). Experiment `2` is now the registered experiment; hypothesis, metrics (`shipped_ambiguous` primary, `held_clear_win` guardrail), allocation (50/50, 100% rollout), and freeze time (**Nov 29, 2026, 09:00 IST**) remain unchanged.
