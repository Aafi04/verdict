"""Tests for `verdict apply` dry-run, refusal, typed approval, drift check, and audit log."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from tests.test_rest_client import _mock_transport, _sample_detail
from verdict.cli import main
from verdict.client.rest import RestClient


def _ship_results_payload(*, control_n: int = 110, variant_n: int = 110) -> dict:
    return {
        "srm_p_value": 0.99,
        "metrics": [
            {
                "metric_id": 101,
                "variants": {
                    "control": {"n": control_n, "sum": 66.0, "sum_squares": 66.0},
                    "verdict": {"n": variant_n, "sum": 33.0, "sum_squares": 33.0},
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
                    "control": {"n": control_n, "sum": 8.0, "sum_squares": 8.0},
                    "verdict": {"n": variant_n, "sum": 9.0, "sum_squares": 9.0},
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
    }


def _patch_rest_client(
    monkeypatch,
    *,
    exposures_points: list[dict],
    results_payload: dict,
    recorded_calls: list[tuple[str, str, dict | None]],
    drift_on_second_read: bool = False,
) -> None:
    monkeypatch.setenv("FLAGSMITH_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("FLAGSMITH_API_KEY", "org.key")
    monkeypatch.setenv("FLAGSMITH_SIM_ENV_KEY", "sim-key")

    detail = _sample_detail(exp_id=7, name="result_view_sim")
    read_counter = {"exposures": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if drift_on_second_read and path.endswith("/exposures/") and request.method == "GET":
            read_counter["exposures"] += 1
            pts = (
                [{"new_identities": {"control": 115, "verdict": 110}}]
                if read_counter["exposures"] >= 3
                else exposures_points
            )
            recorded_calls.append((request.method, path, None))
            return httpx.Response(
                200,
                json={
                    "exposures": {
                        "as_of": "2026-11-28T10:00:00Z",
                        "payload": {
                            "timeseries": {"points": pts, "granularity": "hour"},
                            "excluded_identities": 0,
                        },
                    }
                },
            )
        return _mock_transport(
            detail=detail,
            exposures_points=exposures_points,
            results_payload=results_payload,
            recorded_calls=recorded_calls,
        ).handle_request(request)

    orig_init = RestClient.__init__

    def patched_init(self, base_url, api_key, env_key, **kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(handler))
        kwargs["refresh_wait_seconds"] = 0.0
        orig_init(self, base_url, api_key, env_key, **kwargs)

    monkeypatch.setattr(RestClient, "__init__", patched_init)


def test_apply_refuses_non_ship_fixtures(tmp_path: Path, capsys) -> None:
    audit_path = tmp_path / "audit.jsonl"
    looks_path = tmp_path / "looks.jsonl"

    for fixture in ("hold", "reject_srm", "reject_guardrail"):
        rc = main(
            [
                "apply",
                fixture,
                "--fixture",
                "--fixtures-dir",
                "data/fixtures",
                "--looks-path",
                str(looks_path),
                "--audit-path",
                str(audit_path),
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "REFUSED" in err

    assert not audit_path.exists()


def test_apply_ship_fixture_dry_run_default(tmp_path: Path, capsys) -> None:
    audit_path = tmp_path / "audit.jsonl"
    rc = main(
        [
            "apply",
            "ship",
            "--fixture",
            "--fixtures-dir",
            "data/fixtures",
            "--looks-path",
            str(tmp_path / "looks.jsonl"),
            "--audit-path",
            str(audit_path),
            "--plain",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "VERDICT: SHIP" in out
    assert "DRY-RUN ONLY" in out
    assert "APPROVE ship " in out
    assert not audit_path.exists()


def test_apply_execute_rejects_wrong_approval_without_writing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    _patch_rest_client(
        monkeypatch,
        exposures_points=[{"new_identities": {"control": 110, "verdict": 110}}],
        results_payload=_ship_results_payload(),
        recorded_calls=calls,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "APPROVE 7 wronghash")
    audit_path = tmp_path / "audit.jsonl"

    rc = main(
        [
            "apply",
            "7",
            "--live",
            "--env",
            "simulation",
            "--execute",
            "--looks-path",
            str(tmp_path / "looks.jsonl"),
            "--audit-path",
            str(audit_path),
        ]
    )
    assert rc == 1
    assert not any(path.endswith("/complete/") for _, path, _ in calls)
    assert not audit_path.exists()


def test_apply_execute_aborts_on_state_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    _patch_rest_client(
        monkeypatch,
        exposures_points=[{"new_identities": {"control": 110, "verdict": 110}}],
        results_payload=_ship_results_payload(),
        recorded_calls=calls,
        drift_on_second_read=True,
    )
    audit_path = tmp_path / "audit.jsonl"

    # Obtain the initial hash via --json check first
    assert (
        main(
            [
                "check",
                "7",
                "--live",
                "--env",
                "simulation",
                "--looks-path",
                str(tmp_path / "looks.jsonl"),
                "--json",
            ]
        )
        == 0
    )
    verdict_hash = json.loads(capsys.readouterr().out)["verdict_hash"]
    monkeypatch.setattr("builtins.input", lambda _prompt: f"APPROVE 7 {verdict_hash[:8]}")

    rc = main(
        [
            "apply",
            "7",
            "--live",
            "--env",
            "simulation",
            "--execute",
            "--looks-path",
            str(tmp_path / "looks.jsonl"),
            "--audit-path",
            str(audit_path),
        ]
    )
    assert rc == 1
    assert "state changed" in capsys.readouterr().err
    assert not any(path.endswith("/complete/") for _, path, _ in calls)
    assert not audit_path.exists()


def test_apply_execute_succeeds_and_writes_audit_log(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    _patch_rest_client(
        monkeypatch,
        exposures_points=[{"new_identities": {"control": 110, "verdict": 110}}],
        results_payload=_ship_results_payload(),
        recorded_calls=calls,
    )
    looks_path = tmp_path / "looks.jsonl"
    audit_path = tmp_path / "audit.jsonl"

    assert (
        main(
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
        == 0
    )
    verdict_hash = json.loads(capsys.readouterr().out)["verdict_hash"]
    monkeypatch.setattr("builtins.input", lambda _prompt: f"APPROVE 7 {verdict_hash[:8]}")

    rc = main(
        [
            "apply",
            "7",
            "--live",
            "--env",
            "simulation",
            "--execute",
            "--looks-path",
            str(looks_path),
            "--audit-path",
            str(audit_path),
            "--plain",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "APPLIED:" in out
    assert any(method == "POST" and path.endswith("/complete/") for method, path, _ in calls)

    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    assert entries[0]["id"] == "7"
    assert entries[0]["verdict_hash"] == verdict_hash
    # Under pytest (non-TTY stdin), approver must be recorded as "non-interactive"
    assert entries[0]["approver"] == "non-interactive"
    assert entries[0]["change"]["payload"] == {"rollout_decision": "variant", "rollout_variant": 10}


def test_apply_execute_records_human_typed_only_on_tty(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    _patch_rest_client(
        monkeypatch,
        exposures_points=[{"new_identities": {"control": 110, "verdict": 110}}],
        results_payload=_ship_results_payload(),
        recorded_calls=calls,
    )
    looks_path = tmp_path / "looks.jsonl"
    audit_path = tmp_path / "audit.jsonl"

    assert (
        main(
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
        == 0
    )
    verdict_hash = json.loads(capsys.readouterr().out)["verdict_hash"]
    monkeypatch.setattr("builtins.input", lambda _prompt: f"APPROVE 7 {verdict_hash[:8]}")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    rc = main(
        [
            "apply",
            "7",
            "--live",
            "--env",
            "simulation",
            "--execute",
            "--looks-path",
            str(looks_path),
            "--audit-path",
            str(audit_path),
            "--plain",
        ]
    )
    assert rc == 0
    entry = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["approver"] == "human-typed"


def test_rollback_dry_run_and_execute(tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setenv("FLAGSMITH_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("FLAGSMITH_API_KEY", "org.key")
    monkeypatch.setenv("FLAGSMITH_SIM_ENV_KEY", "sim-key")

    detail = _sample_detail(exp_id=7, name="result_view_sim")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, path, body))
        if path.endswith("/experiments/7/") and request.method == "GET":
            return httpx.Response(200, json=detail)
        if path.endswith("/featurestates/") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 99,
                            "feature": 1,
                            "environment": 3,
                            "feature_segment": None,
                            "enabled": True,
                            "feature_state_value": "dashboard",
                            "multivariate_feature_state_values": [
                                {
                                    "id": 50,
                                    "multivariate_feature_option": 10,
                                    "percentage_allocation": 100.0,
                                }
                            ],
                        }
                    ]
                },
            )
        if path.endswith("/featurestates/99/") and request.method == "PUT":
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "feature": 1,
                    "enabled": True,
                    "feature_state_value": "dashboard",
                    "multivariate_feature_state_values": (body or {}).get(
                        "multivariate_feature_state_values"
                    ),
                },
            )
        return httpx.Response(404, json={})

    orig_init = RestClient.__init__

    def patched_init(self, base_url, api_key, env_key, **kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(handler))
        orig_init(self, base_url, api_key, env_key, **kwargs)

    monkeypatch.setattr(RestClient, "__init__", patched_init)
    audit_path = tmp_path / "audit.jsonl"

    # Dry-run makes no PUT writes
    rc_dry = main(["rollback", "7", "--env", "simulation", "--audit-path", str(audit_path), "--plain"])
    assert rc_dry == 0
    assert "DRY-RUN ONLY" in capsys.readouterr().out
    assert not any(m == "PUT" for m, _, _ in calls)

    # Execute with typed approval runs PUT and writes audit log
    monkeypatch.setattr("builtins.input", lambda _p: "ROLLBACK 7 result_view")
    rc_exec = main(
        [
            "rollback",
            "7",
            "--env",
            "simulation",
            "--execute",
            "--audit-path",
            str(audit_path),
            "--plain",
        ]
    )
    assert rc_exec == 0
    assert "ROLLED BACK:" in capsys.readouterr().out
    assert any(m == "PUT" and path.endswith("/featurestates/99/") for m, path, _ in calls)
    rb_entry = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert rb_entry["action"] == "rollback"
    assert rb_entry["approver"] == "non-interactive"
    assert rb_entry["result"]["multivariate_feature_state_values"][0]["percentage_allocation"] == 0.0
