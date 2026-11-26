"""Offline tests for scripts/seed_fault.py safety guards, Flagsmith provisioning, and REJECT behavior."""

from __future__ import annotations

import json

import httpx
import pytest

from scripts.seed_fault import (
    SeedFaultConfigError,
    ensure_seeded_fault_experiment,
    validate_simulation_only,
)
from verdict.checks import run_all_checks
from verdict.models import Experiment, ExperimentResults, Exposure, Interval, MetricResult
from verdict.policy import evaluate_verdict


def test_seed_fault_refuses_production_environment() -> None:
    with pytest.raises(SeedFaultConfigError):
        validate_simulation_only("sim-key", "prod-key", "production")
    with pytest.raises(SeedFaultConfigError):
        validate_simulation_only("same-key", "same-key", "simulation")
    with pytest.raises(SeedFaultConfigError):
        validate_simulation_only("", "prod-key", "simulation")
    validate_simulation_only("sim-key", "prod-key", "simulation")


def test_ensure_seeded_fault_creates_flag_metric_and_experiment() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/v1/environments/sim-key/":
            return httpx.Response(200, json={"name": "Simulation", "project": 1})
        if path == "/api/v1/environments/sim-key/experiments/":
            if request.method == "GET":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(201, json={"id": 9, "name": "seeded_fault_sim", "status": "created"})
        if path == "/api/v1/projects/1/features/":
            if request.method == "GET":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                201,
                json={"id": 4, "name": "seeded_fault_demo", "multivariate_options": [{"id": 15}]},
            )
        if path == "/api/v1/environments/sim-key/experiment-metrics/":
            if request.method == "GET":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(201, json={"id": 3, "name": "fault_conversion"})
        if path == "/api/v1/environments/sim-key/experiments/9/start/":
            return httpx.Response(200, json={"id": 9, "name": "seeded_fault_sim", "status": "running"})
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    exp = ensure_seeded_fault_experiment(
        client, base_url="http://127.0.0.1:8000", sim_env_key="sim-key"
    )
    assert exp["id"] == 9
    assert exp["status"] == "running"
    assert ("POST", "/api/v1/environments/sim-key/experiments/9/start/") in calls


def test_ensure_seeded_fault_rejects_non_simulation_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "Production", "project": 1})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SeedFaultConfigError, match="Production"):
        ensure_seeded_fault_experiment(
            client, base_url="http://127.0.0.1:8000", sim_env_key="oops-prod-key"
        )


def test_seeded_fault_shape_triggers_verdict_reject() -> None:
    exp = Experiment(
        id="9",
        name="seeded_fault_sim",
        feature_name="seeded_fault_demo",
        planned_sample=100,
        simulated=True,
    )
    exposure = Exposure(
        planned_weights={"control": 0.5, "variant": 0.5},
        counts={"control": 90, "variant": 15},
    )
    results = ExperimentResults(
        metrics=[
            MetricResult(
                name="fault_conversion",
                role="primary",
                expected_direction="increase",
                control_arm="control",
                variant_arm="variant",
                control_n=90,
                control_events=22,
                variant_n=15,
                variant_events=11,
                relative_lift=1.95,
                interval=Interval(low=0.80, high=3.20),
                win_probability=0.999,
            )
        ]
    )
    checks = run_all_checks(exp, exposure, results)
    verdict = evaluate_verdict(exp, exposure, results, checks)
    assert verdict.verdict == "REJECT"
    assert json.loads(verdict.model_dump_json())["winning_arm"] is None
