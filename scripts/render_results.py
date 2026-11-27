"""Render the static Verdict HTML results page from a fixture or Flagsmith environment."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from verdict.audit import DEFAULT_AUDIT_PATH, DEFAULT_LOOKS_PATH, count_prior_looks, read_looks
from verdict.checks import (
    _interval_regressed,
    compute_proportion_diff_interval,
    compute_srm_p_value,
    run_all_checks,
)
from verdict.client.fixture import FixtureClient
from verdict.client.rest import RestClient
from verdict.cohort import build_cohort_comparison
from verdict.config import FREEZE_UTC, FREEZE_UTC_ISO, VerdictThresholds, resolve_env_config
from verdict.memo import memo_from_verdict
from verdict.models import Direction, Interval, MetricResult
from verdict.policy import evaluate_verdict

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "verdict" / "templates"


def _git_short_commit() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        out = res.stdout.strip()
        return out or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _interval_bar_data(
    low: float,
    high: float,
    point: float | None,
    *,
    direction: Direction,
    is_pp: bool = False,
) -> dict[str, Any]:
    """Compute percentage positions for a horizontal CI bar with a vertical 0 line."""
    scale = 100.0
    lo_val = low * scale
    hi_val = high * scale
    pt_val = point * scale if point is not None else None

    min_v = min(lo_val, 0.0, pt_val if pt_val is not None else 0.0)
    max_v = max(hi_val, 0.0, pt_val if pt_val is not None else 0.0)
    span = max(max_v - min_v, 10.0)
    pad = span * 0.14
    axis_min = min_v - pad
    axis_max = max_v + pad
    axis_span = max(axis_max - axis_min, 1e-6)

    zero_pct = (0.0 - axis_min) / axis_span * 100.0
    left_pct = (lo_val - axis_min) / axis_span * 100.0
    width_pct = max(1.8, (hi_val - lo_val) / axis_span * 100.0)
    point_pct = ((pt_val - axis_min) / axis_span * 100.0) if pt_val is not None else None

    iv = Interval(low=low, high=high)
    if _interval_regressed(iv, direction):
        tone = "regressed"
    elif (direction in ("decrease", "do_not_increase", "not_increase") and high < 0.0) or (
        direction in ("increase", "do_not_decrease", "not_decrease") and low > 0.0
    ):
        tone = "favourable"
    else:
        tone = "uncertain"

    unit = " pp" if is_pp else "%"
    return {
        "zero_pct": round(zero_pct, 2),
        "left_pct": round(left_pct, 2),
        "width_pct": round(width_pct, 2),
        "point_pct": round(point_pct, 2) if point_pct is not None else None,
        "low_label": f"{lo_val:+.1f}{unit}",
        "high_label": f"{hi_val:+.1f}{unit}",
        "point_label": f"{pt_val:+.1f}{unit}" if pt_val is not None else "n/a",
        "tone": tone,
    }


def _metric_rate_str(events: int, n: int) -> str:
    if n <= 0:
        return f"{events} / 0 (n/a)"
    return f"{events} / {n} ({events / n:.1%})"


def _build_primary_card(primary: MetricResult | None) -> dict[str, Any]:
    if primary is None:
        return {
            "name": "unavailable",
            "direction": "n/a",
            "control_str": "n/a",
            "variant_str": "n/a",
            "lift_str": "insufficient data",
            "ci_str": "insufficient data",
            "win_prob_str": "n/a",
            "bar": None,
        }
    bar = (
        _interval_bar_data(
            primary.interval.low,
            primary.interval.high,
            primary.relative_lift,
            direction=primary.expected_direction,
            is_pp=False,
        )
        if primary.interval is not None
        else None
    )
    return {
        "name": primary.name,
        "direction": primary.expected_direction,
        "control_str": _metric_rate_str(primary.control_events, primary.control_n),
        "variant_str": _metric_rate_str(primary.variant_events, primary.variant_n),
        "lift_str": (
            f"{primary.relative_lift:+.1%}" if primary.relative_lift is not None else "pending"
        ),
        "ci_str": (
            f"[{primary.interval.low:+.1%}, {primary.interval.high:+.1%}]"
            if primary.interval is not None
            else "pending (< ~50/arm)"
        ),
        "win_prob_str": (
            f"{primary.win_probability:.1%}" if primary.win_probability is not None else "pending"
        ),
        "bar": bar,
    }


def _build_guardrail_card(guardrail: MetricResult | None) -> dict[str, Any]:
    if guardrail is None:
        return {
            "name": "none attached",
            "direction": "n/a",
            "control_str": "n/a",
            "variant_str": "n/a",
            "effect_str": "none attached",
            "ci_str": "check SQLite log",
            "bar": None,
        }
    if guardrail.interval is not None:
        bar = _interval_bar_data(
            guardrail.interval.low,
            guardrail.interval.high,
            guardrail.relative_lift,
            direction=guardrail.expected_direction,
            is_pp=False,
        )
        return {
            "name": guardrail.name,
            "direction": guardrail.expected_direction,
            "control_str": _metric_rate_str(guardrail.control_events, guardrail.control_n),
            "variant_str": _metric_rate_str(guardrail.variant_events, guardrail.variant_n),
            "effect_str": (
                f"{guardrail.relative_lift:+.1%} rel lift"
                if guardrail.relative_lift is not None
                else "relative CI"
            ),
            "ci_str": f"[{guardrail.interval.low:+.1%}, {guardrail.interval.high:+.1%}]",
            "bar": bar,
        }

    diff_iv = compute_proportion_diff_interval(
        guardrail.control_n,
        guardrail.control_events,
        guardrail.variant_n,
        guardrail.variant_events,
    )
    if diff_iv is not None:
        diff = (guardrail.variant_events / guardrail.variant_n) - (
            guardrail.control_events / guardrail.control_n
        )
        bar = _interval_bar_data(
            diff_iv.low,
            diff_iv.high,
            diff,
            direction=guardrail.expected_direction,
            is_pp=True,
        )
        return {
            "name": guardrail.name,
            "direction": guardrail.expected_direction,
            "control_str": _metric_rate_str(guardrail.control_events, guardrail.control_n),
            "variant_str": _metric_rate_str(guardrail.variant_events, guardrail.variant_n),
            "effect_str": f"{diff * 100:+.1f} pp diff",
            "ci_str": f"[{diff_iv.low * 100:+.1f} pp, {diff_iv.high * 100:+.1f} pp]",
            "bar": bar,
        }

    return {
        "name": guardrail.name,
        "direction": guardrail.expected_direction,
        "control_str": _metric_rate_str(guardrail.control_events, guardrail.control_n),
        "variant_str": _metric_rate_str(guardrail.variant_events, guardrail.variant_n),
        "effect_str": "interval unavailable",
        "ci_str": "requires >= 50/arm",
        "bar": None,
    }


def render_results_html(
    *,
    experiment_arg: str,
    use_fixture: bool = False,
    force_live: bool = False,
    env_name: str = "simulation",
    fixtures_dir: Path = Path("data/fixtures"),
    db_path: Path | None = None,
    looks_path: Path = DEFAULT_LOOKS_PATH,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    planned_sample: int = 200,
    refresh: bool = False,
    page_label: str | None = None,
    now: datetime | None = None,
    allow_pre_freeze: bool = False,
) -> str:
    """Evaluate `experiment_arg` and return rendered static HTML."""
    current = now or datetime.now(UTC)
    fixture_exists = (fixtures_dir / f"{Path(experiment_arg).name}.json").exists()
    is_fixture = use_fixture or (not force_live and fixture_exists)
    if is_fixture:
        client: Any = FixtureClient(fixtures_dir=fixtures_dir)
    else:
        if env_name.strip().lower() == "production" and current < FREEZE_UTC and not allow_pre_freeze:
            raise PermissionError(
                "Pre-freeze guard: refusing to render Production results before "
                f"{FREEZE_UTC.isoformat()} without --allow-pre-freeze."
            )
        base_url, api_key, env_key, simulated = resolve_env_config(env_name)
        client = RestClient(
            base_url=base_url,
            api_key=api_key,
            env_key=env_key,
            planned_sample=planned_sample,
            simulated=simulated,
            refresh=refresh,
        )
        if db_path is None:
            db_path = Path("data/quiz.db")

    try:
        experiment = client.get_experiment(experiment_arg)
        exposure = client.get_exposures(experiment_arg)
        results = client.get_results(experiment_arg)
        thresholds = VerdictThresholds(default_planned_sample=planned_sample)
        prior_looks = count_prior_looks(experiment.id, looks_path=looks_path)
        checks = run_all_checks(
            experiment, exposure, results, prior_looks=prior_looks, thresholds=thresholds
        )
        verdict = evaluate_verdict(experiment, exposure, results, checks, thresholds)

        proposed_description: str
        proposed_payload_json: str | None = None
        if verdict.verdict == "SHIP" and verdict.winning_arm:
            proposal = client.propose_rollout(experiment.id, verdict.winning_arm)
            proposed_description = proposal.description
            proposed_payload_json = json.dumps(proposal.payload)
        else:
            proposed_description = (
                f"Blocked by deterministic policy (verdict={verdict.verdict}); "
                "Flagsmith will NOT be modified and `verdict apply` refuses to propose a rollout."
            )
    finally:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            close_fn()

    cohort = build_cohort_comparison(
        experiment,
        exposure,
        verdict,
        db_path=db_path,
        prior_looks=prior_looks,
        thresholds=thresholds,
    )
    memo = memo_from_verdict(experiment, exposure, results, verdict, comparison=cohort)

    primary = next((m for m in results.metrics if m.role == "primary"), None)
    guardrail = next((m for m in results.metrics if m.role == "guardrail"), None)
    primary_card = _build_primary_card(primary)
    guardrail_card = _build_guardrail_card(guardrail)

    total_exp = max(0, exposure.total)
    sample_pct = min(100, round((total_exp / max(1, experiment.planned_sample)) * 100))
    arms_breakdown = [
        {
            "arm": arm,
            "count": count,
            "pct": round((count / total_exp) * 100, 1) if total_exp else 0.0,
        }
        for arm, count in exposure.counts.items()
    ]
    verdict_srm_p = compute_srm_p_value(exposure.counts, exposure.planned_weights)
    srm_chk = next((c for c in checks if c.name == "srm"), None)
    srm_card = {
        "status": srm_chk.status if srm_chk else "WARN",
        "verdict_p": f"{verdict_srm_p:.4g}" if verdict_srm_p is not None else "n/a",
        "flagsmith_p": f"{results.srm_p_value:.4g}" if results.srm_p_value is not None else "n/a",
        "detail": srm_chk.detail if srm_chk else "No SRM check recorded.",
    }

    audit_all = read_looks(audit_path, limit=100)
    audit_entries = [a for a in audit_all if str(a.get("id")) == str(experiment.id)]

    env_flag = "fixture" if is_fixture else env_name.strip().lower()
    approval_cmd = f"verdict apply {experiment.id} --live --env {env_flag} --execute"
    approval_phrase = f"APPROVE {experiment.id} {verdict.verdict_hash[:8]}"

    jinja_env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = jinja_env.get_template("results.html")
    return template.render(
        experiment=experiment,
        exposure=exposure,
        results=results,
        verdict=verdict,
        cohort=cohort,
        memo=memo,
        primary_card=primary_card,
        guardrail_card=guardrail_card,
        sample_pct=sample_pct,
        arms_breakdown=arms_breakdown,
        srm_card=srm_card,
        proposed_description=proposed_description,
        proposed_payload_json=proposed_payload_json,
        approval_cmd=approval_cmd,
        approval_phrase=approval_phrase,
        audit_entries=audit_entries,
        page_label=page_label or ("Fixture" if is_fixture else env_flag.upper()),
        freeze_utc=FREEZE_UTC_ISO,
        data_tag="data-complete",
        commit_hash=_git_short_commit(),
        generated_at=current.strftime("%Y-%m-%d %H:%M UTC"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Render static Verdict HTML results report.")
    parser.add_argument("experiment", help="Fixture name or experiment ID/name")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--env",
        choices=("simulation", "development", "production"),
        default="simulation",
    )
    parser.add_argument("--fixtures-dir", default="data/fixtures")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--looks-path", default=str(DEFAULT_LOOKS_PATH))
    parser.add_argument("--audit-path", default=str(DEFAULT_AUDIT_PATH))
    parser.add_argument("--planned-sample", type=int, default=200)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--page-label", default=None)
    parser.add_argument("--out", default="data/export/results.html")
    parser.add_argument("--allow-pre-freeze", action="store_true")
    args = parser.parse_args()

    try:
        html = render_results_html(
            experiment_arg=args.experiment,
            use_fixture=bool(args.fixture),
            force_live=bool(args.live),
            env_name=args.env,
            fixtures_dir=Path(args.fixtures_dir),
            db_path=Path(args.db_path) if args.db_path else None,
            looks_path=Path(args.looks_path),
            audit_path=Path(args.audit_path),
            planned_sample=int(args.planned_sample),
            refresh=bool(args.refresh),
            page_label=args.page_label,
            allow_pre_freeze=bool(args.allow_pre_freeze),
        )
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote static results page: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
