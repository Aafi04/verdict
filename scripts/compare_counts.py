"""Compare quiz SQLite log counts against Flagsmith experiment exposures and events.

Usage:
    python scripts/compare_counts.py --env simulation --refresh
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from quiz.config import load_dotenv
from quiz.db import QuizDatabase


def _fetch_flagsmith_api(
    base_url: str, env_key: str, api_key: str, exp_id: str, refresh: bool
) -> dict[str, Any]:
    headers = {"Authorization": f"Api-Key {api_key}"}
    base = base_url.rstrip("/")
    out: dict[str, Any] = {"refresh": {}}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        if refresh:
            for endpoint in ("exposures/refresh", "results/refresh"):
                r = client.post(
                    f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/{endpoint}/"
                )
                out["refresh"][endpoint] = {"status": r.status_code, "body": r.text}
            time.sleep(2.5)

        wh = client.get(f"{base}/api/v1/environments/{env_key}/warehouse-connections/").json()
        exp = client.get(f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/exposures/").json()
        res = client.get(f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/results/").json()
        out["warehouse"] = wh
        out["exposures"] = exp
        out["results"] = res
    return out


def _fetch_postgres_event_counts(env_key: str) -> dict[str, Any]:
    """Query Flagsmith PostgreSQL event table directly to verify raw event ingestion."""
    if not env_key.isalnum():
        return {"error": "Invalid env_key"}
    sql = (
        "SELECT event, COALESCE(value, ''), COUNT(*), COUNT(DISTINCT identifier) "
        f"FROM experimentation_sdkexperimentationevent WHERE environment_key = '{env_key}' "
        "GROUP BY event, COALESCE(value, '') ORDER BY event, COALESCE(value, '');"
    )
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "flagsmith_postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "flagsmith",
            "-At",
            "-F",
            "|",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()}
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.strip().splitlines():
        if not line:
            continue
        event, val, raw_count, distinct_ids = line.split("|")
        rows.append(
            {
                "event": event,
                "value": val,
                "raw_events": int(raw_count),
                "distinct_identities": int(distinct_ids),
            }
        )
    return {"rows": rows}


def _sum_exposure_points(exposures_payload: dict[str, Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    exp = exposures_payload.get("exposures") or {}
    points = (exp.get("payload") or {}).get("timeseries", {}).get("points", [])
    for pt in points:
        for arm, cnt in (pt.get("new_identities") or {}).items():
            totals[arm] = totals.get(arm, 0) + int(cnt)
    return totals


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Compare SQLite quiz counts vs Flagsmith.")
    parser.add_argument(
        "--env",
        choices=("simulation", "development", "production"),
        default="simulation",
    )
    parser.add_argument("--db-path", default="data/quiz.db")
    parser.add_argument("--refresh", action="store_true", help="Refresh Flagsmith exposures/results")
    args = parser.parse_args()

    flagsmith_url = os.getenv("FLAGSMITH_URL", "http://localhost:8000")
    api_key = os.getenv("FLAGSMITH_API_KEY", "").strip()
    if args.env == "simulation":
        env_key = os.getenv("FLAGSMITH_SIM_ENV_KEY", "").strip()
        exp_id = os.getenv("FLAGSMITH_SIM_EXPERIMENT_ID", "2").strip()
        simulated_filter = True
    elif args.env == "development":
        env_key = os.getenv("FLAGSMITH_ENV_KEY", "").strip()
        exp_id = os.getenv("FLAGSMITH_EXPERIMENT_ID", "1").strip()
        simulated_filter = False
    else:
        env_key = os.getenv("FLAGSMITH_PROD_ENV_KEY", "").strip()
        exp_id = os.getenv("FLAGSMITH_PROD_EXPERIMENT_ID", "").strip()
        simulated_filter = False

    if not api_key or not env_key or not exp_id:
        print("ERROR: Missing API key, env key, or experiment id.", file=sys.stderr)
        return 2

    db = QuizDatabase(Path(args.db_path))
    sqlite_summary = db.summarize(simulated=simulated_filter)

    fs_data = _fetch_flagsmith_api(flagsmith_url, env_key, api_key, exp_id, refresh=args.refresh)
    fs_exposures_by_arm = _sum_exposure_points(fs_data["exposures"])

    # Look up environment numeric ID for raw event table cross-check
    pg_counts = _fetch_postgres_event_counts(env_key)

    report = {
        "target_env": args.env,
        "simulated_filter": simulated_filter,
        "sqlite": sqlite_summary,
        "flagsmith_exposures_by_arm": fs_exposures_by_arm,
        "flagsmith_exposures_as_of": (fs_data["exposures"].get("exposures") or {}).get("as_of"),
        "flagsmith_results": fs_data["results"],
        "flagsmith_warehouse": fs_data["warehouse"],
        "flagsmith_postgres_events": pg_counts,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
