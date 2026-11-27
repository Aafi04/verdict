"""Clear-win simulated experiment generator for Flagsmith Simulation ONLY.

Creates/uses an isolated `seeded_clear_win_demo` flag and `seeded_clear_win_sim`
experiment in the `Simulation` environment (refusing Production) with both a
primary metric (`clear_win_primary`, decrease) and guardrail (`clear_win_guardrail`,
not_increase), then emits balanced synthetic traffic (`sim-win-`) so `verdict check`
returns SHIP and `verdict apply --execute` + `verdict rollback --execute` can be
verified end-to-end without touching Production.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid

import httpx
from flagsmith import EventProcessorConfig, Flagsmith

from scripts.seed_fault import (
    SeedFaultConfigError,
    ensure_simulation_experiment,
    validate_simulation_only,
)
from verdict.config import load_dotenv


def emit_clear_win_traffic(
    *,
    base_url: str,
    sim_env_key: str,
    flag_name: str = "seeded_clear_win_demo",
    primary_event: str = "clear_win_primary",
    guardrail_event: str = "clear_win_guardrail",
    total_visitors: int = 200,
    seed: int = 20261127,
) -> dict[str, int]:
    """Emit balanced simulated traffic producing a clear SHIP outcome with stable guardrail."""
    clean = base_url.rstrip("/")
    rng = random.Random(seed)
    sdk = Flagsmith(
        environment_key=sim_env_key,
        api_url=f"{clean}/api/v1/",
        enable_events=True,
        event_processor_config=EventProcessorConfig(
            events_api_url=f"{clean}/api/",
            max_buffer_items=300,
            flush_interval_seconds=1.0,
        ),
    )
    counts = {
        "control": 0,
        "variant": 0,
        "primary_ctrl": 0,
        "primary_var": 0,
        "guardrail_ctrl": 0,
        "guardrail_var": 0,
    }
    try:
        for _ in range(total_visitors):
            vid = f"sim-win-{uuid.UUID(int=rng.getrandbits(128))}"
            flag = sdk.get_experiment_flag(flag_name, identifier=vid)
            arm = "variant" if str(getattr(flag, "value", "")) == "variant" else "control"
            counts[arm] += 1

            p_primary = 0.25 if arm == "variant" else 0.62
            p_guardrail = 0.08
            if rng.random() < p_primary:
                sdk.track_event(primary_event, identifier=vid)
                counts["primary_var" if arm == "variant" else "primary_ctrl"] += 1
            if rng.random() < p_guardrail:
                sdk.track_event(guardrail_event, identifier=vid)
                counts["guardrail_var" if arm == "variant" else "guardrail_ctrl"] += 1

        sdk.flush_events()
    finally:
        sdk.close(timeout_seconds=5)
    return counts


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Create/seed a simulated clear-win SHIP experiment in Flagsmith Simulation."
    )
    parser.add_argument("--env", default="simulation", help="Must be 'simulation'")
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261127)
    parser.add_argument("--dry-run", action="store_true", help="Print plan without touching Flagsmith")
    args = parser.parse_args()

    sim_key = os.getenv("FLAGSMITH_SIM_ENV_KEY", "").strip()
    prod_key = os.getenv("FLAGSMITH_ENV_KEY", "").strip()
    base_url = os.getenv("FLAGSMITH_URL", "http://localhost:8000").strip()
    api_key = os.getenv("FLAGSMITH_API_KEY", "").strip()

    try:
        validate_simulation_only(sim_key, prod_key, args.env)
    except SeedFaultConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(
            f"[DRY-RUN] Would create/use 'seeded_clear_win_sim' in Simulation ({base_url}) "
            f"and emit {args.total} balanced synthetic visitors (seed={args.seed})."
        )
        return 0

    if not api_key:
        print("ERROR: FLAGSMITH_API_KEY is required.", file=sys.stderr)
        return 2

    with httpx.Client(timeout=15.0, headers={"Authorization": f"Api-Key {api_key}"}) as client:
        exp = ensure_simulation_experiment(
            client,
            base_url=base_url,
            sim_env_key=sim_key,
            flag_name="seeded_clear_win_demo",
            exp_name="seeded_clear_win_sim",
            hypothesis="[SIMULATED CLEAR WIN] Demonstrate Verdict SHIP + approved apply + rollback.",
            metrics_spec=[
                {
                    "name": "clear_win_primary",
                    "direction": "down",
                    "expected_direction": "decrease",
                },
                {
                    "name": "clear_win_guardrail",
                    "direction": "down",
                    "expected_direction": "not_increase",
                },
            ],
        )

    sent = emit_clear_win_traffic(
        base_url=base_url,
        sim_env_key=sim_key,
        total_visitors=args.total,
        seed=args.seed,
    )
    print(f"Seeded experiment id={exp.get('id')} name={exp.get('name')} (simulated=True): {sent}")
    print(f"Next: verdict check {exp.get('id')} --live --env simulation --refresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
