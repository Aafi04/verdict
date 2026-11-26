"""Combined vs real-only cohort evaluation and disagreement reporting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Literal

from quiz.db import QuizDatabase
from verdict.checks import run_all_checks
from verdict.config import VerdictThresholds
from verdict.models import (
    CheckResult,
    Experiment,
    ExperimentResults,
    Exposure,
    Interval,
    MetricResult,
    VerdictResult,
)
from verdict.policy import _compute_verdict_hash, evaluate_verdict

_STD_NORMAL = NormalDist()
_Z_95 = 1.959963984540054
_MIN_IDENTITIES_PER_VARIANT = 50
_MIN_CONVERSIONS_PER_VARIANT = 5


@dataclass(frozen=True)
class CohortComparison:
    """Combined vs real-only sample breakdown and disagreement summary."""

    combined_n: int
    real_n: int
    synthetic_n: int
    combined_verdict: VerdictResult
    real_only_verdict: VerdictResult
    real_only_exposure: Exposure
    real_only_results: ExperimentResults
    disagreement: bool
    disagreement_detail: str | None


def _delta_inference(
    ctrl_n: int,
    ctrl_events: int,
    var_n: int,
    var_events: int,
    *,
    lower_is_better: bool,
) -> tuple[float | None, Interval | None, float | None]:
    """Match Flagsmith `experimentation/stats.py` normal-delta relative-lift inference."""
    if (
        ctrl_n < _MIN_IDENTITIES_PER_VARIANT
        or var_n < _MIN_IDENTITIES_PER_VARIANT
        or ctrl_events < _MIN_CONVERSIONS_PER_VARIANT
        or var_events < _MIN_CONVERSIONS_PER_VARIANT
        or ctrl_events <= 0
    ):
        return None, None, None

    p_c = ctrl_events / ctrl_n
    p_v = var_events / var_n
    lift = (p_v - p_c) / p_c
    var_c = (ctrl_events - (ctrl_events**2) / ctrl_n) / (ctrl_n - 1)
    var_v = (var_events - (var_events**2) / var_n) / (var_n - 1)
    se_sq = max(0.0, var_v / (var_n * p_c**2) + (p_v**2) * var_c / (ctrl_n * p_c**4))
    se = math.sqrt(se_sq)
    if se == 0.0:
        raw_prob = 0.5 if lift == 0.0 else float(lift > 0.0)
        win_prob = (1.0 - raw_prob) if lower_is_better else raw_prob
        return lift, Interval(low=lift, high=lift), win_prob

    raw_prob = _STD_NORMAL.cdf(lift / se)
    win_prob = (1.0 - raw_prob) if lower_is_better else raw_prob
    return lift, Interval(low=lift - _Z_95 * se, high=lift + _Z_95 * se), win_prob


def evaluate_sqlite_cohort(
    base_experiment: Experiment,
    db_path: Path,
    *,
    cohort: Literal["real", "combined", "synthetic"] = "real",
    prior_looks: int = 0,
    thresholds: VerdictThresholds | None = None,
) -> tuple[Exposure, ExperimentResults, VerdictResult]:
    """Build and evaluate Exposure + ExperimentResults from a SQLite cohort."""
    cfg = thresholds or VerdictThresholds()
    exp_model = base_experiment.model_copy(
        update={
            "simulated": cohort != "real",
            "planned_sample": cfg.default_planned_sample,
        }
    )

    if not db_path.exists():
        summary = {
            "totals": {"exposures": 0, "shipped_ambiguous": 0, "held_clear_win": 0, "completions": 0},
            "by_arm": {},
        }
    else:
        summary = QuizDatabase(db_path).summarize(cohort=cohort)

    by_arm = summary["by_arm"]
    ctrl = by_arm.get("dashboard", {"exposures": 0, "shipped_ambiguous": 0, "held_clear_win": 0})
    var = by_arm.get("verdict", {"exposures": 0, "shipped_ambiguous": 0, "held_clear_win": 0})
    ctrl_n = int(ctrl["exposures"])
    var_n = int(var["exposures"])

    exposure = Exposure(
        as_of=None,
        planned_weights={"dashboard": 0.5, "verdict": 0.5},
        counts={"dashboard": ctrl_n, "verdict": var_n},
        excluded_identities=0,
    )

    p_lift, p_iv, p_prob = _delta_inference(
        ctrl_n,
        int(ctrl["shipped_ambiguous"]),
        var_n,
        int(var["shipped_ambiguous"]),
        lower_is_better=True,
    )
    g_lift, g_iv, g_prob = _delta_inference(
        ctrl_n,
        int(ctrl["held_clear_win"]),
        var_n,
        int(var["held_clear_win"]),
        lower_is_better=True,
    )

    results = ExperimentResults(
        as_of=None,
        srm_p_value=None,
        metrics=[
            MetricResult(
                name="shipped_ambiguous",
                role="primary",
                expected_direction="decrease",
                control_arm="dashboard",
                variant_arm="verdict",
                control_n=ctrl_n,
                control_events=int(ctrl["shipped_ambiguous"]),
                variant_n=var_n,
                variant_events=int(var["shipped_ambiguous"]),
                relative_lift=p_lift,
                interval=p_iv,
                win_probability=p_prob,
            ),
            MetricResult(
                name="held_clear_win",
                role="guardrail",
                expected_direction="not_increase",
                control_arm="dashboard",
                variant_arm="verdict",
                control_n=ctrl_n,
                control_events=int(ctrl["held_clear_win"]),
                variant_n=var_n,
                variant_events=int(var["held_clear_win"]),
                relative_lift=g_lift,
                interval=g_iv,
                win_probability=g_prob,
            ),
        ],
    )

    if exposure.total == 0:
        # 0 real responses is insufficient real sample (HOLD), not broken SRM tracking.
        checks = [
            CheckResult(
                name="sample_reached",
                status="HOLD",
                detail=f"Real-only exposures: 0 / {cfg.default_planned_sample} (no real responses recorded).",
            ),
            CheckResult(
                name="interval_zero",
                status="HOLD",
                detail="Insufficient data: 0 real-only exposures.",
            ),
        ]
        v_hash = _compute_verdict_hash(exp_model, exposure, results, "HOLD", checks)
        verdict = VerdictResult(
            verdict="HOLD",
            verdict_hash=v_hash,
            checks=checks,
            reasons=["Real-only cohort has 0 exposures; insufficient real data."],
            next_action="Interpret combined result as synthetic-driven or collect real responses.",
            winning_arm=None,
        )
        return exposure, results, verdict

    checks = run_all_checks(
        exp_model,
        exposure,
        results,
        prior_looks=prior_looks,
        thresholds=cfg,
    )
    verdict = evaluate_verdict(exp_model, exposure, results, checks, cfg)
    return exposure, results, verdict


def build_cohort_comparison(
    experiment: Experiment,
    combined_exposure: Exposure,
    combined_verdict: VerdictResult,
    *,
    db_path: Path | None,
    prior_looks: int = 0,
    thresholds: VerdictThresholds | None = None,
) -> CohortComparison:
    """Compute combined N, real N, synthetic N, and compare combined vs real-only verdicts."""
    cfg = thresholds or VerdictThresholds(default_planned_sample=experiment.planned_sample)
    if db_path is not None and db_path.exists():
        db = QuizDatabase(db_path)
        real_total = int(db.summarize(cohort="real")["totals"]["exposures"])
        syn_total = int(db.summarize(cohort="synthetic")["totals"]["exposures"])
        combined_n = max(combined_exposure.total, real_total + syn_total)
        # If Flagsmith combined total includes synthetic/real not yet synced or fixtures are evaluated:
        if combined_n > real_total + syn_total and experiment.simulated:
            syn_total = max(syn_total, combined_n - real_total)
        elif combined_n > real_total + syn_total:
            real_total = max(real_total, combined_n - syn_total)
    else:
        combined_n = combined_exposure.total
        real_total = 0 if experiment.simulated else combined_n
        syn_total = combined_n if experiment.simulated else 0

    if db_path is not None and db_path.exists():
        real_exp, real_res, real_verdict = evaluate_sqlite_cohort(
            experiment,
            db_path,
            cohort="real",
            prior_looks=prior_looks,
            thresholds=cfg,
        )
    elif real_total == 0:
        real_exp, real_res, real_verdict = evaluate_sqlite_cohort(
            experiment,
            Path("__nonexistent_real_db__.db"),
            cohort="real",
            prior_looks=prior_looks,
            thresholds=cfg,
        )
    else:
        # Purely real-only run with no separate SQLite split available -> mirrors combined
        real_exp = combined_exposure
        real_res = ExperimentResults()
        real_verdict = combined_verdict

    disagree = combined_verdict.verdict != real_verdict.verdict
    detail: str | None = None
    if disagree:
        detail = (
            f"VERDICT DISAGREEMENT: Combined verdict is {combined_verdict.verdict} "
            f"(N={combined_n}: {real_total} real + {syn_total} synthetic), "
            f"whereas Real-only verdict is {real_verdict.verdict} (N={real_total})."
        )

    return CohortComparison(
        combined_n=combined_n,
        real_n=real_total,
        synthetic_n=syn_total,
        combined_verdict=combined_verdict,
        real_only_verdict=real_verdict,
        real_only_exposure=real_exp,
        real_only_results=real_res,
        disagreement=disagree,
        disagreement_detail=detail,
    )
