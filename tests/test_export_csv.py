"""Unit tests for anonymised CSV export in scripts/export_csv.py."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quiz.db import QuizDatabase
from scripts.export_csv import FREEZE_UTC, export_csvs


def test_export_csv_strips_vids_and_separates_cohorts(tmp_path: Path) -> None:
    db_path = tmp_path / "quiz.db"
    db = QuizDatabase(db_path)
    db.record_exposure_once("real-uuid-secret-1", "dashboard", simulated=False)
    db.record_choice_once("real-uuid-secret-1", "s1", "ship")
    db.record_choice_once("real-uuid-secret-1", "s2", "hold")
    db.mark_completed("real-uuid-secret-1")

    db.record_exposure_once("sim-uuid-secret-2", "verdict", simulated=True)
    db.record_choice_once("sim-uuid-secret-2", "s1", "hold")
    db.record_choice_once("sim-uuid-secret-2", "s2", "ship")
    db.mark_completed("sim-uuid-secret-2")

    looks_path = tmp_path / "looks.jsonl"
    looks_path.write_text(
        json.dumps(
            {
                "ts": "2026-11-26T08:41:00Z",
                "kind": "integrity",
                "source": "scripts/health.py",
                "experiment_id": "2",
                "env": "production",
                "sqlite_exposures": {"total": 0},
                "integrity_status": "OK",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "export"
    written = export_csvs(
        db_path=db_path,
        looks_path=looks_path,
        out_dir=out_dir,
        mode="all",
        now=FREEZE_UTC,
    )

    visitors_text = written["visitors"].read_text(encoding="utf-8")
    assert "real-uuid-secret-1" not in visitors_text
    assert "sim-uuid-secret-2" not in visitors_text

    with written["visitors"].open(encoding="utf-8") as fh:
        v_rows = list(csv.DictReader(fh))
    assert len(v_rows) == 2
    assert {len(r["anon_id"]) for r in v_rows} == {16}
    assert all(r["exposed_hour_utc"].endswith(":00Z") for r in v_rows)

    with written["summary"].open(encoding="utf-8") as fh:
        s_rows = list(csv.DictReader(fh))
    assert len(s_rows) == 2
    by_ds = {r["dataset"]: r for r in s_rows}
    assert by_ds["production"]["shipped_ambiguous_rate"] == "1.0000"
    assert by_ds["production"]["held_clear_win_rate"] == "1.0000"
    assert by_ds["simulation"]["shipped_ambiguous_rate"] == "0.0000"

    with written["looks"].open(encoding="utf-8") as fh:
        l_rows = list(csv.DictReader(fh))
    assert len(l_rows) == 1
    assert l_rows[0]["integrity_status"] == "OK"


def test_export_csv_blocks_production_before_freeze(tmp_path: Path) -> None:
    db_path = tmp_path / "quiz.db"
    QuizDatabase(db_path)
    before = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)

    with pytest.raises(PermissionError, match="Pre-freeze"):
        export_csvs(
            db_path=db_path,
            looks_path=tmp_path / "looks.jsonl",
            out_dir=tmp_path / "export",
            mode="production",
            now=before,
        )

    # Simulation mode is allowed pre-freeze
    written = export_csvs(
        db_path=db_path,
        looks_path=tmp_path / "looks.jsonl",
        out_dir=tmp_path / "export",
        mode="simulation",
        now=before,
    )
    assert written["visitors"].exists()
