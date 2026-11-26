"""Seeded-fault experiment generator for Flagsmith Simulation ONLY.

Creates/uses an isolated `seeded_fault_demo` flag and `seeded_fault_sim` experiment
in the `Simulation` environment (refusing Production) and emits a controlled
sample-ratio-mismatch (SRM) bug where `verdict` drop-off causes a ~85/15 split
against a 50/50 planned allocation — causing `verdict check` to return REJECT.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Any

import httpx
from flagsmith import EventProcessorConfig, Flagsmith

from verdict.config import load_dotenv


class SeedFaultConfigError(RuntimeError):
    """Raised when environment safety checks fail before seeding."""


def validate_simulation_only(sim_env_key: str, prod_env_key: str, env_arg: str) -> None:
    """Refuse to run against Production under any configuration."""
    if env_arg.strip().lower() != "simulation":
        raise SeedFaultConfigError("Refused: scripts/seed_fault.py may only target --env simulation.")
    if not sim_env_key.strip():
        raise SeedFaultConfigError("FLAGSMITH_SIM_ENV_KEY is required.")
    if prod_env_key.strip() and sim_env_key.strip() == prod_env_key.strip():
        raise SeedFaultConfigError(
            "Refused: FLAGSMITH_SIM_ENV_KEY matches FLAGSMITH_ENV_KEY (Production safety guard)."
        )


def ensure_seeded_fault_experiment(
    client: httpx.Client,
    *,
    base_url: str,
    sim_env_key: str,
    flag_name: str = "seeded_fault_demo",
    exp_name: str = "seeded_fault_sim",
) -> dict[str, Any]:
    """Ensure the Simulation environment has `seeded_fault_demo` and `seeded_fault_sim`."""
    base = base_url.rstrip("/")

    # 1. Resolve Simulation environment and project ID
    envs_resp = client.get(f"{base}/api/v1/environments/{sim_env_key}/")
    envs_resp.raise_for_status()
    env_data = envs_resp.json()
    env_name = str(env_data.get("name") or "")
    if "sim" not in env_name.lower():
        raise SeedFaultConfigError(
            f"Refused: key resolved to environment '{env_name}', expected a Simulation environment."
        )
    project_id = int(env_data["project"])

    # 2. Find or reuse existing seeded_fault_sim experiment
    exps_resp = client.get(f"{base}/api/v1/environments/{sim_env_key}/experiments/")
    exps_resp.raise_for_status()
    exp_items = exps_resp.json()
    if isinstance(exp_items, dict):
        exp_items = exp_items.get("results", [])
    for item in exp_items:
        if item.get("name") == exp_name:
            return item

    # 3. Find or create multivariate flag `seeded_fault_demo`
    feat_resp = client.get(f"{base}/api/v1/projects/{project_id}/features/?search={flag_name}")
    feat_resp.raise_for_status()
    feat_body = feat_resp.json()
    feat_list = feat_body.get("results", feat_body if isinstance(feat_body, list) else [])
    feature = next((f for f in feat_list if f.get("name") == flag_name), None)
    if feature is None:
        create_f = client.post(
            f"{base}/api/v1/projects/{project_id}/features/",
            json={
                "name": flag_name,
                "type": "MULTIVARIATE",
                "default_enabled": True,
                "initial_value": "control",
                "multivariate_options": [
                    {
                        "type": "unicode",
                        "string_value": "variant",
                        "default_percentage_allocation": 0.0,
                        "key": "variant",
                    }
                ],
            },
        )
        create_f.raise_for_status()
        feature = create_f.json()

    feature_id = int(feature["id"])
    mv_options = feature.get("multivariate_options") or []
    mv_opt_id = int(mv_options[0]["id"])

    # 4. Find or create metric `fault_conversion`
    exp_metrics_url = f"{base}/api/v1/environments/{sim_env_key}/experiment-metrics/"
    m_list_resp = client.get(exp_metrics_url)
    metric_id: int | None = None
    if m_list_resp.status_code == 200:
        m_items = m_list_resp.json()
        if isinstance(m_items, dict):
            m_items = m_items.get("results", [])
        found = next((m for m in m_items if m.get("name") == "fault_conversion"), None)
        if found:
            metric_id = int(found["id"])
    if metric_id is None:
        m_create = client.post(
            exp_metrics_url,
            json={
                "name": "fault_conversion",
                "aggregation": "occurrence",
                "direction": "up",
                "definition": {"event": "fault_conversion", "version": 1},
            },
        )
        m_create.raise_for_status()
        metric_id = int(m_create.json()["id"])

    # 5. Create and start `seeded_fault_sim` (50/50, 100% rollout)
    create_exp = client.post(
        f"{base}/api/v1/environments/{sim_env_key}/experiments/",
        json={
            "feature": feature_id,
            "name": exp_name,
            "hypothesis": "[SIMULATED FAULT] Demonstrate Verdict REJECT on a broken 50/50 split.",
            "metrics": [{"metric": metric_id, "expected_direction": "increase"}],
            "experiment_rollout": {
                "enabled": True,
                "rollout_percentage": 100.0,
                "feature_state_value": {"type": "string", "value": "control"},
                "multivariate_feature_state_values": [
                    {"multivariate_feature_option": mv_opt_id, "percentage_allocation": 50.0}
                ],
                "audience": {"match": "any", "segment_ids": []},
            },
        },
    )
    create_exp.raise_for_status()
    created = create_exp.json()
    exp_id = int(created["id"])
    start_resp = client.post(
        f"{base}/api/v1/environments/{sim_env_key}/experiments/{exp_id}/start/",
        json={},
    )
    start_resp.raise_for_status()
    return start_resp.json()


def emit_srm_fault_traffic(
    *,
    base_url: str,
    sim_env_key: str,
    flag_name: str = "seeded_fault_demo",
    event_name: str = "fault_conversion",
    control_target: int = 90,
    variant_target: int = 15,
    conversion_rate_ctrl: float = 0.25,
    conversion_rate_var: float = 0.70,
    seed_prefix: str = "sim-fault-",
) -> dict[str, int]:
    """Send skewed simulated traffic (simulating client drop-off on variant) to trigger SRM."""
    clean = base_url.rstrip("/")
    peek_sdk = Flagsmith(
        environment_key=sim_env_key,
        api_url=f"{clean}/api/v1/",
        enable_events=False,
    )
    track_sdk = Flagsmith(
        environment_key=sim_env_key,
        api_url=f"{clean}/api/v1/",
        enable_events=True,
        event_processor_config=EventProcessorConfig(
            events_api_url=f"{clean}/api/",
            max_buffer_items=250,
            flush_interval_seconds=1.0,
        ),
    )

    sent = {"control": 0, "variant": 0, "conversions": 0}
    try:
        idx = 0
        while sent["control"] < control_target or sent["variant"] < variant_target:
            idx += 1
            vid = f"{seed_prefix}{uuid.uuid4().hex[:12]}"
            peek = peek_sdk.get_experiment_flag(flag_name, identifier=vid)
            arm = "variant" if str(getattr(peek, "value", "")) == "variant" else "control"

            if arm == "control" and sent["control"] < control_target:
                track_sdk.get_experiment_flag(flag_name, identifier=vid)
                sent["control"] += 1
                if (idx % round(1.0 / conversion_rate_ctrl)) == 0:
                    track_sdk.track_event(event_name, identifier=vid)
                    sent["conversions"] += 1
            elif arm == "variant" and sent["variant"] < variant_target:
                track_sdk.get_experiment_flag(flag_name, identifier=vid)
                sent["variant"] += 1
                if (idx % round(1.0 / conversion_rate_var)) == 0 or (sent["variant"] % 3 != 0):
                    # Deliberately inflated win rate on skewed variant so naive win-prob looks great!
                    track_sdk.track_event(event_name, identifier=vid)
                    sent["conversions"] += 1

        track_sdk.flush_events()
    finally:
        peek_sdk.close(timeout_seconds=2)
        track_sdk.close(timeout_seconds=5)
    return sent


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Create/seed a simulated SRM fault experiment in Flagsmith Simulation."
    )
    parser.add_argument("--env", default="simulation", help="Must be 'simulation'")
    parser.add_argument("--control-count", type=int, default=90)
    parser.add_argument("--variant-count", type=int, default=15)
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
            f"[DRY-RUN] Would create/use 'seeded_fault_sim' in Simulation ({base_url}) "
            f"and emit simulated SRM fault ({args.control_count} control / {args.variant_count} variant)."
        )
        return 0

    if not api_key:
        print("ERROR: FLAGSMITH_API_KEY is required.", file=sys.stderr)
        return 2

    with httpx.Client(timeout=15.0, headers={"Authorization": f"Api-Key {api_key}"}) as client:
        exp = ensure_seeded_fault_experiment(client, base_url=base_url, sim_env_key=sim_key)

    sent = emit_srm_fault_traffic(
        base_url=base_url,
        sim_env_key=sim_key,
        control_target=args.control_count,
        variant_target=args.variant_count,
    )
    print(f"Seeded experiment id={exp.get('id')} name={exp.get('name')} (simulated=True): {sent}")
    print(
        f"Next: python -m verdict.cli check {exp.get('id')} --live --env simulation --refresh"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
