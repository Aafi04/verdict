"""Pre-freeze integrity health check for the Verdict experiment.

Reports ONLY integrity signals (no conversion rates, lift, win probabilities,
or credible intervals before the pre-registered freeze time):
  - Exposures per arm (Flagsmith vs local SQLite)
  - Flagsmith vs SQLite exposure count diff
  - SRM p-value (from Flagsmith if available + Verdict's chi-square cross-check)
  - Quiz /healthz uptime

Every run is appended to `data/looks.jsonl`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from quiz.config import load_dotenv
from quiz.db import QuizDatabase
from verdict.config import EXPERIMENT_STARTED_AT_UTC

# Fixed freeze time from docs/EXPERIMENT.md: Nov 29, 2026, 09:00 IST (+05:30)
FREEZE_UTC = datetime(2026, 11, 29, 3, 30, 0, tzinfo=UTC)


def compute_chi2_srm_p_value(control_n: int, variant_n: int, control_weight: float = 0.5) -> float | None:
    """Exact 2-arm (df=1) chi-square p-value for sample ratio mismatch."""
    total = control_n + variant_n
    if total <= 0 or control_weight <= 0.0 or control_weight >= 1.0:
        return None
    exp_ctrl = total * control_weight
    exp_var = total * (1.0 - control_weight)
    chi2 = ((control_n - exp_ctrl) ** 2) / exp_ctrl + ((variant_n - exp_var) ** 2) / exp_var
    return math.erfc(math.sqrt(chi2 / 2.0))


def check_quiz_uptime(quiz_url: str) -> dict[str, Any]:
    """Check /healthz endpoint on the local quiz server."""
    url = f"{quiz_url.rstrip('/')}/healthz"
    t0 = time.monotonic()
    try:
        resp = httpx.get(url, timeout=3.0)
        latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
        ok = resp.status_code == 200 and resp.json().get("status") == "ok"
        return {"status": "up" if ok else "degraded", "http_status": resp.status_code, "latency_ms": latency_ms}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "down", "detail": type(exc).__name__}


def _sum_flagsmith_exposures(exposures_payload: dict[str, Any]) -> tuple[dict[str, int], str | None, int]:
    exp = exposures_payload.get("exposures") or {}
    payload = exp.get("payload") or {}
    points = (payload.get("timeseries") or {}).get("points", [])
    totals: dict[str, int] = {}
    for pt in points:
        for arm, cnt in (pt.get("new_identities") or {}).items():
            totals[arm] = totals.get(arm, 0) + int(cnt)
    excluded = int(payload.get("excluded_identities") or 0)
    return totals, exp.get("as_of"), excluded


def build_health_report(
    *,
    env_name: str,
    flagsmith_url: str,
    env_key: str,
    api_key: str,
    exp_id: str,
    db_path: Path,
    quiz_url: str,
    refresh_exposures: bool = False,
) -> dict[str, Any]:
    """Collect integrity-only metrics from SQLite, Flagsmith, and quiz /healthz."""
    now = datetime.now(UTC)
    db = QuizDatabase(db_path)
    window_start = (
        EXPERIMENT_STARTED_AT_UTC
        if env_name == "production" and str(exp_id) == "2"
        else None
    )

    def _strip_outcomes(raw: dict[str, Any]) -> dict[str, Any]:
        # Strip any conversion/outcome counts — expose ONLY exposure and completion counts!
        return {
            "total": raw["totals"]["exposures"],
            "completions": raw["totals"]["completions"],
            "by_arm": {arm: stats["exposures"] for arm, stats in raw["by_arm"].items()},
        }

    sqlite_combined = _strip_outcomes(db.summarize(cohort="combined", since=window_start))
    sqlite_real = _strip_outcomes(db.summarize(cohort="real", since=window_start))
    sqlite_synthetic = _strip_outcomes(db.summarize(cohort="synthetic", since=window_start))
    sqlite_exposures = {
        # Keep top-level total/completions/by_arm reflecting combined SQLite vs Flagsmith
        # alongside explicit real vs synthetic splits so combined == Flagsmith reconciles cleanly.
        "total": sqlite_combined["total"],
        "completions": sqlite_combined["completions"],
        "by_arm": sqlite_combined["by_arm"],
        "combined": sqlite_combined,
        "real": sqlite_real,
        "synthetic": sqlite_synthetic,
    }

    base = flagsmith_url.rstrip("/")
    headers = {"Authorization": f"Api-Key {api_key}"}
    flagsmith_snapshot_stale = False
    with httpx.Client(timeout=15.0, headers=headers) as client:
        if refresh_exposures:
            ref_resp = client.post(
                f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/exposures/refresh/"
            )
            if ref_resp.status_code == 429:
                flagsmith_snapshot_stale = True
            else:
                time.sleep(1.5)
        exp_meta = client.get(f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/").json()
        exp_payload = client.get(
            f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/exposures/"
        ).json()
        res_payload = client.get(
            f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/results/"
        ).json()

    fs_by_arm, fs_as_of, excluded_identities = _sum_flagsmith_exposures(exp_payload)
    fs_total = sum(fs_by_arm.values())

    # Read ONLY srm_p_value from results; never store or return metrics/intervals.
    fs_results_obj = res_payload.get("results") or {}
    fs_srm_p = (fs_results_obj.get("payload") or {}).get("srm_p_value")

    cohort_for_srm = (
        sqlite_real
        if env_name != "simulation" and sqlite_synthetic["total"] == 0
        else (sqlite_synthetic if env_name == "simulation" else sqlite_combined)
    )
    ctrl_n = cohort_for_srm["by_arm"].get("dashboard", 0)
    var_n = cohort_for_srm["by_arm"].get("verdict", 0)
    verdict_srm_p = compute_chi2_srm_p_value(ctrl_n, var_n, control_weight=0.5)

    srm_fail = (verdict_srm_p is not None and verdict_srm_p < 0.001) or (
        fs_srm_p is not None and float(fs_srm_p) < 0.001
    )

    if flagsmith_snapshot_stale:
        count_diff: dict[str, Any] | None = None
        integrity_status = "FAIL_SRM" if srm_fail else "STALE_SNAPSHOT"
    else:
        fs_ctrl = fs_by_arm.get("control", 0)
        fs_var = fs_by_arm.get("verdict", 0)
        compared_sqlite = sqlite_synthetic if env_name == "simulation" else sqlite_combined
        diff_total = compared_sqlite["total"] - fs_total
        diff_by_arm = {
            "dashboard_vs_control": compared_sqlite["by_arm"].get("dashboard", 0) - fs_ctrl,
            "verdict": compared_sqlite["by_arm"].get("verdict", 0) - fs_var,
        }
        count_diff = {"total": diff_total, "by_arm": diff_by_arm}
        if srm_fail:
            integrity_status = "FAIL_SRM"
        elif excluded_identities > 0:
            integrity_status = "WARN_EXCLUDED_IDENTITIES"
        elif diff_total != 0:
            integrity_status = "WARN_COUNT_DIFF"
        else:
            integrity_status = "OK"

    return {
        "ts": now.isoformat(),
        "kind": "integrity",
        "source": "scripts/health.py",
        "pre_freeze": now < FREEZE_UTC,
        "freeze_utc": FREEZE_UTC.isoformat(),
        "env": env_name,
        "experiment_id": exp_id,
        "window_start": window_start,
        "experiment_name": exp_meta.get("name"),
        "experiment_status": exp_meta.get("status"),
        "quiz_uptime": check_quiz_uptime(quiz_url),
        "sqlite_exposures": sqlite_exposures,
        "flagsmith_snapshot_stale": flagsmith_snapshot_stale,
        "flagsmith_exposures": {
            "total": fs_total,
            "by_arm": fs_by_arm,
            "excluded_identities": excluded_identities,
            "as_of": fs_as_of,
        },
        "count_diff": count_diff,
        "srm_p_value_flagsmith": fs_srm_p,
        "srm_p_value_verdict": round(verdict_srm_p, 6) if verdict_srm_p is not None else None,
        "integrity_status": integrity_status,
    }


def append_look_log(entry: dict[str, Any], looks_path: Path = Path("data/looks.jsonl")) -> None:
    """Append a single integrity look entry to data/looks.jsonl."""
    payload = {"kind": "integrity", **entry}
    looks_path.parent.mkdir(parents=True, exist_ok=True)
    with looks_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Integrity-only pre-freeze experiment health check.")
    parser.add_argument(
        "--env",
        choices=("production", "simulation", "development"),
        default="production",
    )
    parser.add_argument("--db-path", default="data/quiz.db")
    parser.add_argument("--looks-path", default="data/looks.jsonl")
    parser.add_argument("--quiz-url", default="http://127.0.0.1:3000")
    parser.add_argument("--refresh-exposures", action="store_true")
    args = parser.parse_args()

    flagsmith_url = os.getenv("FLAGSMITH_URL", "http://localhost:8000")
    api_key = os.getenv("FLAGSMITH_API_KEY", "").strip()
    if args.env == "simulation":
        env_key = os.getenv("FLAGSMITH_SIM_ENV_KEY", "").strip()
        exp_id = os.getenv("FLAGSMITH_SIM_EXPERIMENT_ID", "2").strip()
    elif args.env == "development":
        env_key = os.getenv("FLAGSMITH_DEV_ENV_KEY", "").strip()
        exp_id = os.getenv("FLAGSMITH_DEV_EXPERIMENT_ID", "1").strip()
    else:
        env_key = (
            os.getenv("FLAGSMITH_PROD_ENV_KEY", "").strip()
            or os.getenv("FLAGSMITH_ENV_KEY", "").strip()
        )
        exp_id = (
            os.getenv("FLAGSMITH_PROD_EXPERIMENT_ID", "").strip()
            or os.getenv("FLAGSMITH_EXPERIMENT_ID", "3").strip()
        )

    if not api_key or not env_key or not exp_id:
        print("ERROR: Missing Flagsmith credentials or experiment ID.", file=sys.stderr)
        return 2

    report = build_health_report(
        env_name=args.env,
        flagsmith_url=flagsmith_url,
        env_key=env_key,
        api_key=api_key,
        exp_id=exp_id,
        db_path=Path(args.db_path),
        quiz_url=args.quiz_url,
        refresh_exposures=args.refresh_exposures,
    )
    append_look_log(report, looks_path=Path(args.looks_path))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
