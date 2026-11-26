"""Tests for static Verdict HTML results template and renderer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.render_results import render_results_html


def test_render_results_html_for_fixtures_and_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        json.dumps(
            {
                "ts": "2026-11-29T04:00:00Z",
                "id": "ship",
                "verdict_hash": "abcdef1234567890",
                "approver": "human-typed",
                "change": {"description": "Promote verdict to 100%"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    for fixture_name, badge, should_have_audit in (
        ("ship", "VERDICT: SHIP", True),
        ("hold", "VERDICT: HOLD", False),
        ("reject_srm", "VERDICT: REJECT", False),
    ):
        html = render_results_html(
            experiment_arg=fixture_name,
            use_fixture=True,
            looks_path=tmp_path / "looks.jsonl",
            audit_path=audit_path,
        )
        assert badge in html
        assert "SIMULATED DATA" in html
        assert "Autonomy Boundary" in html
        if should_have_audit:
            assert "Promote verdict to 100%" in html
        else:
            assert "No flag mutations recorded" in html


def test_render_results_escapes_untrusted_text_and_guards_pre_freeze(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    xss_fixture = json.loads(Path("data/fixtures/hold.json").read_text(encoding="utf-8"))
    xss_fixture["experiment"]["id"] = "xss"
    xss_fixture["experiment"]["hypothesis"] = "<script>alert('xss')</script>"
    (fixtures_dir / "xss.json").write_text(json.dumps(xss_fixture), encoding="utf-8")

    html = render_results_html(
        experiment_arg="xss",
        use_fixture=True,
        fixtures_dir=fixtures_dir,
        looks_path=tmp_path / "looks.jsonl",
        audit_path=tmp_path / "audit.jsonl",
    )
    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;alert" in html

    with pytest.raises(PermissionError, match="Pre-freeze"):
        render_results_html(
            experiment_arg="2",
            force_live=True,
            env_name="production",
            now=datetime(2026, 11, 26, 10, 0, tzinfo=UTC),
        )
