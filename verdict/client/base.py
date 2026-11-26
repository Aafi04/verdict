"""Abstract client protocol for accessing Flagsmith experiments."""

from __future__ import annotations

from typing import Any, Protocol

from verdict.models import Experiment, ExperimentResults, Exposure, ProposedChange


class FlagsmithClient(Protocol):
    """Uniform interface implemented by FixtureClient, McpClient, and RestClient."""

    def get_experiment(self, experiment_id: str) -> Experiment: ...

    def get_exposures(self, experiment_id: str) -> Exposure: ...

    def get_results(self, experiment_id: str) -> ExperimentResults: ...

    def propose_rollout(self, experiment_id: str, arm: str) -> ProposedChange: ...

    def apply(self, change: ProposedChange) -> dict[str, Any]: ...
