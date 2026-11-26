"""Typed data models for Verdict experiment evaluation and rollout proposals."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["PASS", "WARN", "FAIL", "HOLD"]
VerdictDecision = Literal["SHIP", "HOLD", "REJECT"]
Direction = Literal["increase", "decrease", "do_not_increase", "do_not_decrease"]
MetricRole = Literal["primary", "guardrail", "secondary"]


class Interval(BaseModel):
    """95% credible interval bounds for relative lift."""

    low: float
    high: float


class Experiment(BaseModel):
    """Metadata describing a Flagsmith experiment."""

    id: str
    name: str
    hypothesis: str = ""
    status: str = "running"
    feature_name: str
    planned_sample: int = 200
    simulated: bool = False


class Exposure(BaseModel):
    """Per-arm exposure counts and expected split weights."""

    as_of: str | None = None
    planned_weights: dict[str, float]
    counts: dict[str, int]
    excluded_identities: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class MetricResult(BaseModel):
    """Per-metric outcomes, lift, interval, and win probability."""

    name: str
    role: MetricRole = "primary"
    expected_direction: Direction = "increase"
    control_arm: str = "control"
    variant_arm: str = "variant"
    control_n: int = 0
    control_events: int = 0
    variant_n: int = 0
    variant_events: int = 0
    relative_lift: float | None = None
    interval: Interval | None = None
    win_probability: float | None = None


class ExperimentResults(BaseModel):
    """Statistical results snapshot for an experiment."""

    as_of: str | None = None
    srm_p_value: float | None = None
    metrics: list[MetricResult] = Field(default_factory=list)


class CheckResult(BaseModel):
    """Outcome of one deterministic integrity or statistical check."""

    name: str
    status: CheckStatus
    detail: str


class ProposedChange(BaseModel):
    """Exact flag mutation proposed after a SHIP verdict (dry-run by default)."""

    experiment_id: str
    feature_name: str
    winning_arm: str
    description: str
    payload: dict[str, Any] = Field(default_factory=dict)


class VerdictResult(BaseModel):
    """Final deterministic verdict, hash, check details, and next step."""

    verdict: VerdictDecision
    verdict_hash: str
    checks: list[CheckResult]
    reasons: list[str]
    next_action: str
    winning_arm: str | None = None
