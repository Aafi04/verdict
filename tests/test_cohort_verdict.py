"""Tests for combined vs real-only verdict breakdown and disagreement reporting."""

from __future__ import annotations

import json
from pathlib import Path

from quiz.db import QuizDatabase
from scripts.render_results import render_results_html
from verdict.cli import main


def test_cli_shows_combined_real_synthetic_and_disagreement(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "quiz.db"
    db = QuizDatabase(db_path)
    # 6 real visitors (insufficient -> real-only verdict = HOLD)
    for i in range(6):
        arm = "dashboard" if i % 2 == 0 else "verdict"
        db.record_exposure_once(f"real-{i}", arm, simulated=False, synthetic=False)
        db.record_choice_once(f"real-{i}", "s1", "ship" if arm == "dashboard" else "hold")
    # 220 synthetic visitors matching ship.json combined counts
    for i in range(220):
        arm = "dashboard" if i < 112 else "verdict"
        db.record_exposure_once(f"sim-{i}", arm, simulated=True, synthetic=True)

    rc = main(
        [
            "check",
            "ship",
            "--fixture",
            "--fixtures-dir",
            "data/fixtures",
            "--db-path",
            str(db_path),
            "--looks-path",
            str(tmp_path / "looks.jsonl"),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["combined_verdict"] == "SHIP"
    assert payload["real_only"]["verdict"] == "HOLD"
    assert payload["sample_breakdown"] == {
        "combined_n": 226,
        "real_n": 6,
        "synthetic_n": 220,
    }
    assert payload["verdict_disagreement"] is True
    assert "DISAGREEMENT" in payload["disagreement_detail"]

    # Also verify HTML report displays combined N, real N, synthetic N, both verdicts, and disagreement.
    html = render_results_html(
        experiment_arg="ship",
        use_fixture=True,
        db_path=db_path,
        looks_path=tmp_path / "looks.jsonl",
        audit_path=tmp_path / "audit.jsonl",
    )
    assert "COMBINED VERDICT: SHIP" in html
    assert "REAL-ONLY VERDICT: HOLD" in html
    assert "VERDICT DISAGREEMENT" in html
    assert "Synthetic data:" in html
    assert "not evidence about human behaviour" in html
    assert "226" in html
    assert "220" in html


def test_cli_text_memo_no_disagreement_when_real_and_combined_both_hold(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "quiz.db"
    db = QuizDatabase(db_path)
    for i in range(4):
        arm = "dashboard" if i % 2 == 0 else "verdict"
        db.record_exposure_once(f"real-{i}", arm, simulated=False, synthetic=False)

    rc = main(
        [
            "check",
            "hold",
            "--fixture",
            "--fixtures-dir",
            "data/fixtures",
            "--db-path",
            str(db_path),
            "--looks-path",
            str(tmp_path / "looks.jsonl"),
            "--plain",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Synthetic data: primarily synthetic" in out
    assert "Cohorts: combined N=" in out
    assert "Verdicts: combined=HOLD | real-only=HOLD" in out
    assert "DISAGREEMENT" not in out


def test_real_only_n2_one_arm_returns_hold_while_seeded_fault_returns_reject(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "quiz.db"
    db = QuizDatabase(db_path)
    # Real N=2, both in dashboard arm (0 in verdict arm) -> HOLD, not REJECT
    db.record_exposure_once("real-1", "dashboard", simulated=False, synthetic=False)
    db.record_exposure_once("real-2", "dashboard", simulated=False, synthetic=False)
    # 198 balanced synthetic visitors matching ship.json / Experiment 2 combined N
    for i in range(198):
        arm = "dashboard" if i < 98 else "verdict"
        db.record_exposure_once(f"sim-{i}", arm, simulated=True, synthetic=True)

    rc = main(
        [
            "check",
            "ship",
            "--fixture",
            "--fixtures-dir",
            "data/fixtures",
            "--db-path",
            str(db_path),
            "--looks-path",
            str(tmp_path / "looks.jsonl"),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["combined_verdict"] == "SHIP"
    assert payload["real_only"]["verdict"] == "HOLD"
    assert "below pre-registered minimum (50 per arm)" in payload["real_only"]["reasons"][0]

    # Seeded SRM fault (adequate N, broken split) must still return REJECT
    rc_fault = main(
        [
            "check",
            "reject_srm",
            "--fixture",
            "--fixtures-dir",
            "data/fixtures",
            "--db-path",
            str(tmp_path / "empty.db"),
            "--looks-path",
            str(tmp_path / "looks.jsonl"),
            "--json",
        ]
    )
    assert rc_fault == 0
    fault_payload = json.loads(capsys.readouterr().out)
    assert fault_payload["combined_verdict"] == "REJECT"


def test_freeze_cutoff_excludes_post_freeze_rows_in_experiment_2(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "quiz.db"
    QuizDatabase(db_path)
    conn = sqlite3.connect(db_path)
    # 2 real rows within [EXPERIMENT_STARTED_AT_UTC, FREEZE_UTC_ISO]
    conn.execute(
        """
        INSERT INTO visitors (vid, arm, exposed_at, simulated, synthetic)
        VALUES
          ('real-pre-1', 'dashboard', '2026-11-26T09:00:00Z', 0, 0),
          ('real-pre-2', 'dashboard', '2026-11-26T10:00:00Z', 0, 0),
          ('real-post-1', 'verdict', '2026-11-26T12:00:00Z', 0, 0)
        """
    )
    conn.commit()
    conn.close()

    from verdict.cohort import build_cohort_comparison
    from verdict.models import Experiment, Exposure, VerdictResult

    exp2 = Experiment(
        id="2",
        name="result_view_experiment_v2",
        feature_name="result_view",
        planned_sample=200,
        simulated=True,
    )
    combined_exp = Exposure(
        planned_weights={"dashboard": 0.5, "verdict": 0.5},
        counts={"dashboard": 100, "verdict": 100},
    )
    combined_verdict = VerdictResult(
        verdict="REJECT",
        verdict_hash="abc",
        checks=[],
        reasons=["guardrail"],
        next_action="hold",
    )
    comp = build_cohort_comparison(
        exp2, combined_exp, combined_verdict, db_path=db_path
    )
    assert comp.real_n == 2
    assert comp.real_only_verdict.verdict == "HOLD"
