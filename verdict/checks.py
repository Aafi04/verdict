"""Pure deterministic integrity and statistical checks for Verdict."""

from __future__ import annotations

import math

from verdict.config import VerdictThresholds
from verdict.models import (
    CheckResult,
    Direction,
    Experiment,
    ExperimentResults,
    Exposure,
    Interval,
    MetricResult,
)

# Matching Flagsmith experimentation/stats.py (_Z_95) and RESULTS_MIN_IDENTITIES_PER_VARIANT (50).
_Z_95 = 1.959963984540054
MIN_GUARDRAIL_IDENTITIES_PER_ARM = 50


def compute_proportion_diff_interval(
    control_n: int,
    control_events: int,
    variant_n: int,
    variant_events: int,
    *,
    min_per_arm: int = MIN_GUARDRAIL_IDENTITIES_PER_ARM,
) -> Interval | None:
    """95% CI for difference in proportions (variant - control) using Flagsmith's normal variance."""
    if control_n < min_per_arm or variant_n < min_per_arm or control_n < 2 or variant_n < 2:
        return None
    p_c = control_events / control_n
    p_v = variant_events / variant_n
    diff = p_v - p_c
    # Same sample-variance formula as Flagsmith experimentation/stats.py VariantStats.variance.
    var_c = max(0.0, (control_events - (control_events**2) / control_n) / (control_n - 1))
    var_v = max(0.0, (variant_events - (variant_events**2) / variant_n) / (variant_n - 1))
    se = math.sqrt(var_c / control_n + var_v / variant_n)
    return Interval(low=diff - _Z_95 * se, high=diff + _Z_95 * se)


def _gamma_q(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x) = Gamma(a, x) / Gamma(a)."""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        # Series representation for P(a, x), then Q = 1 - P
        ap = a
        term = 1.0 / a
        total = term
        for _ in range(200):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        p_val = total * math.exp(-x + a * math.log(x) - math.lgamma(a))
        return max(0.0, min(1.0, 1.0 - p_val))
    # Continued fraction (Lentz's method) for Q(a, x)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b if abs(b) > tiny else 1.0 / tiny
    h = d
    for i in range(1, 201):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    q_val = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return max(0.0, min(1.0, q_val))


def compute_srm_p_value(counts: dict[str, int], weights: dict[str, float]) -> float | None:
    """Compute chi-square goodness-of-fit p-value across configured arms."""
    arms = list(weights.keys())
    if len(arms) < 2:
        return None
    weight_sum = sum(weights.values())
    total = sum(counts.get(a, 0) for a in arms)
    if total <= 0 or weight_sum <= 0.0:
        return None

    chi2 = 0.0
    for arm in arms:
        expected = total * (weights[arm] / weight_sum)
        if expected <= 0.0:
            return None
        observed = counts.get(arm, 0)
        chi2 += ((observed - expected) ** 2) / expected

    df = len(arms) - 1
    if df == 1:
        return math.erfc(math.sqrt(chi2 / 2.0))
    if df == 2:
        return math.exp(-chi2 / 2.0)
    return _gamma_q(df / 2.0, chi2 / 2.0)


def _interval_includes_zero(interval: Interval) -> bool:
    return interval.low <= 0.0 <= interval.high


def _interval_is_favourable(interval: Interval, direction: Direction) -> bool:
    if direction in ("decrease", "do_not_increase", "not_increase"):
        return interval.high < 0.0
    return interval.low > 0.0


def _interval_regressed(interval: Interval, direction: Direction) -> bool:
    if direction in ("decrease", "do_not_increase", "not_increase"):
        return interval.low > 0.0
    return interval.high < 0.0


def check_srm(
    exposure: Exposure, results: ExperimentResults, thresholds: VerdictThresholds
) -> CheckResult:
    """Check sample ratio mismatch using Verdict's chi-square and Flagsmith's srm_p_value."""
    verdict_p = compute_srm_p_value(exposure.counts, exposure.planned_weights)
    fs_p = results.srm_p_value

    if verdict_p is None and fs_p is None:
        if exposure.total > 0 and len(exposure.planned_weights) < 2:
            return CheckResult(
                name="srm",
                status="WARN",
                detail="SRM unavailable: planned allocation weights cover fewer than 2 arms.",
            )
        return CheckResult(name="srm", status="WARN", detail="No exposures yet to test SRM.")

    v_fail = verdict_p is not None and verdict_p < thresholds.srm_p_threshold
    f_fail = fs_p is not None and fs_p < thresholds.srm_p_threshold
    parts = []
    if verdict_p is not None:
        parts.append(f"verdict_p={verdict_p:.4g}")
    if fs_p is not None:
        parts.append(f"flagsmith_p={fs_p:.4g}")
    p_desc = ", ".join(parts)
    assumed_note = (
        "; equal weights assumed (completed experiment rollout cleared by Flagsmith)"
        if exposure.weights_assumed_equal
        else ""
    )

    if v_fail or f_fail:
        return CheckResult(
            name="srm",
            status="FAIL",
            detail=(
                f"Sample ratio mismatch detected ({p_desc} < {thresholds.srm_p_threshold}"
                f"{assumed_note})."
            ),
        )
    return CheckResult(
        name="srm",
        status="PASS",
        detail=f"Observed split matches allocation ({p_desc}{assumed_note}).",
    )


def check_arms_present(exposure: Exposure, thresholds: VerdictThresholds) -> CheckResult:
    """Ensure every configured arm has exposures above minimum threshold."""
    arms = list(exposure.planned_weights.keys()) or list(exposure.counts.keys())
    empty = [a for a in arms if exposure.counts.get(a, 0) <= 0]
    if empty:
        if exposure.total < thresholds.min_exposures_per_arm:
            return CheckResult(
                name="arms_present",
                status="WARN",
                detail=(
                    f"Insufficient sample (N={exposure.total} < {thresholds.min_exposures_per_arm}): "
                    f"arm(s) without exposures yet: {', '.join(empty)}."
                ),
            )
        return CheckResult(
            name="arms_present",
            status="FAIL",
            detail=f"Missing/empty exposure arm(s): {', '.join(empty)}.",
        )
    low = [
        f"{a}={exposure.counts.get(a, 0)}"
        for a in arms
        if exposure.counts.get(a, 0) < thresholds.min_exposures_per_arm
    ]
    if low:
        return CheckResult(
            name="arms_present",
            status="WARN",
            detail=(
                f"Low exposures (< {thresholds.min_exposures_per_arm}/arm): {', '.join(low)}."
            ),
        )
    counts_str = ", ".join(f"{a}={exposure.counts.get(a, 0)}" for a in arms)
    return CheckResult(name="arms_present", status="PASS", detail=f"All arms present ({counts_str}).")


def check_sample_reached(experiment: Experiment, exposure: Exposure) -> CheckResult:
    """Check whether total exposures reached the pre-registered planned sample."""
    total = exposure.total
    planned = max(1, experiment.planned_sample)
    if total < planned:
        remaining = planned - total
        pct = round((total / planned) * 100)
        return CheckResult(
            name="sample_reached",
            status="HOLD",
            detail=(
                f"Planned sample not reached: {total} / {planned} ({pct}%, {remaining} more needed)."
            ),
        )
    return CheckResult(
        name="sample_reached",
        status="PASS",
        detail=f"Planned sample reached ({total} / {planned}).",
    )


def check_interval_zero(primary: MetricResult | None) -> CheckResult:
    """Check whether primary metric credible interval excludes zero on favourable side."""
    if primary is None or primary.interval is None:
        return CheckResult(
            name="interval_zero",
            status="HOLD",
            detail="Insufficient data: credible interval not yet available.",
        )
    iv = primary.interval
    if _interval_is_favourable(iv, primary.expected_direction):
        return CheckResult(
            name="interval_zero",
            status="PASS",
            detail=(
                f"95% interval [{iv.low:+.1%}, {iv.high:+.1%}] is strictly favourable "
                f"({primary.expected_direction})."
            ),
        )
    if _interval_includes_zero(iv):
        return CheckResult(
            name="interval_zero",
            status="HOLD",
            detail=f"95% interval [{iv.low:+.1%}, {iv.high:+.1%}] includes zero.",
        )
    return CheckResult(
        name="interval_zero",
        status="HOLD",
        detail=(
            f"95% interval [{iv.low:+.1%}, {iv.high:+.1%}] is not on the favourable side "
            f"({primary.expected_direction})."
        ),
    )


def check_prob_vs_interval(
    primary: MetricResult | None, thresholds: VerdictThresholds
) -> CheckResult:
    """Flag misleading high win probability when credible interval still includes zero."""
    if primary is None or primary.win_probability is None:
        return CheckResult(
            name="prob_vs_interval",
            status="PASS",
            detail="No win probability headline reported yet.",
        )
    high_prob = primary.win_probability > thresholds.misleading_win_prob_threshold
    uncertain = primary.interval is None or _interval_includes_zero(primary.interval)
    if high_prob and uncertain:
        return CheckResult(
            name="prob_vs_interval",
            status="WARN",
            detail=(
                f"Misleading headline: win probability is {primary.win_probability:.0%} "
                f"while interval includes zero."
            ),
        )
    return CheckResult(
        name="prob_vs_interval",
        status="PASS",
        detail=f"Win probability ({primary.win_probability:.0%}) aligns with interval.",
    )


def check_guardrail(guardrails: list[MetricResult]) -> CheckResult:
    """Verify guardrail metrics satisfy pre-registered rules (must not rise / interval excluding 0).

    Pre-registered rule (docs/EXPERIMENT.md, docs/ARCHITECTURE.md, docs/PRD.md):
      - expected_direction: not_increase ("must not rise materially")
      - guardrail worse with interval excluding 0 -> FAIL (REJECT)
      - interval unavailable or insufficient sample -> HOLD (never PASS)
    """
    if not guardrails:
        return CheckResult(
            name="guardrail",
            status="WARN",
            detail="No guardrail metric provided.",
        )

    summary_parts: list[str] = []
    for g in guardrails:
        if g.interval is not None:
            if _interval_regressed(g.interval, g.expected_direction):
                return CheckResult(
                    name="guardrail",
                    status="FAIL",
                    detail=(
                        f"Guardrail '{g.name}' regressed significantly "
                        f"(CI [{g.interval.low:+.1%}, {g.interval.high:+.1%}], "
                        f"expected {g.expected_direction}; worse with interval excluding 0)."
                    ),
                )
            summary_parts.append(
                f"{g.name} CI [{g.interval.low:+.1%}, {g.interval.high:+.1%}]"
            )
            continue

        diff_iv = compute_proportion_diff_interval(
            g.control_n, g.control_events, g.variant_n, g.variant_events
        )
        if diff_iv is None:
            return CheckResult(
                name="guardrail",
                status="HOLD",
                detail=(
                    f"Guardrail '{g.name}' interval unavailable (got {g.control_n} ctrl, "
                    f"{g.variant_n} var; requires >= {MIN_GUARDRAIL_IDENTITIES_PER_ARM}/arm); "
                    "cannot pass guardrail check."
                ),
            )

        p_c = g.control_events / g.control_n
        p_v = g.variant_events / g.variant_n
        diff_pp = (p_v - p_c) * 100.0
        low_pp = diff_iv.low * 100.0
        high_pp = diff_iv.high * 100.0
        if _interval_regressed(diff_iv, g.expected_direction):
            return CheckResult(
                name="guardrail",
                status="FAIL",
                detail=(
                    f"Guardrail '{g.name}' regressed under pre-registered rule "
                    f"('{g.expected_direction}' / worse with interval excluding 0): "
                    f"{g.control_events}/{g.control_n} ({p_c:.1%}) ctrl vs "
                    f"{g.variant_events}/{g.variant_n} ({p_v:.1%}) var; "
                    f"diff {diff_pp:+.1f} pp (95% diff CI [{low_pp:+.1f} pp, {high_pp:+.1f} pp])."
                ),
            )
        summary_parts.append(
            f"{g.name} diff {diff_pp:+.1f} pp (95% diff CI [{low_pp:+.1f} pp, {high_pp:+.1f} pp])"
        )

    return CheckResult(
        name="guardrail",
        status="PASS",
        detail=f"Guardrail metric(s) stable ({'; '.join(summary_parts)}).",
    )


def check_exposure_event_ratio(
    exposure: Exposure, results: ExperimentResults, thresholds: VerdictThresholds
) -> CheckResult:
    """Detect events without matching exposures, high identity exclusion, or flat-zero events."""
    total_exp = exposure.total
    if total_exp > 0 and exposure.excluded_identities > 0:
        excl_share = exposure.excluded_identities / (total_exp + exposure.excluded_identities)
        if excl_share > thresholds.max_excluded_identity_share:
            return CheckResult(
                name="exposure_event_ratio",
                status="FAIL",
                detail=(
                    f"{exposure.excluded_identities} identities quarantined across arms "
                    f"({excl_share:.1%} > {thresholds.max_excluded_identity_share:.0%})."
                ),
            )

    for m in results.metrics:
        ctrl_cap = max(0.0, m.control_n * thresholds.max_event_over_exposure_ratio)
        var_cap = max(0.0, m.variant_n * thresholds.max_event_over_exposure_ratio)
        if (m.control_n == 0 and m.control_events > 0) or (
            m.variant_n == 0 and m.variant_events > 0
        ):
            return CheckResult(
                name="exposure_event_ratio",
                status="FAIL",
                detail=f"Events recorded with zero exposures in metric '{m.name}'.",
            )
        if m.control_events > ctrl_cap or m.variant_events > var_cap:
            return CheckResult(
                name="exposure_event_ratio",
                status="FAIL",
                detail=(
                    f"Events exceed exposures beyond tolerance in '{m.name}' "
                    f"(ctrl {m.control_events}/{m.control_n}, var {m.variant_events}/{m.variant_n})."
                ),
            )

    primary = next((m for m in results.metrics if m.role == "primary"), None)
    if (
        primary is not None
        and total_exp >= thresholds.flat_events_exposure_threshold
        and (primary.control_events + primary.variant_events) == 0
    ):
        return CheckResult(
            name="exposure_event_ratio",
            status="WARN",
            detail=(
                f"{total_exp} exposures recorded with 0 '{primary.name}' events (check event wiring)."
            ),
        )

    return CheckResult(
        name="exposure_event_ratio",
        status="PASS",
        detail="Exposure-to-event ratios are within expected bounds.",
    )


def check_looks(prior_looks: int, sample_reached: bool) -> CheckResult:
    """Warn if checks were run repeatedly before reaching the planned sample size."""
    if prior_looks > 0 and not sample_reached:
        return CheckResult(
            name="looks",
            status="WARN",
            detail=(
                f"Peeking warning: {prior_looks} prior look(s) recorded before planned sample reached."
            ),
        )
    return CheckResult(
        name="looks",
        status="PASS",
        detail=f"Look count: {prior_looks} prior look(s).",
    )


def run_all_checks(
    experiment: Experiment,
    exposure: Exposure,
    results: ExperimentResults,
    *,
    prior_looks: int = 0,
    thresholds: VerdictThresholds | None = None,
) -> list[CheckResult]:
    """Run every deterministic Verdict check and return ordered CheckResults."""
    cfg = thresholds or VerdictThresholds()
    primary = next((m for m in results.metrics if m.role == "primary"), None)
    guardrails = [m for m in results.metrics if m.role == "guardrail"]

    sample_chk = check_sample_reached(experiment, exposure)
    return [
        check_srm(exposure, results, cfg),
        check_arms_present(exposure, cfg),
        check_exposure_event_ratio(exposure, results, cfg),
        check_guardrail(guardrails),
        sample_chk,
        check_interval_zero(primary),
        check_prob_vs_interval(primary, cfg),
        check_looks(prior_looks, sample_reached=(sample_chk.status == "PASS")),
    ]
