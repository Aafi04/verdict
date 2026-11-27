"""Render the static Verdict HTML results page from a fixture or Flagsmith Simulation."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from verdict.audit import DEFAULT_AUDIT_PATH, DEFAULT_LOOKS_PATH, count_prior_looks, read_looks
from verdict.checks import run_all_checks
from verdict.client.fixture import FixtureClient
from verdict.client.rest import RestClient
from verdict.cohort import build_cohort_comparison
from verdict.config import FREEZE_UTC, VerdictThresholds, resolve_env_config
from verdict.memo import memo_from_verdict
from verdict.policy import evaluate_verdict

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "verdict" / "templates"


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
        )
        if db_path is None:
            db_path = Path("data/quiz.db")

    try:
        experiment = client.get_experiment(experiment_arg)
        exposure = client.get_exposures(experiment_arg)
        results = client.get_results(experiment_arg)
    finally:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            close_fn()

    thresholds = VerdictThresholds(default_planned_sample=planned_sample)
    prior_looks = count_prior_looks(experiment.id, looks_path=looks_path)
    checks = run_all_checks(experiment, exposure, results, prior_looks=prior_looks, thresholds=thresholds)
    verdict = evaluate_verdict(experiment, exposure, results, checks, thresholds)
    cohort = build_cohort_comparison(
        experiment,
        exposure,
        verdict,
        db_path=db_path,
        prior_looks=prior_looks,
        thresholds=thresholds,
    )
    memo = memo_from_verdict(experiment, exposure, results, verdict, comparison=cohort)

    audit_all = read_looks(audit_path, limit=100)
    audit_entries = [a for a in audit_all if str(a.get("id")) == str(experiment.id)]

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
        audit_entries=audit_entries,
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
