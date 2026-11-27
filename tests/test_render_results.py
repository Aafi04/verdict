"""Tests for static Verdict HTML results template and renderer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.render_results import render_results_html
from verdict.checks import run_all_checks
from verdict.client.fixture import FixtureClient
from verdict.policy import evaluate_verdict


def _fixture_verdict_hash(fixture_name: str) -> str:
    client = FixtureClient(Path("data/fixtures"))
    exp = client.get_experiment(fixture_name)
    exposure = client.get_exposures(fixture_name)
    results = client.get_results(fixture_name)
    checks = run_all_checks(exp, exposure, results)
    return evaluate_verdict(exp, exposure, results, checks).verdict_hash


def test_render_results_html_for_fixtures_and_audit(tmp_path: Path) -> None:
    ship_hash = _fixture_verdict_hash("ship")
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-11-29T04:00:00Z",
                        "id": "ship",
                        "verdict_hash": ship_hash,
                        "approver": "human-typed",
                        "change": {"description": "Promote verdict to 100%"},
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-11-29T03:00:00Z",
                        "id": "ship",
                        "verdict_hash": "stale000000000000",
                        "approver": "non-interactive",
                        "note": "legacy, produced during automated testing",
                        "change": {"description": "Legacy stale rollout"},
                    }
                ),
            ]
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
        assert "REAL-ONLY: n/a (no real data)" in html
        assert "Synthetic data:" in html
        assert "not evidence about human behaviour" in html
        assert "Autonomy Boundary" in html
        assert "Showing only approvals matching current verdict hash" in html
        if should_have_audit:
            assert "Promote verdict to 100%" in html
            assert "Legacy stale rollout" not in html
            assert "1 prior/mismatched or legacy entry(ies) excluded" in html
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
