"""Unit tests for quiz/simulate.py generative model, --dry-run determinism, and auth."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quiz.app import create_app
from quiz.config import DEFAULT_SCENARIOS_DIR, QuizSettings
from quiz.db import QuizDatabase
from quiz.simulate import (
    P_COMPLETE_POST_EXPOSURE,
    P_HOLD_S2_DASHBOARD,
    P_HOLD_S2_VERDICT,
    P_SHIP_S1_DASHBOARD,
    P_SHIP_S1_VERDICT,
    build_dry_run_plan,
    default_synthetic_count,
    main,
    run_simulation,
)
from tests.test_quiz import StubSdk


def test_fixed_generative_parameters_match_spec() -> None:
    assert P_SHIP_S1_DASHBOARD == 0.60
    assert P_SHIP_S1_VERDICT == 0.42
    assert P_HOLD_S2_DASHBOARD == 0.08
    assert P_HOLD_S2_VERDICT == 0.09
    assert P_COMPLETE_POST_EXPOSURE == 0.88


def test_dry_run_identical_output_for_same_seed_and_no_http(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("HTTP client must not be constructed in --dry-run")

    monkeypatch.setattr("quiz.simulate.httpx.Client", fail_if_called)

    db_path = tmp_path / "quiz.db"
    db = QuizDatabase(db_path)
    for i in range(5):
        db.record_exposure_once(f"real-{i}", "dashboard", simulated=False, synthetic=False)
    for i in range(10):
        db.record_exposure_once(f"sim-{i}", "verdict", simulated=True, synthetic=True)

    assert main(["--dry-run", "--db-path", str(db_path), "--seed", "20261126"]) == 0
    out1 = capsys.readouterr().out
    assert main(["--dry-run", "--db-path", str(db_path), "--seed", "20261126"]) == 0
    out2 = capsys.readouterr().out

    assert out1 == out2
    parsed = json.loads(out1)
    assert parsed["dry_run"] is True
    assert "DRY-RUN" in parsed["label"]
    assert parsed["simulated"] is True
    assert parsed["count"] == 195  # max(0, 200 - 5 real exposures), ignores 10 synthetic
    assert len(parsed["visitors"]) == 195


def test_default_synthetic_count_clamps_at_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "quiz.db"
    db = QuizDatabase(db_path)
    for i in range(210):
        db.record_exposure_once(f"real-{i}", "verdict", simulated=False, synthetic=False)
    assert default_synthetic_count(db_path) == 0


def test_simulation_runs_through_production_quiz_with_token_and_dropout(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="verdict")
    settings = QuizSettings(
        flagsmith_url="http://127.0.0.1:8000",
        flagsmith_env_key="prod-key",
        cookie_secret="unit-test-cookie-secret",
        db_path=tmp_path / "quiz.db",
        scenarios_dir=DEFAULT_SCENARIOS_DIR,
        simulated_mode=False,
        rate_limit_per_min=2,
        synthetic_token="synth-token-xyz",
    )
    app = create_app(settings=settings, sdk=sdk)

    summary = run_simulation(
        "http://testserver",
        count=25,
        seed=20261126,
        synthetic_token="synth-token-xyz",
        client_factory=lambda **kw: TestClient(app, headers=kw.get("headers", {})),
    )
    assert summary["count"] == 25
    assert summary["arms"]["verdict"] == 25
    assert summary["refresh_checked"] is True
    assert summary["double_submit_checked"] is True
    # 12% post-exposure drop-off produces partial completions (< 25)
    assert 1 <= summary["completions"]["verdict"] < 25

    db = QuizDatabase(settings.db_path)
    assert db.summarize(cohort="synthetic")["totals"]["exposures"] == 25
    assert db.summarize(cohort="real")["totals"]["exposures"] == 0


def test_simulation_batch_size_runs_integrity_checks_and_stops_on_mismatch(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="verdict")
    settings = QuizSettings(
        flagsmith_url="http://127.0.0.1:8000",
        flagsmith_env_key="prod-key",
        cookie_secret="unit-test-cookie-secret",
        db_path=tmp_path / "quiz.db",
        scenarios_dir=DEFAULT_SCENARIOS_DIR,
        simulated_mode=False,
        rate_limit_per_min=2,
        synthetic_token="synth-token-xyz",
    )
    app = create_app(settings=settings, sdk=sdk)

    seen_batches: list[int] = []

    def ok_checker(sent: int) -> dict:
        seen_batches.append(sent)
        return {"integrity_status": "OK", "count_diff": {"total": 0}}

    summary = run_simulation(
        "http://testserver",
        count=12,
        seed=20261126,
        synthetic_token="synth-token-xyz",
        batch_size=5,
        batch_settle_seconds=0.0,
        batch_checker=ok_checker,
        client_factory=lambda **kw: TestClient(app, headers=kw.get("headers", {})),
    )
    assert seen_batches == [5, 10, 12]
    assert len(summary["batch_checks"]) == 3

    # Also verify _run_health_cmd stops immediately if count_diff != 0 or integrity != OK.
    import sys

    bad_cmd = (
        f'"{sys.executable}" -c '
        '"import json; print(json.dumps({\'integrity_status\': \'WARN_COUNT_DIFF\', \'count_diff\': {\'total\': 1}}))"'
    )
    with pytest.raises(RuntimeError, match="Batch integrity check failed"):
        run_simulation(
            "http://testserver",
            count=10,
            seed=20261126,
            synthetic_token="synth-token-xyz",
            batch_size=5,
            batch_settle_seconds=0.0,
            health_cmd=bad_cmd,
            client_factory=lambda **kw: TestClient(app, headers=kw.get("headers", {})),
        )
    # Stopped after the very first batch of 5 (did not continue to 10)
    db = QuizDatabase(settings.db_path)
    assert db.summarize(cohort="synthetic")["totals"]["exposures"] == 12 + 5


def test_dry_run_offset_reproduces_items_50_to_197() -> None:
    full = build_dry_run_plan(count=198, seed=20261126, offset=0)
    tail = build_dry_run_plan(count=148, seed=20261126, offset=50)
    assert tail["offset"] == 50
    assert tail["count"] == 148
    assert tail["visitors"] == full["visitors"][50:198]


def test_batch_check_retries_once_on_stale_snapshot_then_proceeds_or_aborts(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = StubSdk(default_arm="verdict")
    settings = QuizSettings(
        flagsmith_url="http://127.0.0.1:8000",
        flagsmith_env_key="prod-key",
        cookie_secret="unit-test-cookie-secret",
        db_path=tmp_path / "quiz.db",
        scenarios_dir=DEFAULT_SCENARIOS_DIR,
        simulated_mode=False,
        rate_limit_per_min=2,
        synthetic_token="synth-token-xyz",
    )
    app = create_app(settings=settings, sdk=sdk)

    sleeps: list[float] = []
    monkeypatch.setattr("quiz.simulate.time.sleep", lambda s: sleeps.append(s))

    # Case 1: STALE_SNAPSHOT once -> waits 305s -> retry returns OK -> proceeds cleanly.
    calls_1 = {"n": 0}

    def stale_then_ok(_sent: int) -> dict:
        calls_1["n"] += 1
        if calls_1["n"] == 1:
            return {
                "integrity_status": "STALE_SNAPSHOT",
                "flagsmith_snapshot_stale": True,
                "count_diff": None,
            }
        return {
            "integrity_status": "OK",
            "flagsmith_snapshot_stale": False,
            "count_diff": {"total": 0},
        }

    summary = run_simulation(
        "http://testserver",
        count=5,
        seed=20261126,
        synthetic_token="synth-token-xyz",
        batch_size=5,
        batch_settle_seconds=0.0,
        cooldown_wait_seconds=305.0,
        batch_checker=stale_then_ok,
        client_factory=lambda **kw: TestClient(app, headers=kw.get("headers", {})),
    )
    assert calls_1["n"] == 2
    assert 305.0 in sleeps
    assert summary["batch_checks"][0]["integrity_status"] == "OK"

    # Case 2: STALE_SNAPSHOT twice -> aborts without continuing.
    with pytest.raises(RuntimeError, match="STALE_SNAPSHOT"):
        run_simulation(
            "http://testserver",
            count=10,
            seed=20261126,
            synthetic_token="synth-token-xyz",
            batch_size=5,
            batch_settle_seconds=0.0,
            cooldown_wait_seconds=305.0,
            batch_checker=lambda _s: {
                "integrity_status": "STALE_SNAPSHOT",
                "flagsmith_snapshot_stale": True,
                "count_diff": None,
            },
            client_factory=lambda **kw: TestClient(app, headers=kw.get("headers", {})),
        )
