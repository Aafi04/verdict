# Verdict Demo Script & Recording Runbook (Target: 2:00)

Simulation-only rollout demo + honest Production Experiment 2 walkthrough. Never touch Production flag state or `verdict-tunnel.service` during recording.

---

## 1. Pre-Recording Checklist

1. **Check Linux production stack & tunnel** (read-only):
   ```powershell
   .venv\Scripts\python scripts/status.py
   ```
   Confirm `ALL OK` and public redirect target matches `https://aafi04.github.io/verdict-quiz/`.
2. **Confirm local Flagsmith Simulation Docker stack** is up at `http://localhost:8000` (`.env` points `FLAGSMITH_URL=http://localhost:8000`, `FLAGSMITH_SIM_ENV_KEY` set).
3. **Browser tabs pre-loaded (1080p, dark/clean zoom 110%)**:
   - Tab 1: Quiz (`https://aafi04.github.io/verdict-quiz/`)
   - Tab 2: Production Experiment 2 report (`data/export/production_exp2.html`)
   - Tab 3: Simulation Seeded Fault report (`data/export/sim_seeded_fault.html`)
   - Tab 4: Simulation Clear Win v3 report (`data/export/sim_clear_win_v3.html`)
   - Tab 5: Local Flagsmith UI (`http://localhost:8000`) — open to Simulation feature `seeded_clear_win_demo_v3` so the 50/50 split before `apply` and 100% rollout after `apply` are visible on reload.
4. **Terminal prep**:
   - Single PowerShell window in `c:\Users\Aafi\Desktop\Toggle Wars`, font size 16–18 pt, `.venv` activated or prefix `.venv\Scripts\`.
   - Clear scrollback (`cls`), set clean window title (`$Host.UI.RawUI.WindowTitle = "Verdict Demo"`), **never open `.env` or print secrets**.
   - Keep `data/looks_demo.jsonl` out of Production looks or use `--looks-path $env:TEMP\looks_demo.jsonl` if doing warmups beforehand so `looks` count stays low during the recorded take.

---

## 2. Two-Minute Script

| Time | Beat | Screen | Narration & Action |
|---|---|---|---|
| **0:00 – 0:15** | **Hook** | Tab 5 (Flagsmith) / Tab 2 (`production_exp2.html`) | "Feature flag tools show a percentage win probability—here, Flagsmith shows an experiment that looks ready to call. Would you ship it? Verdict inspects the confidence interval, sample ratio mismatch, guardrails, and cohort makeup before a flag ever changes." |
| **0:15 – 0:30** | **Live Quiz & Hypothesis** | Tab 1 (`https://aafi04.github.io/verdict-quiz/`) | "This live quiz tests whether showing Verdict's memo instead of a raw win-probability dashboard changes how often reviewers hold an ambiguous experiment. Self-hosted FastAPI + Flagsmith on Linux, random UUID cookie, zero PII." |
| **0:30 – 0:55** | **Honest Production Exp 2 (`id=2`)** | Tab 2 (`production_exp2.html`) | "In Production Experiment 2, collection froze at 200 exposures: 2 real and 198 synthetic from our seeded simulator—labelled everywhere as synthetic. Verdict returns **REJECT** on combined data (`held_clear_win` 4/100 vs 12/100, +8.0 pp, 95% diff CI [+0.5 pp, +15.5 pp] triggering the guardrail) and **HOLD** on real-only (`N=2`, below the 50-per-arm minimum). We report that honestly instead of claiming human behaviour." |
| **0:55 – 1:15** | **Seeded SRM Fault (`id=4`)** | Terminal + Tab 3 (`sim_seeded_fault.html`) | Run `verdict check 4 --live --env simulation` then `verdict apply 4 --live --env simulation --execute`. "Even when raw numbers look one-sided, `seeded_fault_sim` has `control=90, variant=0`—a blatant sample ratio mismatch. Verdict blocks `apply` with exit code 1. It refuses to mutate the flag." |
| **1:15 – 1:45** | **Clear Win + Human Approval (`v3`, `id=7`)** | Terminal + Tab 4 (`sim_clear_win_v3.html`) + Tab 5 (`localhost:8000`) | Run `verdict check 7 --live --env simulation` (`SHIP`), run dry-run `verdict apply 7 --live --env simulation`, then run `verdict apply 7 --live --env simulation --execute` and type `APPROVE 7 304754d4`. Refresh Flagsmith Tab 5. "All 8 deterministic checks pass. Verdict shows the exact diff, asks for a hash-locked phrase bound to this memo, promotes `variant` to 100%, and writes an audit log." |
| **1:45 – 2:00** | **Autonomy Boundary & Close** | Terminal / Repo `README` | "Stats and decisions run in deterministic Python (`verdict/policy.py`), never an LLM. Read checks are autonomous; any Flagsmith mutation requires typed human approval—and `rollback` is one typed command away. Zero-dollar self-hosted Flagsmith, full audit trail, honest when data isn't ready." |

---

## 3. Recording Command Sheet (Exact IDs & Phrases)

Use **Take 1 (`v3`)** first. Do **not** run `apply` ahead of time—each experiment can only be applied once cleanly before Flagsmith marks it `completed`.

### Core Demo Commands (Take 1 — `v3`)

```powershell
# 1. Seeded SRM fault -> REJECT (and apply refuses)
.venv\Scripts\verdict check 4 --live --env simulation
.venv\Scripts\verdict apply 4 --live --env simulation --execute

# 2. Clear-win v3 -> SHIP
.venv\Scripts\verdict check 7 --live --env simulation

# 3. Dry-run preview (default, read-only)
.venv\Scripts\verdict apply 7 --live --env simulation

# 4. Execute rollout (type exact phrase when prompted)
.venv\Scripts\verdict apply 7 --live --env simulation --execute
# Prompt input: APPROVE 7 304754d4
```

### Pre-Seeded Clear-Win Experiments (Simulation Only)

All three are synthetic (`simulated=true`, 200 exposures each) and pass all 8 checks (`SHIP`), currently un-applied:

| Take | Experiment Name | Exp ID | Flag Name | Expected Verdict | Exact Execute Command | Exact Approval Phrase |
|---|---|---|---|---|---|---|
| **Take 1 (`v3`)** | `seeded_clear_win_sim_v3` | `7` | `seeded_clear_win_demo_v3` | `SHIP` | `.venv\Scripts\verdict apply 7 --live --env simulation --execute` | `APPROVE 7 304754d4` |
| **Retake 1 (`v4`)** | `seeded_clear_win_sim_v4` | `8` | `seeded_clear_win_demo_v4` | `SHIP` | `.venv\Scripts\verdict apply 8 --live --env simulation --execute` | `APPROVE 8 d48b2140` |
| **Retake 2 (`v5`)** | `seeded_clear_win_sim_v5` | `9` | `seeded_clear_win_demo_v5` | `SHIP` | `.venv\Scripts\verdict apply 9 --live --env simulation --execute` | `APPROVE 9 02367db1` |

*Archived reference export:* `data/export/sim_clear_win.html` remains preserved for experiment `6` (`seeded_clear_win_sim_v2`). `data/export/sim_clear_win_v3.html` holds the rendered report for Take 1 (`v3`, experiment `7`).

---

## 4. Exact Reset / Retake Steps

Once `verdict apply <id> --live --env simulation --execute` succeeds, Flagsmith shifts the feature to 100% rollout and marks that experiment `completed`. **Never reuse an already-applied experiment ID for a retake.**

1. **First retake (switch to `v4`, ID `8`)**:
   - Point Flagsmith Tab 5 at feature `seeded_clear_win_demo_v4`.
   - In the script above, replace `7` with `8` and type `APPROVE 8 d48b2140`.
   - *(Optional cleanup of Take 1)*: `.venv\Scripts\verdict rollback 7 --live --env simulation --execute` -> type `ROLLBACK 7 seeded_clear_win_demo_v3`.
2. **Second retake (switch to `v5`, ID `9`)**:
   - Point Flagsmith Tab 5 at feature `seeded_clear_win_demo_v5`.
   - Replace `7` with `9` and type `APPROVE 9 02367db1`.
   - *(Optional cleanup of `v4`)*: `.venv\Scripts\verdict rollback 8 --live --env simulation --execute` -> type `ROLLBACK 8 seeded_clear_win_demo_v4`.
3. **If you need a 4th+ take (`v6+`)**:
   ```powershell
   .venv\Scripts\python -m scripts.seed_ship --env simulation --exp-name seeded_clear_win_sim_v6 --flag-name seeded_clear_win_demo_v6 --seed 2026112706
   # Wait ~2 seconds for Flagsmith to finish aggregating, then inspect hash8:
   .venv\Scripts\verdict check <NEW_ID> --live --env simulation --refresh --json
   ```
   Copy `verdict_hash[:8]` from JSON output, confirm `"verdict": "SHIP"`, and use `APPROVE <NEW_ID> <HASH8>`.

---

## 5. Offline / Static Fallback Plan

If local Docker Flagsmith or network access hiccups mid-recording:
1. Show the static self-contained HTML reports in the browser (`data/export/production_exp2.html`, `data/export/sim_seeded_fault.html`, `data/export/sim_clear_win_v3.html`).
2. Run deterministic offline fixtures in the terminal (zero network required):
   ```powershell
   .venv\Scripts\verdict check srm_fail --fixture
   .venv\Scripts\verdict apply srm_fail --fixture
   .venv\Scripts\verdict check ship --fixture
   .venv\Scripts\verdict apply ship --fixture
   ```

---

## 6. Post-Recording Checklist

1. If you rolled out `v3` (`7`), `v4` (`8`), or `v5` (`9`) during recording and want Simulation flags back at 50/50 control/variant, run the rollback command (`ROLLBACK <id> <feature_name>`).
2. Confirm Production Experiment `2` and `verdict-tunnel.service` are untouched:
   ```powershell
   .venv\Scripts\python scripts/status.py
   ```
3. Verify `git status` is clean of secrets and temporary logs before uploading the recording.
