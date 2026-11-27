"""Unit tests for deterministic Verdict checks and decision policy."""

from __future__ import annotations

from pathlib import Path

from verdict.checks import (
    check_arms_present,
    check_exposure_event_ratio,
    check_guardrail,
    check_interval_zero,
    check_looks,
    check_prob_vs_interval,
    check_sample_reached,
    check_srm,
    run_all_checks,
)
from verdict.client.fixture import FixtureClient
from verdict.config import VerdictThresholds
from verdict.models import Experiment, ExperimentResults, Exposure, Interval, MetricResult
from verdict.policy import evaluate_verdict


def _load_and_eval(fixture_id: str, prior_looks: int = 0):
    client = FixtureClient(Path("data/fixtures"))
    exp = client.get_experiment(fixture_id)
    exposures = client.get_exposures(fixture_id)
    results = client.get_results(fixture_id)
    thresholds = VerdictThresholds()
    checks = run_all_checks(exp, exposures, results, prior_looks=prior_looks, thresholds=thresholds)
    return evaluate_verdict(exp, exposures, results, checks, thresholds)


def test_fixtures_produce_expected_verdicts() -> None:
    ship = _load_and_eval("ship")
    assert ship.verdict == "SHIP"
    assert ship.winning_arm == "verdict"
    assert all(c.status == "PASS" for c in ship.checks)

    hold = _load_and_eval("hold")
    assert hold.verdict == "HOLD"
    by_name = {c.name: c for c in hold.checks}
    assert by_name["interval_zero"].status == "HOLD"
    assert by_name["sample_reached"].status == "HOLD"
    assert by_name["prob_vs_interval"].status == "WARN"

    insufficient = _load_and_eval("hold_insufficient_data")
    assert insufficient.verdict == "HOLD"
    ins_checks = {c.name: c for c in insufficient.checks}
    assert ins_checks["interval_zero"].status == "HOLD"
    assert "insufficient data" in ins_checks["interval_zero"].detail.lower()

    rej_srm = _load_and_eval("reject_srm")
    assert rej_srm.verdict == "REJECT"
    assert {c.name: c for c in rej_srm.checks}["srm"].status == "FAIL"

    rej_guard = _load_and_eval("reject_guardrail")
    assert rej_guard.verdict == "REJECT"
    assert {c.name: c for c in rej_guard.checks}["guardrail"].status == "FAIL"

    peeking = _load_and_eval("peeking", prior_looks=3)
    assert peeking.verdict == "HOLD"
    assert {c.name: c for c in peeking.checks}["looks"].status == "WARN"


def test_srm_crosscheck_fails_if_flagsmith_or_verdict_detects_skew() -> None:
    thresholds = VerdictThresholds()
    exp_res = ExperimentResults(as_of=None, srm_p_value=0.95, metrics=[])
    skewed = Exposure(
        as_of=None,
        planned_weights={"dashboard": 0.5, "verdict": 0.5},
        counts={"dashboard": 150, "verdict": 50},
    )
    assert check_srm(skewed, exp_res, thresholds).status == "FAIL"

    balanced = Exposure(
        as_of=None,
        planned_weights={"dashboard": 0.5, "verdict": 0.5},
        counts={"dashboard": 100, "verdict": 100},
    )
    flagsmith_flagged = ExperimentResults(as_of=None, srm_p_value=0.0002, metrics=[])
    assert check_srm(balanced, flagsmith_flagged, thresholds).status == "FAIL"


def test_arms_present_empty_fails_low_warns() -> None:
    thresholds = VerdictThresholds(min_exposures_per_arm=30)
    empty = Exposure(as_of=None, planned_weights={"a": 0.5, "b": 0.5}, counts={"a": 40, "b": 0})
    assert check_arms_present(empty, thresholds).status == "FAIL"

    low = Exposure(as_of=None, planned_weights={"a": 0.5, "b": 0.5}, counts={"a": 30, "b": 15})
    assert check_arms_present(low, thresholds).status == "WARN"

    ok = Exposure(as_of=None, planned_weights={"a": 0.5, "b": 0.5}, counts={"a": 35, "b": 30})
    assert check_arms_present(ok, thresholds).status == "PASS"


def test_interval_direction_increase_and_decrease() -> None:
    m_inc = MetricResult(
        name="conv",
        role="primary",
        expected_direction="increase",
        control_arm="control",
        variant_arm="v1",
        control_n=100,
        control_events=20,
        variant_n=100,
        variant_events=35,
        relative_lift=0.75,
        interval=Interval(low=0.12, high=1.40),
        win_probability=0.99,
    )
    assert check_interval_zero(m_inc).status == "PASS"

    m_dec_bad = m_inc.model_copy(
        update={"expected_direction": "decrease", "interval": Interval(low=-0.10, high=0.25)}
    )
    assert check_interval_zero(m_dec_bad).status == "HOLD"


def test_exposure_event_ratio_detects_events_without_exposures() -> None:
    thresholds = VerdictThresholds()
    exp = Exposure(
        as_of=None,
        planned_weights={"dashboard": 0.5, "verdict": 0.5},
        counts={"dashboard": 50, "verdict": 50},
    )
    broken = ExperimentResults(
        as_of=None,
        srm_p_value=1.0,
        metrics=[
            MetricResult(
                name="shipped_ambiguous",
                role="primary",
                expected_direction="decrease",
                control_arm="dashboard",
                variant_arm="verdict",
                control_n=50,
                control_events=75,
                variant_n=50,
                variant_events=10,
            )
        ],
    )
    assert check_exposure_event_ratio(exp, broken, thresholds).status == "FAIL"


def test_prob_vs_interval_guardrail_sample_and_looks() -> None:
    thresholds = VerdictThresholds()
    primary = MetricResult(
        name="shipped_ambiguous",
        role="primary",
        expected_direction="decrease",
        control_arm="dashboard",
        variant_arm="verdict",
        control_n=60,
        control_events=36,
        variant_n=60,
        variant_events=28,
        relative_lift=-0.22,
        interval=Interval(low=-0.50, high=0.09),
        win_probability=0.88,
    )
    assert check_prob_vs_interval(primary, thresholds).status == "WARN"
    assert check_guardrail([]).status == "WARN"
    assert check_sample_reached(
        Experiment(
            id="x", name="x", hypothesis="h", status="running", feature_name="f", planned_sample=200
        ),
        Exposure(as_of=None, planned_weights={"a": 0.5, "b": 0.5}, counts={"a": 60, "b": 60}),
    ).status == "HOLD"
    assert check_looks(prior_looks=2, sample_reached=False).status == "WARN"
    assert check_looks(prior_looks=2, sample_reached=True).status == "PASS"


def test_guardrail_interval_unavailable_holds_and_diff_ci_detects_regression() -> None:
    # Unavailable interval (< 50/arm) must HOLD, never PASS
    low_n_guard = MetricResult(
        name="held_clear_win",
        role="guardrail",
        expected_direction="not_increase",
        control_arm="dashboard",
        variant_arm="verdict",
        control_n=20,
        control_events=1,
        variant_n=20,
        variant_events=3,
        interval=None,
    )
    chk_hold = check_guardrail([low_n_guard])
    assert chk_hold.status == "HOLD"

    # Full experiment policy must return HOLD when guardrail interval is unavailable
    exp = Experiment(
        id="x", name="x", hypothesis="h", status="running", feature_name="f", planned_sample=40
    )
    exposure = Exposure(
        as_of=None,
        planned_weights={"dashboard": 0.5, "verdict": 0.5},
        counts={"dashboard": 20, "verdict": 20},
    )
    results = ExperimentResults(
        srm_p_value=1.0,
        metrics=[
            MetricResult(
                name="shipped_ambiguous",
                role="primary",
                expected_direction="decrease",
                control_arm="dashboard",
                variant_arm="verdict",
                control_n=20,
                control_events=14,
                variant_n=20,
                variant_events=4,
                relative_lift=-0.71,
                interval=Interval(low=-0.85, high=-0.20),
                win_probability=0.99,
            ),
            low_n_guard,
        ],
    )
    checks = run_all_checks(exp, exposure, results)
    assert evaluate_verdict(exp, exposure, results, checks).verdict == "HOLD"

    # 4/100 vs 12/100 (Flagsmith interval=None because ctrl<5) -> 95% diff CI excludes 0 -> FAIL -> REJECT
    regressed_guard = MetricResult(
        name="held_clear_win",
        role="guardrail",
        expected_direction="not_increase",
        control_arm="dashboard",
        variant_arm="verdict",
        control_n=100,
        control_events=4,
        variant_n=100,
        variant_events=12,
        interval=None,
    )
    chk_fail = check_guardrail([regressed_guard])
    assert chk_fail.status == "FAIL"
    assert "+8.0 pp" in chk_fail.detail

    # Stable guardrail counts (8/100 vs 9/100) with interval=None -> diff CI includes 0 -> PASS
    stable_guard = MetricResult(
        name="held_clear_win",
        role="guardrail",
        expected_direction="not_increase",
        control_arm="dashboard",
        variant_arm="verdict",
        control_n=100,
        control_events=8,
        variant_n=100,
        variant_events=9,
        interval=None,
    )
    assert check_guardrail([stable_guard]).status == "PASS"


def test_low_n_missing_arm_warns_while_adequate_n_missing_arm_fails() -> None:
    thresholds = VerdictThresholds(min_exposures_per_arm=30)
    tiny = Exposure(
        as_of=None,
        planned_weights={"dashboard": 0.5, "verdict": 0.5},
        counts={"dashboard": 2, "verdict": 0},
    )
    assert check_arms_present(tiny, thresholds).status == "WARN"

    seeded_fault = Exposure(
        as_of=None,
        planned_weights={"control": 0.5, "variant": 0.5},
        counts={"control": 90, "variant": 0},
    )
    assert check_arms_present(seeded_fault, thresholds).status == "FAIL"
    assert check_srm(seeded_fault, ExperimentResults(), thresholds).status == "FAIL"


def test_srm_states_when_equal_weights_assumed_for_completed_experiment() -> None:
    thresholds = VerdictThresholds()
    completed_exp = Exposure(
        as_of=None,
        planned_weights={"control": 0.5, "variant": 0.5},
        counts={"control": 105, "variant": 95},
        weights_assumed_equal=True,
    )
    chk = check_srm(completed_exp, ExperimentResults(srm_p_value=None), thresholds)
    assert chk.status == "PASS"
    assert "equal weights assumed" in chk.detail.lower()
