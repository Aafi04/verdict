"""Simulated traffic generator for the Track 1 quiz (always labelled simulated=true).

Usage:
    Start the quiz in simulation mode against the Flagsmith Simulation environment:
        $env:QUIZ_SIMULATED_MODE="true"
        uvicorn quiz.app:app --host 127.0.0.1 --port 3000
    Then run:
        python -m quiz.simulate --base-url http://127.0.0.1:3000 --count 20
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys

import httpx


def _detect_arm(html: str) -> str:
    if "VERDICT:" in html:
        return "verdict"
    if "Chance to Beat Control" in html:
        return "dashboard"
    return "unknown"


def run_simulation(base_url: str, count: int, seed: int, test_refresh_idx: int = 0) -> dict:
    """Simulate `count` full quiz visits through /, /s1, /s2, /done."""
    rng = random.Random(seed)
    base = base_url.rstrip("/")
    summary: dict = {
        "simulated": True,
        "count": count,
        "arms": {"dashboard": 0, "verdict": 0, "unknown": 0},
        "s1_ship": {"dashboard": 0, "verdict": 0},
        "s2_hold": {"dashboard": 0, "verdict": 0},
        "refresh_checked": False,
        "double_submit_checked": False,
    }

    for i in range(count):
        # Distinct synthetic X-Forwarded-For per simulated visitor to avoid tripping
        # per-IP rate limits during fast batch runs (server hashes only, stores no IP).
        headers = {"X-Forwarded-For": f"10.0.{i // 250}.{(i % 250) + 1}"}
        with httpx.Client(base_url=base, timeout=15.0, follow_redirects=False, headers=headers) as c:
            r0 = c.get("/")
            r0.raise_for_status()
            if "SIMULATED=TRUE" not in r0.text:
                raise RuntimeError(
                    "Target quiz server is not running with QUIZ_SIMULATED_MODE=true!"
                )
            cookie_val = c.cookies.get("vid", "")
            if not cookie_val.startswith("sim-"):
                raise RuntimeError(f"Expected sim- prefixed cookie, got {cookie_val!r}")

            r1 = c.get("/s1")
            r1.raise_for_status()
            arm = _detect_arm(r1.text)
            summary["arms"][arm] = summary["arms"].get(arm, 0) + 1

            # Verify refresh on /s1 preserves the exact same arm.
            if i == test_refresh_idx:
                r1_refresh = c.get("/s1")
                r1_refresh.raise_for_status()
                assert _detect_arm(r1_refresh.text) == arm, "Arm changed on refresh!"
                summary["refresh_checked"] = True

            # Realistic simulated behavior matching H1 priors (~60% ship dashboard, ~30% verdict)
            p_ship_s1 = 0.60 if arm == "dashboard" else 0.30
            s1_choice = "ship" if rng.random() < p_ship_s1 else "hold"
            if s1_choice == "ship" and arm in summary["s1_ship"]:
                summary["s1_ship"][arm] += 1

            p1 = c.post("/s1", data={"choice": s1_choice})
            assert p1.status_code == 303 and p1.headers["location"] == "/s2"

            # Verify double-click on POST /s1 keeps first choice and does not error.
            if i == test_refresh_idx:
                opposite = "hold" if s1_choice == "ship" else "ship"
                p1_dup = c.post("/s1", data={"choice": opposite})
                assert p1_dup.status_code == 303
                summary["double_submit_checked"] = True

            r2 = c.get("/s2")
            r2.raise_for_status()
            s2_choice = "hold" if rng.random() < 0.10 else "ship"
            if s2_choice == "hold" and arm in summary["s2_hold"]:
                summary["s2_hold"][arm] += 1

            p2 = c.post("/s2", data={"choice": s2_choice})
            assert p2.status_code == 303 and p2.headers["location"] == "/done"

            rdone = c.get("/done")
            rdone.raise_for_status()
            # Ensure original choice survived double-submit test.
            if i == test_refresh_idx:
                m = re.search(r"<code>(SHIP|HOLD)</code>", rdone.text)
                assert m and m.group(1).lower() == s1_choice

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run simulated visitors against Verdict quiz.")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    result = run_simulation(args.base_url, args.count, args.seed)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
