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
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cohorts: combined N=" in out
    assert "Verdicts: combined=HOLD | real-only=HOLD" in out
    assert "DISAGREEMENT" not in out
