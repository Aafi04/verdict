"""Offline FlagsmithClient implementation backed by data/fixtures/*.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from verdict.models import (
    Experiment,
    ExperimentResults,
    Exposure,
    Interval,
    MetricResult,
    ProposedChange,
)


class FixtureClient:
    """Read experiment snapshots from local JSON fixture files with zero network."""

    def __init__(self, fixtures_dir: Path = Path("data/fixtures")) -> None:
        self.fixtures_dir = fixtures_dir

    def _load(self, experiment_id: str) -> dict[str, Any]:
        safe_name = Path(experiment_id).name
        path = self.fixtures_dir / f"{safe_name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Fixture not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def get_experiment(self, experiment_id: str) -> Experiment:
        raw = self._load(experiment_id)["experiment"]
        return Experiment.model_validate(raw)

    def get_exposures(self, experiment_id: str) -> Exposure:
        raw = self._load(experiment_id)["exposures"]
        return Exposure.model_validate(raw)

    def get_results(self, experiment_id: str) -> ExperimentResults:
        raw = self._load(experiment_id)["results"]
        metrics: list[MetricResult] = []
        for m in raw.get("metrics", []):
            ci_low = m.get("ci_low")
            ci_high = m.get("ci_high")
            interval = (
                Interval(low=float(ci_low), high=float(ci_high))
                if ci_low is not None and ci_high is not None
                else None
            )
            metrics.append(
                MetricResult(
                    name=m["name"],
                    role=m.get("role", "primary"),
                    expected_direction=m.get("expected_direction", "increase"),
                    control_arm=m.get("control_arm", "control"),
                    variant_arm=m.get("variant_arm", "variant"),
                    control_n=int(m.get("control_n", 0)),
                    control_events=int(m.get("control_events", 0)),
                    variant_n=int(m.get("variant_n", 0)),
                    variant_events=int(m.get("variant_events", 0)),
                    relative_lift=m.get("relative_lift"),
                    interval=interval,
                    win_probability=m.get("win_probability"),
                )
            )
        return ExperimentResults(
            as_of=raw.get("as_of"),
            srm_p_value=raw.get("srm_p_value"),
            metrics=metrics,
        )

    def propose_rollout(self, experiment_id: str, arm: str) -> ProposedChange:
        exp = self.get_experiment(experiment_id)
        return ProposedChange(
            experiment_id=exp.id,
            feature_name=exp.feature_name,
            winning_arm=arm,
            description=f"[FIXTURE DRY-RUN] Set '{exp.feature_name}' to 100% '{arm}'.",
            payload={"feature": exp.feature_name, "value": arm, "allocation": 100.0},
        )

    def apply(self, change: ProposedChange) -> dict[str, Any]:
        raise RuntimeError("FixtureClient is offline-only and cannot mutate Flagsmith.")
