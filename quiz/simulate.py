"""Synthetic respondent traffic generator for the Track 1 quiz.

Fixed generative model (committed before generating any synthetic data; never
tuned after observing results):
  - Scenarios:
      s1 (ambiguous, primary metric `shipped_ambiguous`):
        P(ship | dashboard) = 0.60
        P(ship | verdict)   = 0.42  (18 pp reduction, near pre-registered 20 pp MDE)
      s2 (clear win, guardrail `held_clear_win`):
        P(hold | dashboard) = 0.08
        P(hold | verdict)   = 0.09  (no material guardrail regression)
  - Post-exposure completion rate:
      P(complete | exposed) = 0.88 equally in both arms (12% drop-off after /s1)
  - Sample size default:
      `max(0, 200 - real_exposures)` read from SQLite (`synthetic=0`)

Every synthetic visitor carries a `sim-` identity prefix in Flagsmith and is
recorded with `synthetic=1` (and `simulated=1`) in SQLite. Authenticated in
Production via the `X-Verdict-Synthetic` header (`QUIZ_SYNTHETIC_TOKEN`),
without ever forging `X-Forwarded-For` headers.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import httpx

from quiz.config import load_dotenv
from quiz.db import QuizDatabase

SIM_SEED_DEFAULT = 20261126
PLANNED_TOTAL_TARGET = 200
P_SHIP_S1_DASHBOARD = 0.60
P_SHIP_S1_VERDICT = 0.42
P_HOLD_S2_DASHBOARD = 0.08
P_HOLD_S2_VERDICT = 0.09
P_COMPLETE_POST_EXPOSURE = 0.88


def _detect_arm(html: str) -> str:
    if "VERDICT:" in html:
        return "verdict"
    if "Chance to Beat Control" in html:
        return "dashboard"
    return "unknown"


def default_synthetic_count(db_path: Path, target_total: int = PLANNED_TOTAL_TARGET) -> int:
    """Compute `max(0, 200 - real_exposures)` from SQLite (`synthetic=0`)."""
    if not db_path.exists():
        return max(0, target_total)
    db = QuizDatabase(db_path)
    return max(0, target_total - db.count_real_exposures())


def build_dry_run_plan(count: int, seed: int) -> dict[str, Any]:
    """Deterministically plan synthetic visitors without any network/HTTP calls."""
    rng = random.Random(seed)
    arms = {"dashboard": 0, "verdict": 0}
    s1_ship = {"dashboard": 0, "verdict": 0}
    s2_hold = {"dashboard": 0, "verdict": 0}
    completions = {"dashboard": 0, "verdict": 0}
    visitors: list[dict[str, Any]] = []

    for idx in range(count):
        arm = "dashboard" if rng.random() < 0.50 else "verdict"
        p_s1 = P_SHIP_S1_DASHBOARD if arm == "dashboard" else P_SHIP_S1_VERDICT
        p_s2 = P_HOLD_S2_DASHBOARD if arm == "dashboard" else P_HOLD_S2_VERDICT

        s1_choice = "ship" if rng.random() < p_s1 else "hold"
        completed = rng.random() < P_COMPLETE_POST_EXPOSURE
        s2_choice = ("hold" if rng.random() < p_s2 else "ship") if completed else None

        arms[arm] += 1
        if s1_choice == "ship":
            s1_ship[arm] += 1
        if completed:
            completions[arm] += 1
            if s2_choice == "hold":
                s2_hold[arm] += 1

        visitors.append(
            {
                "index": idx + 1,
                "planned_arm": arm,
                "s1_choice": s1_choice,
                "s2_choice": s2_choice,
                "completed": completed,
            }
        )

    return {
        "dry_run": True,
        "seed": seed,
        "count": count,
        "model": {
            "s1_ship_dashboard": P_SHIP_S1_DASHBOARD,
            "s1_ship_verdict": P_SHIP_S1_VERDICT,
            "s2_hold_dashboard": P_HOLD_S2_DASHBOARD,
            "s2_hold_verdict": P_HOLD_S2_VERDICT,
            "completion_rate": P_COMPLETE_POST_EXPOSURE,
        },
        "planned_counts": {
            "exposures": arms,
            "s1_ship": s1_ship,
            "s2_hold": s2_hold,
            "completions": completions,
        },
        "visitors": visitors,
    }


def run_simulation(
    base_url: str,
    count: int,
    seed: int,
    *,
    synthetic_token: str = "",
    delay_ms: int = 0,
    test_refresh_idx: int = 0,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Send `count` synthetic quiz visits through the normal quiz HTTP routes."""
    rng = random.Random(seed)
    base = base_url.rstrip("/")
    headers: dict[str, str] = {}
    if synthetic_token:
        headers["X-Verdict-Synthetic"] = synthetic_token

    summary: dict[str, Any] = {
        "synthetic": True,
        "seed": seed,
        "count": count,
        "arms": {"dashboard": 0, "verdict": 0, "unknown": 0},
        "s1_ship": {"dashboard": 0, "verdict": 0},
        "s2_hold": {"dashboard": 0, "verdict": 0},
        "completions": {"dashboard": 0, "verdict": 0},
        "refresh_checked": False,
        "double_submit_checked": False,
    }

    for i in range(count):
        # Advance the same PRNG draws per visitor as build_dry_run_plan.
        _ = rng.random()  # unused arm preview (Flagsmith assigns the real arm at /s1)
        u_s1 = rng.random()
        u_comp = rng.random()
        u_s2 = rng.random()

        client_ctx = (
            client_factory(headers=headers)
            if client_factory is not None
            else httpx.Client(
                base_url=base,
                timeout=15.0,
                follow_redirects=False,
                headers=headers,
            )
        )
        with client_ctx as c:
            r0 = c.get("/")
            r0.raise_for_status()
            cookie_val = c.cookies.get("vid", "")
            if not cookie_val.startswith("sim-"):
                raise RuntimeError(
                    "Quiz did not issue a 'sim-' identity. Verify QUIZ_SYNTHETIC_TOKEN or QUIZ_SIMULATED_MODE."
                )

            r1 = c.get("/s1")
            r1.raise_for_status()
            arm = _detect_arm(r1.text)
            summary["arms"][arm] = summary["arms"].get(arm, 0) + 1

            if i == test_refresh_idx:
                r1_refresh = c.get("/s1")
                r1_refresh.raise_for_status()
                assert _detect_arm(r1_refresh.text) == arm, "Arm changed on refresh!"
                summary["refresh_checked"] = True

            p_ship_s1 = P_SHIP_S1_DASHBOARD if arm == "dashboard" else P_SHIP_S1_VERDICT
            s1_choice = "ship" if u_s1 < p_ship_s1 else "hold"
            if s1_choice == "ship" and arm in summary["s1_ship"]:
                summary["s1_ship"][arm] += 1

            p1 = c.post("/s1", data={"choice": s1_choice}, follow_redirects=False)
            assert p1.status_code == 303 and p1.headers["location"] == "/s2"

            if i == test_refresh_idx:
                opposite = "hold" if s1_choice == "ship" else "ship"
                p1_dup = c.post("/s1", data={"choice": opposite}, follow_redirects=False)
                assert p1_dup.status_code == 303
                summary["double_submit_checked"] = True

            # Deterministic post-exposure drop-off (0.88 completion rate equally in both arms).
            if u_comp >= P_COMPLETE_POST_EXPOSURE:
                continue

            r2 = c.get("/s2")
            r2.raise_for_status()
            p_hold_s2 = P_HOLD_S2_DASHBOARD if arm == "dashboard" else P_HOLD_S2_VERDICT
            s2_choice = "hold" if u_s2 < p_hold_s2 else "ship"
            if s2_choice == "hold" and arm in summary["s2_hold"]:
                summary["s2_hold"][arm] += 1

            p2 = c.post("/s2", data={"choice": s2_choice}, follow_redirects=False)
            assert p2.status_code == 303 and p2.headers["location"] == "/done"

            rdone = c.get("/done")
            rdone.raise_for_status()
            if arm in summary["completions"]:
                summary["completions"][arm] += 1

            if i == test_refresh_idx:
                m = re.search(r"<code>(SHIP|HOLD)</code>", rdone.text)
                assert m and m.group(1).lower() == s1_choice

        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    return summary


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run or preview synthetic Verdict quiz visitors.")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--db-path", default="data/quiz.db")
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Number of synthetic visitors (default: max(0, 200 - real exposures in SQLite))",
    )
    parser.add_argument("--seed", type=int, default=SIM_SEED_DEFAULT)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print deterministic planned arms, choices, and counts with zero HTTP calls",
    )
    args = parser.parse_args(argv)

    count = (
        max(0, int(args.count))
        if args.count is not None
        else default_synthetic_count(Path(args.db_path))
    )

    if args.dry_run:
        print(json.dumps(build_dry_run_plan(count=count, seed=args.seed), indent=2))
        return 0

    token = os.getenv("QUIZ_SYNTHETIC_TOKEN", "").strip()
    result = run_simulation(
        args.base_url,
        count,
        args.seed,
        synthetic_token=token,
        delay_ms=max(0, int(args.delay_ms)),
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
