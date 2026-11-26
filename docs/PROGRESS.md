# PROGRESS

## Phase 0 - Setup (GATE G1 PASSED)
- **Goal**: Reach Gate G1 (repo skeleton, self-hosted Flagsmith + experimentation + MCP compose, org/project/env/flag setup, Python SDK exposure + event sent, experiment results + MCP `tools/list` verified).
- **Definition of done**:
  - Flagsmith dashboard reachable at `http://localhost:8000` with experimentation enabled.
  - Multivariate flag `result_view` (`dashboard`, `verdict`, 50/50) and experiment created.
  - `scripts/g1_check.py` sends an exposure + metric event via Python SDK and reads back experiment exposures/results (MCP / REST / CLI).
  - MCP `tools/list` run against local instance and recorded in `docs/FLAGSMITH_NOTES.md`.
- **Checklist**:
  - [x] Doc amendments applied (`README.md`, `docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/PHASES.md`, `docs/EXPERIMENT.md`)
  - [x] Repo skeleton, `.gitignore`, `.env.example`, root `pyproject.toml`, `docker/docker-compose.yml` created
  - [x] Root `.venv` installed (`pip install -e ".[quiz,dev]"`) and `flagsmith==6.7.0` SDK signatures inspected
  - [x] `scripts/g1_check.py` written and linted (`ruff` clean)
  - [x] Docker Desktop started and `docker compose -f docker/docker-compose.yml up -d` running (`flagsmith` `:8000`, `flagsmith-task-processor` `:8001`, `flagsmith-mcp` `:8002`, `flagsmith_postgres` healthy; ~980.5 MiB total RAM)
  - [x] Org, project `verdict`, `Development` environment, `Flagsmith database` warehouse, `result_view` (50/50 `dashboard`/`verdict`), metrics (`shipped_ambiguous`, `held_clear_win`), and `result_view_experiment` (id `1`) configured via UI (screenshot: `final_step_87.png`)
  - [x] Gate G1a + G1b + G1c verified via `scripts/g1_check.py`: `$flag_exposure` + `shipped_ambiguous` persisted in `experimentation_sdkexperimentationevent`; `exposures/refresh` + `GET exposures/` returned `{"verdict": 1}` with `as_of`; 54 MCP tools logged in `docs/FLAGSMITH_NOTES.md`.

## Phase 1 - Quiz live (CODE + SIMULATION VERIFIED; G2 TUNNEL AWAITING APPROVAL)
- **Goal**: Build FastAPI + Jinja2 quiz, scenarios JSON, both views (`dashboard` and `verdict`), signed cookie identity, exposure de-duplication, metric events, `/done` page with SDK flush, SQLite log, comparison script, and verify 20 simulated visits in a separate `Simulation` environment (`sim-` prefix).
- **Definition of done**:
  - 20 simulated visits show matching split and event counts in Flagsmith and SQLite within tolerance.
  - Only port 3000 exposed publicly (Flagsmith 8000/8001/8002 bound to `127.0.0.1`).
  - Gate G2 tunnel option researched and proposed (not started until approved; `Production` experiment not launched until `EXPERIMENT.md` freeze time is committed).
- **Checklist**:
  - [x] `README.md` & `docs/PRD.md` updated (Flagsmith `srm_p_value` + cross-check; `held_clear_win` SQLite guardrail; Verdict differentiators)
  - [x] `docker/docker-compose.yml` ports locked to `127.0.0.1` (`8000`, `8001`, `8002`), verified via `netstat -ano`
  - [x] `Simulation` environment (`GnPdS5LfRUt3FLAciFMVkt`) + `result_view_sim` (id `2`) created cleanly, leaving `Development` (id `1`) and `Production` untouched
  - [x] N5b single API attempt tested (`POST .../experiments/1/metrics/` -> `400 expected_direction`) and logged
  - [x] `scenarios/s1.json`, `scenarios/s2.json`, `verdict/memo.py`, `quiz/` (`app.py`, `config.py`, `db.py`, `flagsmith_sdk.py`, `security.py`, `simulate.py`, templates, CSS), `scripts/compare_counts.py`, `tests/test_quiz.py` implemented
  - [x] 20 `sim-` visits completed; `scripts/compare_counts.py --env simulation --refresh` confirmed exact match: SQLite `20` exposures (`11 dashboard`, `9 verdict`), `11` `shipped_ambiguous` (`7`/`4`), `4` `held_clear_win`; Flagsmith `control: 11`, `verdict: 9` and `shipped_ambiguous: 7 + 4 = 11`
  - [x] Double-click `Occurrence` deduplication confirmed in Flagsmith `results_query.py` and empirically (`12` raw `shipped_ambiguous` events -> `11` distinct / `11` counted)
  - [x] `ruff check` clean (`All checks passed!`) and `pytest -v` (`4 passed`)
  - [ ] Gate G2 tunnel started (awaiting user approval on Cloudflare Quick Tunnels + `EXPERIMENT.md` freeze time for `Production` launch)
