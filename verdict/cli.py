"""Command-line interface for Verdict (`verdict check`, `log`, `plan`, `apply`)."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verdict.audit import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_LOOKS_PATH,
    append_audit,
    append_look,
    count_prior_looks,
    read_looks,
)
from verdict.checks import run_all_checks
from verdict.client.base import FlagsmithClient
from verdict.client.fixture import FixtureClient
from verdict.client.rest import RestClient
from verdict.cohort import build_cohort_comparison
from verdict.config import VerdictThresholds, resolve_env_config
from verdict.memo import memo_from_verdict, render_memo_text
from verdict.models import Experiment, ExperimentResults, Exposure, VerdictResult
from verdict.policy import evaluate_verdict


def _two_proportion_sample_per_arm(baseline: float, mde: float, alpha: float = 0.05) -> int:
    """Two-sided two-proportion sample size per arm (power=0.80 default)."""
    if not (0.0 < baseline < 1.0):
        raise ValueError("baseline must be strictly between 0 and 1")
    p2 = baseline - mde if baseline - mde > 0.0 else baseline + mde
    if not (0.0 < p2 < 1.0) or mde <= 0.0:
        raise ValueError("mde must be positive and keep target rate in (0, 1)")
    p_bar = (baseline + p2) / 2.0
    z_alpha = 1.959963984540054 if abs(alpha - 0.05) < 1e-6 else 1.96
    z_beta = 0.8416212335729143
    numerator = (
        z_alpha * math.sqrt(2.0 * p_bar * (1.0 - p_bar))
        + z_beta * math.sqrt(baseline * (1.0 - baseline) + p2 * (1.0 - p2))
    ) ** 2
    return math.ceil(numerator / ((baseline - p2) ** 2))


def _build_client(
    *,
    experiment_arg: str,
    use_fixture: bool,
    force_live: bool,
    fixtures_dir: Path,
    env_name: str,
    refresh: bool,
    planned_sample: int,
) -> FlagsmithClient:
    fixture_exists = (fixtures_dir / f"{Path(experiment_arg).name}.json").exists()
    if use_fixture or (not force_live and fixture_exists):
        return FixtureClient(fixtures_dir=fixtures_dir)

    base_url, api_key, env_key, simulated = resolve_env_config(env_name)
    return RestClient(
        base_url=base_url,
        api_key=api_key,
        env_key=env_key,
        planned_sample=planned_sample,
        simulated=simulated,
        refresh=refresh,
    )


def _close_if_needed(client: FlagsmithClient) -> None:
    close_fn = getattr(client, "close", None)
    if callable(close_fn):
        close_fn()


def _evaluate_with_client(
    client: FlagsmithClient,
    experiment_arg: str,
    *,
    looks_path: Path,
    planned_sample: int,
) -> tuple[Experiment, Exposure, ExperimentResults, VerdictResult]:
    experiment = client.get_experiment(experiment_arg)
    exposure = client.get_exposures(experiment_arg)
    results = client.get_results(experiment_arg)
    prior_looks = count_prior_looks(experiment.id, looks_path=looks_path)
    thresholds = VerdictThresholds(default_planned_sample=planned_sample)
    checks = run_all_checks(
        experiment,
        exposure,
        results,
        prior_looks=prior_looks,
        thresholds=thresholds,
    )
    verdict = evaluate_verdict(experiment, exposure, results, checks, thresholds)
    return experiment, exposure, results, verdict


def _resolve_db_path(args: argparse.Namespace) -> Path | None:
    explicit = getattr(args, "db_path", None)
    if explicit:
        return Path(explicit)
    fixture_exists = (Path(args.fixtures_dir) / f"{Path(args.experiment).name}.json").exists()
    if bool(args.fixture) or (not bool(args.live) and fixture_exists):
        return None
    return Path("data/quiz.db")


def _cmd_check(args: argparse.Namespace) -> int:
    fixtures_dir = Path(args.fixtures_dir)
    looks_path = Path(args.looks_path)
    db_path = _resolve_db_path(args)

    try:
        client = _build_client(
            experiment_arg=args.experiment,
            use_fixture=bool(args.fixture),
            force_live=bool(args.live),
            fixtures_dir=fixtures_dir,
            env_name=args.env,
            refresh=bool(args.refresh),
            planned_sample=int(args.planned_sample),
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        experiment, exposure, results, verdict = _evaluate_with_client(
            client,
            args.experiment,
            looks_path=looks_path,
            planned_sample=int(args.planned_sample),
        )
    finally:
        _close_if_needed(client)

    prior_looks = count_prior_looks(experiment.id, looks_path=looks_path)
    thresholds = VerdictThresholds(default_planned_sample=int(args.planned_sample))
    comparison = build_cohort_comparison(
        experiment,
        exposure,
        verdict,
        db_path=db_path,
        prior_looks=prior_looks,
        thresholds=thresholds,
    )
    append_look(experiment, exposure, verdict, looks_path=looks_path)

    if args.json:
        payload = {
            "experiment": experiment.model_dump(),
            "exposures": exposure.model_dump(),
            "verdict": verdict.verdict,
            "combined_verdict": comparison.combined_verdict.verdict,
            "verdict_hash": verdict.verdict_hash,
            "winning_arm": verdict.winning_arm,
            "sample_breakdown": {
                "combined_n": comparison.combined_n,
                "real_n": comparison.real_n,
                "synthetic_n": comparison.synthetic_n,
            },
            "real_only": {
                "verdict": comparison.real_only_verdict.verdict,
                "verdict_hash": comparison.real_only_verdict.verdict_hash,
                "reasons": comparison.real_only_verdict.reasons,
                "next_action": comparison.real_only_verdict.next_action,
            },
            "verdict_disagreement": comparison.disagreement,
            "disagreement_detail": comparison.disagreement_detail,
            "checks": [c.model_dump() for c in verdict.checks],
            "reasons": verdict.reasons,
            "next_action": verdict.next_action,
        }
        print(json.dumps(payload, indent=2))
    else:
        memo = memo_from_verdict(experiment, exposure, results, verdict, comparison=comparison)
        print(render_memo_text(memo))
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    target = Path(args.audit_path) if args.audit else Path(args.looks_path)
    entries = read_looks(target, limit=args.limit)
    if not entries:
        label = "audit" if args.audit else "look"
        print(f"No {label} entries recorded yet.")
        return 0
    for entry in entries:
        print(json.dumps(entry))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    n_per_arm = _two_proportion_sample_per_arm(args.baseline, args.mde)
    total = n_per_arm * 2
    print(f"Baseline rate: {args.baseline:.1%} | MDE: {args.mde:.1%} (80% power, alpha=0.05)")
    print(f"Required sample: {n_per_arm} per arm ({total} total)")
    if args.daily_visitors and args.daily_visitors > 0:
        days = math.ceil(total / args.daily_visitors)
        print(f"At {args.daily_visitors} visitors/day: ~{days} day(s)")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    fixtures_dir = Path(args.fixtures_dir)
    looks_path = Path(args.looks_path)
    audit_path = Path(args.audit_path)

    try:
        client = _build_client(
            experiment_arg=args.experiment,
            use_fixture=bool(args.fixture),
            force_live=bool(args.live),
            fixtures_dir=fixtures_dir,
            env_name=args.env,
            refresh=bool(args.refresh),
            planned_sample=int(args.planned_sample),
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        experiment, _exposure, _results, verdict = _evaluate_with_client(
            client,
            args.experiment,
            looks_path=looks_path,
            planned_sample=int(args.planned_sample),
        )
        if verdict.verdict != "SHIP" or not verdict.winning_arm:
            print(
                f"REFUSED: verdict is {verdict.verdict} (not SHIP); no changes proposed.",
                file=sys.stderr,
            )
            for reason in verdict.reasons:
                print(f"  - {reason}", file=sys.stderr)
            return 1

        change = client.propose_rollout(experiment.id, verdict.winning_arm)
        hash8 = verdict.verdict_hash[:8]
        expected_phrase = f"APPROVE {experiment.id} {hash8}"

        print(f"VERDICT: SHIP (hash={verdict.verdict_hash[:12]}...)")
        print(f"Proposed change: {change.description}")
        print(f"Payload: {json.dumps(change.payload)}")

        if not args.execute or args.dry_run:
            print("\nDRY-RUN ONLY: no changes written to Flagsmith.")
            print(f"To execute, re-run with --execute and type: {expected_phrase}")
            return 0

        if isinstance(client, FixtureClient):
            print("ERROR: FixtureClient cannot execute live Flagsmith writes.", file=sys.stderr)
            return 2

        try:
            typed = input(f"Type '{expected_phrase}' to execute: ").strip()
        except EOFError:
            typed = ""

        if typed != expected_phrase:
            print("ABORTED: approval phrase did not match; no changes written.", file=sys.stderr)
            return 1

        # Re-check state immediately before writing to detect drift since approval.
        _, _, _, fresh_verdict = _evaluate_with_client(
            client,
            args.experiment,
            looks_path=looks_path,
            planned_sample=int(args.planned_sample),
        )
        if fresh_verdict.verdict != "SHIP" or fresh_verdict.verdict_hash != verdict.verdict_hash:
            print(
                "ABORTED: experiment state changed between check and write "
                f"(expected {hash8}, got {fresh_verdict.verdict_hash[:8]}).",
                file=sys.stderr,
            )
            return 1

        apply_result: dict[str, Any] = client.apply(change)
        audit_entry = {
            "ts": datetime.now(UTC).isoformat(),
            "id": experiment.id,
            "experiment_name": experiment.name,
            "simulated": experiment.simulated,
            "verdict_hash": verdict.verdict_hash,
            "change": change.model_dump(),
            "result": apply_result,
            "approver": "human-typed",
        }
        append_audit(audit_entry, audit_path=audit_path)
        print(f"APPLIED: {json.dumps(apply_result)}")
        print(f"Audit entry recorded in {audit_path}.")
        return 0
    finally:
        _close_if_needed(client)


def _cmd_rollback(args: argparse.Namespace) -> int:
    audit_path = Path(args.audit_path)
    base_url, api_key, env_key, simulated = resolve_env_config(args.env)
    client = RestClient(
        base_url=base_url,
        api_key=api_key,
        env_key=env_key,
        planned_sample=200,
        simulated=simulated,
        refresh=False,
    )
    try:
        experiment = client.get_experiment(args.experiment)
        change = client.propose_rollback(experiment.id)
        expected_phrase = f"ROLLBACK {experiment.id} {change.feature_name}"

        print(f"Proposed rollback: {change.description}")
        print(f"Payload: {json.dumps(change.payload)}")

        if not args.execute or args.dry_run:
            print("\nDRY-RUN ONLY: no changes written to Flagsmith.")
            print(f"To execute, re-run with --execute and type: {expected_phrase}")
            return 0

        try:
            typed = input(f"Type '{expected_phrase}' to execute rollback: ").strip()
        except EOFError:
            typed = ""

        if typed != expected_phrase:
            print("ABORTED: rollback approval phrase did not match; no changes written.", file=sys.stderr)
            return 1

        rb_result = client.rollback(change)
        audit_entry = {
            "ts": datetime.now(UTC).isoformat(),
            "action": "rollback",
            "id": experiment.id,
            "experiment_name": experiment.name,
            "simulated": experiment.simulated,
            "change": change.model_dump(),
            "result": rb_result,
            "approver": "human-typed",
        }
        append_audit(audit_entry, audit_path=audit_path)
        print(f"ROLLED BACK: {json.dumps(rb_result)}")
        print(f"Audit entry recorded in {audit_path}.")
        return 0
    finally:
        _close_if_needed(client)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verdict", description="Verdict experiment auditor CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Evaluate an experiment and print a decision memo")
    p_check.add_argument("experiment", help="Experiment ID, name, or fixture name")
    p_check.add_argument("--fixture", action="store_true", help="Force loading data/fixtures/*.json")
    p_check.add_argument("--live", action="store_true", help="Force live Flagsmith REST client")
    p_check.add_argument(
        "--env",
        choices=("simulation", "development", "production"),
        default="simulation",
        help="Target Flagsmith environment when running live (default: simulation)",
    )
    p_check.add_argument("--refresh", action="store_true", help="Trigger Flagsmith exposure/results refresh")
    p_check.add_argument("--planned-sample", type=int, default=200)
    p_check.add_argument("--fixtures-dir", default="data/fixtures")
    p_check.add_argument("--db-path", default=None, help="Optional SQLite DB for real vs synthetic split")
    p_check.add_argument("--looks-path", default=str(DEFAULT_LOOKS_PATH))
    p_check.add_argument("--json", action="store_true", help="Print JSON output")

    p_log = sub.add_parser("log", help="Show recent entries from looks.jsonl or audit.jsonl")
    p_log.add_argument("--looks-path", default=str(DEFAULT_LOOKS_PATH))
    p_log.add_argument("--audit-path", default=str(DEFAULT_AUDIT_PATH))
    p_log.add_argument("--audit", action="store_true", help="Read audit.jsonl instead of looks.jsonl")
    p_log.add_argument("--limit", type=int, default=20)

    p_plan = sub.add_parser("plan", help="Estimate sample size for a two-arm experiment")
    p_plan.add_argument("--baseline", type=float, required=True, help="Control rate in (0, 1)")
    p_plan.add_argument("--mde", type=float, required=True, help="Absolute effect size in (0, 1)")
    p_plan.add_argument("--daily-visitors", type=int, default=0)

    p_apply = sub.add_parser("apply", help="Propose/apply winning rollout (dry-run by default)")
    p_apply.add_argument("experiment", help="Experiment ID, name, or fixture name")
    p_apply.add_argument("--fixture", action="store_true", help="Force loading data/fixtures/*.json")
    p_apply.add_argument("--live", action="store_true", help="Force live Flagsmith REST client")
    p_apply.add_argument(
        "--env",
        choices=("simulation", "development", "production"),
        default="simulation",
        help="Target Flagsmith environment when running live (default: simulation)",
    )
    p_apply.add_argument("--refresh", action="store_true", help="Trigger Flagsmith exposure/results refresh")
    p_apply.add_argument("--planned-sample", type=int, default=200)
    p_apply.add_argument("--fixtures-dir", default="data/fixtures")
    p_apply.add_argument("--looks-path", default=str(DEFAULT_LOOKS_PATH))
    p_apply.add_argument("--audit-path", default=str(DEFAULT_AUDIT_PATH))
    p_apply.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default behavior)")
    p_apply.add_argument("--execute", action="store_true", help="Execute write after typed human approval")

    p_rb = sub.add_parser("rollback", help="Roll back an applied rollout to 100% control (dry-run by default)")
    p_rb.add_argument("experiment", help="Experiment ID or name")
    p_rb.add_argument(
        "--env",
        choices=("simulation", "development", "production"),
        default="simulation",
        help="Target Flagsmith environment (default: simulation)",
    )
    p_rb.add_argument("--audit-path", default=str(DEFAULT_AUDIT_PATH))
    p_rb.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default behavior)")
    p_rb.add_argument("--execute", action="store_true", help="Execute rollback after typed human approval")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "log":
        return _cmd_log(args)
    if args.command == "plan":
        return _cmd_plan(args)
    if args.command == "apply":
        return _cmd_apply(args)
    if args.command == "rollback":
        return _cmd_rollback(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
