"""Deterministic thresholds and settings for Verdict."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerdictThresholds:
    """Thresholds governing integrity checks and decision policy."""

    srm_p_threshold: float = 0.001
    min_exposures_per_arm: int = 30
    misleading_win_prob_threshold: float = 0.80
    max_event_over_exposure_ratio: float = 1.05
    max_excluded_identity_share: float = 0.05
    flat_events_exposure_threshold: int = 50
    default_planned_sample: int = 200
