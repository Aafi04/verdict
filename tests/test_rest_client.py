"""Unit tests for RestClient and live CLI wiring (mocked HTTP transport, zero network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from verdict.cli import main
from verdict.client.rest import RestClient


def _sample_detail(*, exp_id: int = 7, name: str = "result_view_sim") -> dict:
    return {
        "id": exp_id,
        "name": name,
        "hypothesis": "Verdict memo lowers premature shipping.",
        "status": "running",
        "feature": {
            "id": 1,
            "name": "result_view",
            "type": "MULTIVARIATE",
            "initial_value": "dashboard",
            "multivariate_options": [
                {"id": 10, "key": "verdict", "string_value": "verdict"},
            ],
        },
        "metrics": [
            {
                "id": 1,
                "metric": 101,
                "metric_name": "shipped_ambiguous",
                "expected_direction": "decrease",
            },
            {
                "id": 2,
                "metric": 102,
                "metric_name": "held_clear_win",
                "expected_direction": "not_increase",
            },
        ],
        "experiment_rollout": {
            "enabled": True,
            "rollout_percentage": 100.0,
            "feature_state_value": {"type": "string", "value": "dashboard"},
            "multivariate_feature_state_values": [
                {"multivariate_feature_option": 10, "percentage_allocation": 50.0}
            ],
        },
    }


def _mock_transport(
    *,
    detail: dict,
    exposures_points: list[dict],
    results_payload: dict,
    refresh_status: int = 202,
    recorded_calls: list[tuple[str, str, dict | None]] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else None
        if recorded_calls is not None:
            recorded_calls.append((request.method, path, body))

        if path.endswith("/experiments/") and request.method == "GET":
            return httpx.Response(200, json={"results": [detail]})
        if path.endswith(f"/experiments/{detail['id']}/") and request.method == "GET":
            return httpx.Response(200, json=detail)
        if path.endswith("/exposures/refresh/") and request.method == "POST":
            return httpx.Response(refresh_status, json={})
        if path.endswith("/results/refresh/") and request.method == "POST":
            return httpx.Response(refresh_status, json={})
        if path.endswith("/exposures/") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "exposures": {
                        "as_of": "2026-11-28T10:00:00Z",
                        "payload": {
                            "timeseries": {"points": exposures_points, "granularity": "hour"},
                            "excluded_identities": 0,
                        },
                    }
                },
            )
        if path.endswith("/results/") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": {
                        "as_of": "2026-11-28T10:00:00Z",
                        "payload": results_payload,
                    }
                },
            )
        if path.endswith("/complete/") and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": detail["id"],
                    "name": detail["name"],
                    "status": "completed",
                    "rollout_decision": (body or {}).get("rollout_decision"),
                    "rollout_variant": (body or {}).get("rollout_variant"),
                    "ended_at": "2026-11-28T10:05:00Z",
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


def test_rest_client_reads_experiment_exposures_and_results_by_name() -> None:
    calls: list[tuple[str, str, dict | None]] = []
    transport = _mock_transport(
        detail=_sample_detail(),
        exposures_points=[
            {"new_identities": {"control": 60, "verdict": 55}},
            {"new_identities": {"control": 50, "verdict": 55}},
        ],
        results_payload={
            "srm_p_value": 0.99,
            "metrics": [
                {
                    "metric_id": 101,
                    "variants": {
                        "control": {"n": 110, "sum": 66.0, "sum_squares": 66.0},
                        "verdict": {"n": 110, "sum": 33.0, "sum_squares": 33.0},
                    },
                    "inference": {
                        "verdict": {
                            "lift": -0.5,
                            "ci_low": -0.65,
                            "ci_high": -0.30,
                            "chance_to_win": 0.999,
                        }
                    },
                },
                {
                    "metric_id": 102,
                    "variants": {
                        "control": {"n": 110, "sum": 8.0, "sum_squares": 8.0},
                        "verdict": {"n": 110, "sum": 9.0, "sum_squares": 9.0},
                    },
                    "inference": {
                        "verdict": {
                            "lift": 0.125,
                            "ci_low": -0.40,
                            "ci_high": 0.85,
                            "chance_to_win": 0.35,
                        }
                    },
                },
            ],
        },
        refresh_status=429,
        recorded_calls=calls,
    )
    http_client = httpx.Client(transport=transport, headers={"Authorization": "Api-Key test"})
    client = RestClient(
        "http://127.0.0.1:8000",
        "org.secret",
        "sim-env-key",
        simulated=True,
        refresh=True,
        refresh_wait_seconds=0.0,
        http_client=http_client,
    )

    exp = client.get_experiment("result_view_sim")
    assert exp.id == "7"
    assert exp.feature_name == "result_view"
    assert exp.simulated is True

    exposure = client.get_exposures("result_view_sim")
    assert exposure.counts == {"control": 110, "verdict": 110}
    assert exposure.planned_weights == {"control": 0.5, "verdict": 0.5}

    results = client.get_results("result_view_sim")
    assert results.srm_p_value == 0.99
    assert len(results.metrics) == 2
    primary, guardrail = results.metrics
    assert primary.name == "shipped_ambiguous"
    assert primary.role == "primary"
    assert primary.expected_direction == "decrease"
    assert primary.interval and primary.interval.high == -0.30
    assert guardrail.name == "held_clear_win"
    assert guardrail.role == "guardrail"
    assert guardrail.expected_direction == "not_increase"

    # Propose & apply variant rollout
    change = client.propose_rollout("7", "verdict")
    assert change.payload == {"rollout_decision": "variant", "rollout_variant": 10}
    applied = client.apply(change)
    assert applied["status"] == "completed"
    assert applied["rollout_variant"] == 10

    with pytest.raises(ValueError):
        client.propose_rollout("7", "nonexistent")


def test_cli_live_check_uses_rest_client_and_logs_effect_look(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    transport = _mock_transport(
        detail=_sample_detail(),
        exposures_points=[{"new_identities": {"control": 10, "verdict": 10}}],
        results_payload={"srm_p_value": None, "metrics": []},
    )
    monkeypatch.setenv("FLAGSMITH_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("FLAGSMITH_API_KEY", "org.key")
    monkeypatch.setenv("FLAGSMITH_SIM_ENV_KEY", "sim-key")

    orig_init = RestClient.__init__

    def patched_init(self, base_url, api_key, env_key, **kwargs):
        kwargs["http_client"] = httpx.Client(transport=transport)
        kwargs["refresh_wait_seconds"] = 0.0
        orig_init(self, base_url, api_key, env_key, **kwargs)

    monkeypatch.setattr(RestClient, "__init__", patched_init)

    looks_path = tmp_path / "looks.jsonl"
    rc = main(
        [
            "check",
            "7",
            "--live",
            "--env",
            "simulation",
            "--looks-path",
            str(looks_path),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "HOLD"
    assert payload["experiment"]["simulated"] is True
    assert len(looks_path.read_text(encoding="utf-8").splitlines()) == 1
