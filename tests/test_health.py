"""Tests for integrity-only scripts/health.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from quiz.db import QuizDatabase
from scripts.health import append_look_log, build_health_report, compute_chi2_srm_p_value


def test_srm_chi2_balanced_and_skewed() -> None:
    p_even = compute_chi2_srm_p_value(50, 50)
    assert p_even is not None and p_even > 0.99
    p_skewed = compute_chi2_srm_p_value(90, 10)
    assert p_skewed is not None and p_skewed < 0.001


def test_health_report_excludes_outcomes_and_logs(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "quiz.db"
    db = QuizDatabase(db_path)
    db.record_exposure_once("u1", "dashboard", simulated=False)
    db.record_choice_once("u1", "s1", "ship")
    db.record_choice_once("u1", "s2", "hold")
    db.mark_completed("u1")
    db.record_exposure_once("u2", "verdict", simulated=False)

    class FakeResp:
        def __init__(self, payload: dict, status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url: str) -> FakeResp:
            if url.endswith("/exposures/"):
                return FakeResp(
                    {
                        "exposures": {
                            "as_of": "2026-11-26T04:00:00Z",
                            "payload": {
                                "timeseries": {
                                    "points": [{"new_identities": {"control": 1, "verdict": 1}}]
                                },
                                "excluded_identities": 0,
                            },
                        }
                    }
                )
            if url.endswith("/results/"):
                return FakeResp(
                    {
                        "results": {
                            "payload": {
                                "metrics": [{"variants": {"control": {"sum": 999}}}],
                                "srm_p_value": 1.0,
                            }
                        }
                    }
                )
            return FakeResp({"name": "result_view_experiment", "status": "running"})

    monkeypatch.setattr("scripts.health.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "scripts.health.httpx.get",
        lambda *a, **kw: FakeResp({"status": "ok"}, status_code=200),
    )

    report = build_health_report(
        env_name="production",
        flagsmith_url="http://127.0.0.1:8000",
        env_key="env-key",
        api_key="api-key",
        exp_id="3",
        db_path=db_path,
        quiz_url="http://127.0.0.1:3000",
    )
    serialized = json.dumps(report)
    assert "shipped_ambiguous" not in serialized
    assert "held_clear_win" not in serialized
    assert "999" not in serialized
    assert "credible_interval" not in serialized
    assert report["kind"] == "integrity"
    assert report["integrity_status"] == "OK"
    assert report["srm_p_value_flagsmith"] == 1.0

    looks_path = tmp_path / "looks.jsonl"
    append_look_log(report, looks_path=looks_path)
    lines = looks_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "integrity"
