# Deploying Verdict + Flagsmith on Linux ($0 / self-hosted)

This guide runs Flagsmith + MCP on a Linux host with ports `8000`, `8001`, and `8002` bound strictly to `127.0.0.1` (never exposed publicly), exposing only the Track 1 quiz (`:3000`) via HTTPS reverse proxy or tunnel.

## 1. Prerequisites on the Linux host
- Docker Engine + Docker Compose v2
- Python 3.11+ (`python3 -m venv .venv`)
- Only ports `80`/`443` (or Cloudflare Tunnel outbound) open to the internet; `8000`, `8001`, `8002` stay loopback-only.

## 2. Clone & configure
```bash
git clone https://github.com/Aafi04/verdict.git
cd verdict
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[quiz]"

cp .env.example .env
# Edit .env and set:
#   FLAGSMITH_URL=http://127.0.0.1:8000
#   FLAGSMITH_ENV_KEY=<Production environment SDK key>
#   FLAGSMITH_API_KEY=<Organisation Management API key>
#   QUIZ_COOKIE_SECRET=<python3 -c "import secrets; print(secrets.token_hex(32))">
#   QUIZ_SIMULATED_MODE=false
chmod 600 .env
```

## 3. Start Flagsmith + MCP (pinned `2.295.2` images)
```bash
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml ps
```
All Flagsmith services bind to `127.0.0.1` only:
- `127.0.0.1:8000` — Flagsmith dashboard + SDK/Management API
- `127.0.0.1:8001` — Flagsmith task processor
- `127.0.0.1:8002` — Flagsmith MCP server (`/mcp`)

If starting fresh on Linux, tunnel temporarily via SSH (`ssh -N -L 8000:127.0.0.1:8000 <user>@<linux-host>`) to open `http://localhost:8000` in your browser, enable `Flagsmith database` warehouse in `Production`, create `result_view` (`dashboard`/`verdict`, 50/50), create metrics `shipped_ambiguous` & `held_clear_win`, and start `result_view_experiment`.

## 4. Run the Quiz on `127.0.0.1:3000`
Run behind TLS (Caddy/Nginx on `443` -> `127.0.0.1:3000`, or `cloudflared tunnel --url http://127.0.0.1:3000`):
```bash
.venv/bin/uvicorn quiz.app:app \
  --host 127.0.0.1 \
  --port 3000 \
  --proxy-headers \
  --forwarded-allow-ips=127.0.0.1
```
Keep `uvicorn` and the tunnel/reverse proxy running in `tmux`/`systemd` with `Restart=on-failure`.

## 5. SSH Port-Forward Recipe (read Production from Windows via `localhost`)
Never open ports `8000` or `8002` in the server firewall. Forward them over SSH from your Windows machine so Verdict CLI and `scripts/health.py` read Production through `localhost`:

**Option A — local Docker Flagsmith stopped (use standard ports `8000` / `8002`):**
```powershell
ssh -N -L 8000:127.0.0.1:8000 -L 8002:127.0.0.1:8002 <user>@<linux-host>
```
Then run normally on Windows against `http://localhost:8000` / `http://localhost:8002/mcp`.

**Option B — local Docker Flagsmith still running (use alternate local ports `18000` / `18002`):**
```powershell
ssh -N -L 18000:127.0.0.1:8000 -L 18002:127.0.0.1:8002 <user>@<linux-host>
```
Then in a second PowerShell window on Windows:
```powershell
$env:FLAGSMITH_URL = "http://127.0.0.1:18000"
$env:FLAGSMITH_MCP_URL = "http://127.0.0.1:18002/mcp"
.\.venv\Scripts\python.exe scripts\health.py --env production --refresh-exposures
```

**Sync read-only SQLite snapshot for guardrail (`held_clear_win`) cross-check:**
```powershell
scp <user>@<linux-host>:~/verdict/data/quiz.db data\quiz.prod.db
.\.venv\Scripts\python.exe scripts\health.py --env production --db-path data\quiz.prod.db
```
*(Note: for SQLite WAL safety on a live server, create the remote backup first with `ssh <user>@<linux-host> "sqlite3 ~/verdict/data/quiz.db '.backup /tmp/quiz.prod.db'"` and `scp` `/tmp/quiz.prod.db`.)*
