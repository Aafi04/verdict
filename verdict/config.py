"""Deterministic thresholds and settings for Verdict."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Registered start timestamp (UTC) of Production Experiment 2 (result_view_experiment_v2).
# Rows recorded prior to this timestamp (e.g. pre-launch Experiment 1 phone-test row)
# remain untouched in SQLite and are excluded from Experiment 2 counts/checks.
EXPERIMENT_STARTED_AT_UTC = "2026-11-26T08:38:17Z"

# Freeze timestamp (UTC) moved from pre-registered 2026-11-29T03:30:00Z to
# collection-complete time (see docs/EXPERIMENT.md Deviations). All Production
# analyses filter SQLite rows up to this timestamp.
FREEZE_UTC = datetime(2026, 11, 26, 11, 21, 55, tzinfo=UTC)
FREEZE_UTC_ISO = "2026-11-26T11:21:55Z"


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load key=value pairs from .env without overriding existing env vars."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resolve_env_config(env_name: str) -> tuple[str, str, str, bool]:
    """Return (flagsmith_url, api_key, env_key, simulated) for an environment name."""
    load_dotenv()
    url = os.getenv("FLAGSMITH_URL", "http://localhost:8000").strip()
    api_key = os.getenv("FLAGSMITH_API_KEY", "").strip()
    normalized = env_name.strip().lower()
    if normalized == "simulation":
        env_key = os.getenv("FLAGSMITH_SIM_ENV_KEY", "").strip()
        simulated = True
    elif normalized == "development":
        env_key = (
            os.getenv("FLAGSMITH_DEV_ENV_KEY", "").strip()
            or os.getenv("FLAGSMITH_ENV_KEY", "").strip()
        )
        simulated = True
    elif normalized == "production":
        env_key = (
            os.getenv("FLAGSMITH_PROD_ENV_KEY", "").strip()
            or os.getenv("FLAGSMITH_ENV_KEY", "").strip()
        )
        simulated = False
    else:
        raise ValueError(f"Unsupported Flagsmith environment '{env_name}'.")
    return url, api_key, env_key, simulated


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
