"""Deterministic SHIP / HOLD / REJECT decision policy (PRD Section 7)."""

from __future__ import annotations

import hashlib
import json

from verdict.config import VerdictThresholds
from verdict.models import (
    CheckResult,
    Experiment,
    ExperimentResults,
    Exposure,
    VerdictDecision,
    VerdictResult,
)

INTEGRITY_CHECK_NAMES = {"srm", "arms_present", "exposure_event_ratio"}


def _compute_verdict_hash(
    experiment: Experiment,
    exposure: Exposure,
    results: ExperimentResults,
    verdict: VerdictDecision,
    checks: list[CheckResult],
) -> str:
    canonical = {
        "experiment_id": experiment.id,
        "feature_name": experiment.feature_name,
        "exposures_as_of": exposure.as_of,
        "results_as_of": results.as_of,
        "counts": exposure.counts,
        "verdict": verdict,
        "checks": [(c.name, c.status) for c in checks],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_verdict(
    experiment: Experiment,
    exposure: Exposure,
    results: ExperimentResults,
    checks: list[CheckResult],
    thresholds: VerdictThresholds | None = None,
) -> VerdictResult:
    """Compute SHIP / HOLD / REJECT deterministically from CheckResults.

    Evaluation order (first match wins):
      1. Any integrity check FAIL -> REJECT (invalid data).
      2. Guardrail check FAIL -> REJECT (guardrail regression).
      3. sample_reached HOLD or interval_zero HOLD -> HOLD.
      4. Otherwise -> SHIP.
    """
    _ = thresholds or VerdictThresholds()
    by_name = {c.name: c for c in checks}
    primary = next((m for m in results.metrics if m.role == "primary"), None)
    candidate_arm = primary.variant_arm if primary else None

    integrity_fails = [
        c for c in checks if c.name in INTEGRITY_CHECK_NAMES and c.status == "FAIL"
    ]
    guardrail_chk = by_name.get("guardrail")
    hold_checks = [
        c
        for c in checks
        if c.name in {"sample_reached", "interval_zero", "guardrail"} and c.status == "HOLD"
    ]

    if integrity_fails:
        verdict: VerdictDecision = "REJECT"
        reasons = [c.detail for c in integrity_fails]
        next_action = "Do NOT ship. Fix experiment tracking/assignment before trusting results."
        winning_arm = None
    elif guardrail_chk is not None and guardrail_chk.status == "FAIL":
        verdict = "REJECT"
        reasons = [guardrail_chk.detail]
        next_action = "Do NOT ship. Guardrail regressed; keep control or stop experiment."
        winning_arm = None
    elif hold_checks:
        verdict = "HOLD"
        reasons = [c.detail for c in hold_checks]
        if (prob_chk := by_name.get("prob_vs_interval")) and prob_chk.status == "WARN":
            reasons.append(prob_chk.detail)
        next_action = (
            "Keep running until planned sample is reached, guardrail interval is available, "
            "and 95% CI excludes zero."
            if guardrail_chk is not None and guardrail_chk.status == "HOLD"
            else "Keep running until planned sample is reached and 95% CI excludes zero."
        )
        winning_arm = None
    else:
        verdict = "SHIP"
        reasons = [
            by_name["interval_zero"].detail if "interval_zero" in by_name else "Interval favourable.",
            by_name["sample_reached"].detail if "sample_reached" in by_name else "Sample reached.",
        ]
        if guardrail_chk is not None:
            reasons.append(guardrail_chk.detail)
        winning_arm = candidate_arm
        next_action = (
            f"Run `verdict apply {experiment.id}` (dry-run by default) to review and approve "
            f"rolling out '{winning_arm}'."
        )

    verdict_hash = _compute_verdict_hash(experiment, exposure, results, verdict, checks)
    return VerdictResult(
        verdict=verdict,
        verdict_hash=verdict_hash,
        checks=checks,
        reasons=reasons,
        next_action=next_action,
        winning_arm=winning_arm,
    )
