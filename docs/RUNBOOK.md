# Verdict Production Runbook (`aafi-server.local`)

## Live Topology
- **Public redirect URL:** `https://aafi04.github.io/verdict-quiz/` (repo `Aafi04/verdict-quiz`, `main` / `/`)
- **Current Cloudflare Quick Tunnel URL:** `https://programming-wife-universities-alternatives.trycloudflare.com` -> `http://127.0.0.1:3000`
- **Host & repo path:** `aafi04@aafi-server.local` (`/home/aafi04/verdict`)
- **Active Production experiment:** `result_view_experiment_v2` (`FLAGSMITH_EXPERIMENT_ID=2`, flag `result_view`, variants `dashboard` / `verdict` 50/50, metrics `shipped_ambiguous` primary & `held_clear_win` guardrail)
- **Freeze time (pre-registered):** `2026-11-29 09:00 IST` (`2026-11-29T03:30:00Z`)

All secrets (`FLAGSMITH_API_KEY`, `FLAGSMITH_ENV_KEY`, `QUIZ_COOKIE_SECRET`) live only in `/home/aafi04/verdict/.env` (`chmod 600`) and must never be printed, logged, or committed.

---

## Services & Containers
On `aafi-server.local`:

- **Docker (`docker/docker-compose.yml`):** Flagsmith `v2.295.2` + MCP `v3.4.11` bound strictly to `127.0.0.1` (`:8000` Flagsmith API/UI, `:8001` task processor, `:8002` MCP, plus `flagsmith_postgres`).
- **`verdict-quiz.service`:** Runs `.venv/bin/uvicorn quiz.app:app --host 127.0.0.1 --port 3000` (`Restart=on-failure`, `EnvironmentFile=/home/aafi04/verdict/.env`).
- **`verdict-tunnel.service`:** Runs `/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:3000` (`Restart=always`).
  - **Do NOT restart, stop, or re-enable `verdict-tunnel.service` unless rotating the public tunnel URL.** Restarting it generates a new `*.trycloudflare.com` hostname.
- **`verdict-health.timer` / `verdict-health.service`:** Runs `/usr/local/bin/verdict-health-check` every 15 minutes (`OnBootSec=2min`, `OnUnitActiveSec=15min`), appending integrity-only snapshots (`kind: "integrity"`) to `/home/aafi04/verdict/data/looks.jsonl`.

---

## Routine Checks (Safe Before Freeze)

```bash
# 1. Service & Docker health
systemctl is-active verdict-quiz verdict-tunnel verdict-health.timer
docker compose -f /home/aafi04/verdict/docker/docker-compose.yml ps

# 2. Local quiz health endpoint
curl -i http://127.0.0.1:3000/healthz

# 3. Run integrity-only check (no conversion rates or intervals printed)
sudo /usr/local/bin/verdict-health-check

# 4. Inspect recent integrity checks (no secrets)
tail -n 10 /home/aafi04/verdict/data/looks.jsonl
```

Public availability check from another machine / phone:
```bash
curl -sI https://aafi04.github.io/verdict-quiz/
```

---

## Inspecting Flagsmith from Windows (SSH Port-Forward)
Never expose ports `8000` or `8002` publicly. Forward them locally when inspecting the UI:

```powershell
ssh -N -L 8000:127.0.0.1:8000 -L 8002:127.0.0.1:8002 aafi04@aafi-server.local
```

Then open `http://localhost:8000` locally (stop local Windows Docker Flagsmith first if port `8000` conflicts, or forward to `18000`/`18002`).

---

## Restart & Recovery Procedures

### Update application code & restart quiz only
```bash
ssh aafi04@aafi-server.local "cd /home/aafi04/verdict && git pull --ff-only && sudo systemctl restart verdict-quiz.service"
curl -s http://127.0.0.1:3000/healthz
```

### Quiz returns HTTP `503` (`Experiment service unavailable`)
`verdict-quiz` fails closed in Production (`QUIZ_SIMULATED_MODE=false`) if `FLAGSMITH_ENV_KEY` is missing or the SDK initializes as `NoopQuizSdk`.
1. Verify `QUIZ_SIMULATED_MODE=false` and `FLAGSMITH_EXPERIMENT_ID=2` in `/home/aafi04/verdict/.env` (without printing secret values).
2. Confirm `docker-flagsmith-1` is healthy (`docker compose ps`).
3. Restart `verdict-quiz.service` only and inspect `journalctl -u verdict-quiz.service -n 50`.

### Tunnel URL changed (after host reboot or tunnel restart)
1. Read the current boot's URL:
   ```bash
   journalctl -u verdict-tunnel.service -b | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -n 1
   ```
2. Update `index.html` in `Aafi04/verdict-quiz` (`<meta http-equiv="refresh">` and `window.location.replace(...)`), commit, and push to `main`.
3. Wait for GitHub Pages build and verify `curl -s https://aafi04.github.io/verdict-quiz/` points to the new URL.

### `WARN_COUNT_DIFF` or SRM warning in `verdict-health-check`
1. Wait one `verdict-health.timer` cycle (~15 min) or re-run `/usr/local/bin/verdict-health-check` (Flagsmith SDK event flushing + exposure refresh can lag by a few seconds).
2. Check `journalctl -u verdict-quiz.service -n 100` and `docker logs --tail 50 docker-flagsmith-task-processor-1`.
3. Never delete or alter recorded production rows after launch; log any incident under `## Deviations` in `docs/EXPERIMENT.md`.

---

## Pre-Freeze Policy & Freeze-Time Checklist (`2026-11-29 09:00 IST`)
- **No peeking:** Do not read conversion rates, credible intervals, or win probabilities before `2026-11-29 09:00 IST`. Only integrity checks (`kind: "integrity"`) are permitted pre-freeze.
- **At freeze (`2026-11-29 09:00 IST`):**
  1. Run final `verdict` evaluation against Production Experiment `2` (`result_view_experiment_v2`) and cross-check SQLite (`data/quiz.db`, `simulated=0`) against Flagsmith.
  2. Record exact results and `verdict/policy.py` output (`SHIP` / `HOLD` / `REJECT`) in `docs/EXPERIMENT.md`.
  3. Complete/freeze the experiment cleanly in Flagsmith and update the landing status once data collection closes.
