"""Offline CLI tests for `verdict check`, `verdict log`, and `verdict plan`."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.cli import main


def test_cli_check_on_all_fixtures_offline(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("VERDICT_LLM", "off")
    looks_file = tmp_path / "looks.jsonl"

    expected = {
        "ship": "VERDICT: SHIP",
        "hold": "VERDICT: HOLD",
        "hold_insufficient_data": "VERDICT: HOLD",
        "reject_srm": "VERDICT: REJECT",
        "reject_guardrail": "VERDICT: REJECT",
        "peeking": "VERDICT: HOLD",
    }

    for exp_id, expected_line in expected.items():
        rc = main(
            [
                "check",
                exp_id,
                "--fixture",
                "--fixtures-dir",
                "data/fixtures",
                "--looks-path",
                str(looks_file),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert expected_line in out

    lines = looks_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(expected)
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["experiment_id"] == "ship"


def test_cli_check_json_and_peeking_log(tmp_path: Path, capsys) -> None:
    looks_file = tmp_path / "looks.jsonl"

    assert (
        main(
            [
                "check",
                "peeking",
                "--fixture",
                "--fixtures-dir",
                "data/fixtures",
                "--looks-path",
                str(looks_file),
                "--json",
            ]
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert first["verdict"] == "HOLD"

    # Second run on same incomplete fixture must trigger peeking WARN
    assert (
        main(
            [
                "check",
                "peeking",
                "--fixture",
                "--fixtures-dir",
                "data/fixtures",
                "--looks-path",
                str(looks_file),
                "--json",
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    looks_chk = next(c for c in second["checks"] if c["name"] == "looks")
    assert looks_chk["status"] == "WARN"


def test_cli_plan_and_log(tmp_path: Path, capsys) -> None:
    looks_file = tmp_path / "looks.jsonl"
    main(
        [
            "check",
            "ship",
            "--fixture",
            "--fixtures-dir",
            "data/fixtures",
            "--looks-path",
            str(looks_file),
        ]
    )
    capsys.readouterr()

    rc_log = main(["log", "--looks-path", str(looks_file)])
    assert rc_log == 0
    assert "ship" in capsys.readouterr().out

    rc_plan = main(["plan", "--baseline", "0.60", "--mde", "0.20", "--daily-visitors", "50"])
    assert rc_plan == 0
    assert "per arm" in capsys.readouterr().out.lower()
