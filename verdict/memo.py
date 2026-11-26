"""Deterministic decision memo renderer for Verdict.

Verdict decisions (SHIP / HOLD / REJECT) and check results are computed
deterministically outside any LLM. By default (`VERDICT_LLM=off`), this
module renders purely from templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

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
