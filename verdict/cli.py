"""Command-line interface for Verdict (`verdict check`, `log`, `plan`, `apply`)."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from verdict.audit import DEFAULT_LOOKS_PATH, append_look, count_prior_looks, read_looks
from verdict.checks import run_all_checks
from verdict.client.fixture import FixtureClient
from verdict.config import VerdictThresholds
from verdict.memo import memo_from_verdict, render_memo_text
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


def _cmd_check(args: argparse.Namespace) -> int:
    fixtures_dir = Path(args.fixtures_dir)
    looks_path = Path(args.looks_path)

    if args.fixture or (fixtures_dir / f"{Path(args.experiment).name}.json").exists():
        client = FixtureClient(fixtures_dir=fixtures_dir)
    else:
        print(
            "ERROR: Live Flagsmith client wiring is scheduled for Phase 3. "
            "Pass --fixture for offline evaluation.",
            file=sys.stderr,
        )
        return 2

    experiment = client.get_experiment(args.experiment)
    exposure = client.get_exposures(args.experiment)
    results = client.get_results(args.experiment)
    prior_looks = count_prior_looks(experiment.id, looks_path=looks_path)

    thresholds = VerdictThresholds()
    checks = run_all_checks(
        experiment,
        exposure,
        results,
        prior_looks=prior_looks,
        thresholds=thresholds,
    )
    verdict = evaluate_verdict(experiment, exposure, results, checks, thresholds)
    append_look(experiment, exposure, verdict, looks_path=looks_path)

    if args.json:
        payload = {
            "experiment": experiment.model_dump(),
            "exposures": exposure.model_dump(),
            "verdict": verdict.verdict,
            "verdict_hash": verdict.verdict_hash,
            "winning_arm": verdict.winning_arm,
            "checks": [c.model_dump() for c in verdict.checks],
            "reasons": verdict.reasons,
            "next_action": verdict.next_action,
        }
        print(json.dumps(payload, indent=2))
    else:
        memo = memo_from_verdict(experiment, exposure, results, verdict)
        print(render_memo_text(memo))
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    entries = read_looks(Path(args.looks_path), limit=args.limit)
    if not entries:
        print("No look entries recorded yet.")
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


def _cmd_apply(_args: argparse.Namespace) -> int:
    print(
        "verdict apply requires live Flagsmith write wiring (Phase 3 / Gate G3). "
        "Use `verdict check <fixture> --fixture` for Phase 2.",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verdict", description="Verdict experiment auditor CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Evaluate an experiment and print a decision memo")
    p_check.add_argument("experiment", help="Experiment ID or fixture name")
    p_check.add_argument("--fixture", action="store_true", help="Load from data/fixtures/*.json")
    p_check.add_argument("--fixtures-dir", default="data/fixtures")
    p_check.add_argument("--looks-path", default=str(DEFAULT_LOOKS_PATH))
    p_check.add_argument("--json", action="store_true", help="Print JSON output")

    p_log = sub.add_parser("log", help="Show recent entries from data/looks.jsonl")
    p_log.add_argument("--looks-path", default=str(DEFAULT_LOOKS_PATH))
    p_log.add_argument("--limit", type=int, default=20)

    p_plan = sub.add_parser("plan", help="Estimate sample size for a two-arm experiment")
    p_plan.add_argument("--baseline", type=float, required=True, help="Control rate in (0, 1)")
    p_plan.add_argument("--mde", type=float, required=True, help="Absolute effect size in (0, 1)")
    p_plan.add_argument("--daily-visitors", type=int, default=0)

    p_apply = sub.add_parser("apply", help="Propose/apply winning rollout (Phase 3)")
    p_apply.add_argument("experiment")
    p_apply.add_argument("--execute", action="store_true")

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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
