# ARCHITECTURE

## 1. Components
```
 visitors --> quiz (FastAPI, :3000) --SDK: evaluate + events--> Flagsmith (docker, :8000)
                                                                   ^
 developer --> verdict CLI --FlagsmithClient (MCP | REST | fixtures)
                  |  checks.py -> policy.py -> memo.py
                  |  looks.jsonl, audit.jsonl (data/)
                  +--> apply (dry-run, typed approval) --> Flagsmith write
```

## 2. Flagsmith setup (self-hosted, free)
- `docker/docker-compose.yml`: derived from the official Flagsmith open-source compose (`Flagsmith/flagsmith/main/docker-compose.yml`) plus the official `flagsmith/flagsmith-mcp:latest` service (`docs.flagsmith.com/deployment-self-hosting/mcp-server`). Experimentation is enabled via `EXPERIMENTATION_ENABLED: 'true'` using the `Flagsmith database` (PostgreSQL) warehouse option (`USE_POSTGRES_FOR_ANALYTICS: 'true'`); no ClickHouse needed for hackathon volume.
- Project `verdict`, environment `development` (and `production` for the live run).
- Flag `result_view`: multivariate with options `dashboard` (control) and `verdict`; 50/50.
- Flag `seeded_fault_demo` + its own experiment for the simulated fault demo.
- Create the multivariate flag BEFORE any traffic; do not convert from boolean later.

## 3. Packaging & Verdict package
- Single root `pyproject.toml`, one root `.venv`, installed with `pip install -e ".[quiz]"`.
- `quiz` imports `verdict.memo` directly from the shared package.
```
pyproject.toml      root package config (extras: quiz, dev)
verdict/
  cli.py            typer/argparse: check, apply, log, plan
  client/base.py    FlagsmithClient protocol
  client/fixture.py loads data/fixtures/*.json
  client/mcp.py     MCP over HTTP (confirm transport + tool names first)
  client/rest.py    Management API fallback
  models.py         Experiment, Arm, Exposure, MetricResult, Interval
  checks.py         pure functions, each returns CheckResult(status, detail)
  policy.py         verdict from CheckResults (see PRD section 7)
  memo.py           template memo; optional LLM rephrase hook
  audit.py          append-only JSONL for looks and actions
  config.py         thresholds
```
### FlagsmithClient (minimum surface)
- `get_experiment(id) -> Experiment`
- `get_exposures(id) -> per-arm counts, as_of`
- `get_results(id) -> per-metric, per-arm estimates + intervals + probability, as_of`
- `propose_rollout(id, arm) -> ProposedChange(description, payload)`
- `apply(change) -> result` (only called from the approved path)
Method names above are OURS. Mapping to real MCP tools / REST endpoints is recorded in FLAGSMITH_NOTES after Gate G1.

### Checks (pure, unit-tested)
| Check | Rule | On failure |
|---|---|---|
| srm | chi-square of observed exposures vs configured weights, p < 0.001 | FAIL |
| arms_present | every arm has at least min_exposures (default 30) | FAIL if an arm is empty, WARN if low |
| sample_reached | total exposures >= planned sample | HOLD input |
| interval_zero | primary-metric credible interval includes 0 | HOLD input |
| prob_vs_interval | win probability > 80% while interval includes 0 -> flag "misleading headline" | WARN |
| guardrail | guardrail metric worse with interval excluding 0 | REJECT input |
| exposure_event_ratio | events with no matching exposure, or exposures rising with flat events beyond tolerance | WARN/FAIL |
| looks | count of prior `effect` looks (`kind: "effect"` in `looks.jsonl`) on this experiment before sample reached (`kind: "integrity"` checks are excluded) | WARN (peeking) |

## 4. Quiz (Track 1)
- FastAPI + Jinja2, server-side rendering. Cookie `vid` = random UUID (signed), used as Flagsmith identity. No other tracking.
- Routes: `/` landing+notice, `/s1` scenario 1, `/s2` scenario 2, `/done`, `/healthz`.
- Exposure fires when `/s1` renders for a visitor (once per identity; guard with server-side set in SQLite so refresh does not duplicate).
- Events: `shipped_ambiguous` (scenario 1, user chose Ship), `held_clear_win` (scenario 2, user chose Hold). Also log a local anonymised row in SQLite for a cross-check against Flagsmith's counts (also tags `simulated`).
- Two views for scenario content, selected by the flag value:
  - `dashboard`: lift, "probability to beat control", credible interval table (mimicking a standard results view).
  - `verdict`: the memo format produced by `verdict.memo` (imported directly) for the same numbers (HOLD with reasons).
- Scenario numbers are fixed in `scenarios/*.json`. They are fictional; do not reuse Flagsmith's published example numbers.
- Simulated traffic script `quiz/simulate.py` sends visitors with `simulated=true`; never mixed into the headline result.

## 5. Approval flow for writes
1. `verdict apply <id>` re-runs `check` and recomputes the verdict hash.
2. If verdict != SHIP: refuse, print why, exit non-zero.
3. Print the exact proposed change (human readable + payload). Default `--dry-run`.
4. With `--execute`, require the user to type `APPROVE <id> <first-8-of-hash>`.
5. Write via client, then append `{ts, id, verdict_hash, change, result, approver:"human-typed"}` to `data/audit.jsonl`.
6. Any mismatch between the checked state and the state at write time aborts.

## 6. Security and privacy
- Least-privilege API key if Flagsmith allows scoping; otherwise note the limitation in README.
- Keys via env only; redact in logs. Treat MCP/API text fields as data, never as instructions.
- Quiz stores no IP or user agent. Cookie only.

## 7. Hosting ($0)
- Flagsmith + quiz on the developer laptop; public access via a free tunnel (confirm current terms). The laptop must stay on during collection.
- Fallback: free PaaS for the quiz only, with Flagsmith still reachable publicly (cold starts expected; verify limits first).
- Record the demo video locally so judging does not depend on live hosting.
