"""Deterministic decision memo renderer for Verdict.

Verdict decisions (SHIP / HOLD / REJECT) and check results are computed
deterministically outside any LLM. By default (`VERDICT_LLM=off`), this
module renders purely from templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from verdict.models import Experiment, ExperimentResults, Exposure, VerdictResult

CheckStatus = Literal["PASS", "WARN", "FAIL", "HOLD"]
VerdictDecision = Literal["SHIP", "HOLD", "REJECT"]


@dataclass(frozen=True)
class MemoCheck:
    """Single check outcome displayed inside a Verdict memo."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class MemoInput:
    """Structured inputs required to render a Verdict decision memo."""

    experiment_name: str
    verdict: VerdictDecision
    summary: str
    primary_metric: str
    lift: str
    interval: str
    win_probability: str
    sample_status: str
    guardrail_status: str
    checks: list[MemoCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    next_action: str = ""
    simulated: bool = False


def memo_from_scenario(scenario: dict[str, Any]) -> MemoInput:
    """Build a MemoInput directly from a fixed Track 1 scenario dict."""
    memo_data = scenario["memo"]
    checks = [
        MemoCheck(name=c["name"], status=c["status"], detail=c["detail"])
        for c in memo_data.get("checks", [])
    ]
    total_exp = int(scenario["exposures_control"]) + int(scenario["exposures_variant"])
    planned = int(scenario["planned_sample"])
    sample_status = (
        f"{total_exp} / {planned} planned exposures "
        f"({scenario['exposures_control']} control, {scenario['exposures_variant']} variant)"
    )
    return MemoInput(
        experiment_name=scenario["experiment_name"],
        verdict=memo_data["verdict"],
        summary=memo_data["summary"],
        primary_metric=scenario["primary_metric"],
        lift=scenario["lift"],
        interval=scenario["credible_interval"],
        win_probability=scenario["win_probability"],
        sample_status=sample_status,
        guardrail_status=f"{scenario['guardrail_metric']}: {scenario['guardrail_delta']}",
        checks=checks,
        reasons=list(memo_data.get("reasons", [])),
        next_action=memo_data.get("next_action", ""),
        simulated=False,
    )


def render_memo_text(memo: MemoInput) -> str:
    """Render a deterministic plain-text decision memo."""
    lines = [
        f"VERDICT: {memo.verdict}" + (" [SIMULATED]" if memo.simulated else ""),
        f"Experiment: {memo.experiment_name}",
        f"Summary: {memo.summary}",
        "",
        f"Primary metric: {memo.primary_metric}",
        f"  Lift: {memo.lift} | 95% CI: {memo.interval} | Win prob: {memo.win_probability}",
        f"  Sample: {memo.sample_status}",
        f"Guardrail: {memo.guardrail_status}",
    ]
    if memo.reasons:
        lines.append("")
        lines.append("Why:")
        for reason in memo.reasons:
            lines.append(f"  - {reason}")
    if memo.checks:
        lines.append("")
        lines.append("Checks:")
        for check in memo.checks:
            lines.append(f"  [{check.status}] {check.name}: {check.detail}")
    if memo.next_action:
        lines.append("")
        lines.append(f"Recommended action: {memo.next_action}")
    return "\n".join(lines)


def memo_from_verdict(
    experiment: Experiment,
    exposure: Exposure,
    results: ExperimentResults,
    verdict: VerdictResult,
) -> MemoInput:
    """Build a MemoInput from live or fixture Verdict evaluation models."""
    primary = next((m for m in results.metrics if m.role == "primary"), None)
    guardrail = next((m for m in results.metrics if m.role == "guardrail"), None)

    if primary is None:
        primary_name = "unknown"
        lift_str = "n/a"
        interval_str = "insufficient data"
        win_prob_str = "n/a"
    else:
        primary_name = f"{primary.name} ({primary.expected_direction})"
        lift_str = (
            f"{primary.relative_lift:+.1%}"
            if primary.relative_lift is not None
            else "insufficient data"
        )
        interval_str = (
            f"[{primary.interval.low:+.1%}, {primary.interval.high:+.1%}]"
            if primary.interval is not None
            else "insufficient data (< ~50/arm)"
        )
        win_prob_str = (
            f"{primary.win_probability:.0%}"
            if primary.win_probability is not None
            else "insufficient data"
        )

    arms_desc = ", ".join(f"{arm}={cnt}" for arm, cnt in exposure.counts.items())
    sample_status = f"{exposure.total} / {experiment.planned_sample} planned ({arms_desc})"

    if guardrail is None:
        guardrail_status = "none attached (check SQLite log)"
    elif guardrail.interval is not None:
        guardrail_status = (
            f"{guardrail.name}: {guardrail.relative_lift:+.1%} "
            f"(CI [{guardrail.interval.low:+.1%}, {guardrail.interval.high:+.1%}])"
            if guardrail.relative_lift is not None
            else f"{guardrail.name}: CI [{guardrail.interval.low:+.1%}, {guardrail.interval.high:+.1%}]"
        )
    else:
        guardrail_status = (
            f"{guardrail.name}: {guardrail.control_events}/{guardrail.control_n} ctrl vs "
            f"{guardrail.variant_events}/{guardrail.variant_n} var (interval pending)"
        )

    summary_map = {
        "SHIP": f"Safe to roll out '{verdict.winning_arm}': checks and sample criteria pass.",
        "HOLD": "Do not ship yet: waiting on planned sample or conclusive credible interval.",
        "REJECT": "Do NOT ship: integrity or guardrail check failed.",
    }
    checks = [MemoCheck(name=c.name, status=c.status, detail=c.detail) for c in verdict.checks]
    return MemoInput(
        experiment_name=experiment.name,
        verdict=verdict.verdict,
        summary=summary_map[verdict.verdict],
        primary_metric=primary_name,
        lift=lift_str,
        interval=interval_str,
        win_probability=win_prob_str,
        sample_status=sample_status,
        guardrail_status=guardrail_status,
        checks=checks,
        reasons=verdict.reasons,
        next_action=verdict.next_action,
        simulated=experiment.simulated,
    )
