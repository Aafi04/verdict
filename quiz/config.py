"""Configuration for the Verdict Track 1 quiz application."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("verdict.quiz")

DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"
DEFAULT_DB_PATH = Path("data/quiz.db")


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


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class QuizSettings:
    """Runtime settings for the quiz app. Never serialized to HTTP responses."""

    flagsmith_url: str
    flagsmith_env_key: str
    cookie_secret: str
    db_path: Path
    scenarios_dir: Path
    flag_name: str = "result_view"
    simulated_mode: bool = False
    rate_limit_per_min: int = 120

    @classmethod
    def from_env(cls) -> QuizSettings:
        load_dotenv()
        simulated_mode = _parse_bool(os.getenv("QUIZ_SIMULATED_MODE"), default=False)
        env_key = (
            os.getenv("FLAGSMITH_SIM_ENV_KEY", "").strip()
            if simulated_mode and os.getenv("FLAGSMITH_SIM_ENV_KEY", "").strip()
            else os.getenv("FLAGSMITH_ENV_KEY", "").strip()
        )
        raw_secret = os.getenv("QUIZ_COOKIE_SECRET", "").strip()
        if not raw_secret or raw_secret == "change-me":
            logger.warning(
                "QUIZ_COOKIE_SECRET is unset or default; generating an ephemeral per-process secret."
            )
            cookie_secret = secrets.token_hex(32)
        else:
            cookie_secret = raw_secret

        rate_raw = os.getenv("QUIZ_RATE_LIMIT_PER_MIN", "120").strip()
        try:
            rate_limit = max(1, int(rate_raw))
        except ValueError:
            rate_limit = 120

        return cls(
            flagsmith_url=os.getenv("FLAGSMITH_URL", "http://localhost:8000").strip(),
            flagsmith_env_key=env_key,
            cookie_secret=cookie_secret,
            db_path=Path(os.getenv("QUIZ_DB_PATH", str(DEFAULT_DB_PATH))),
            scenarios_dir=Path(os.getenv("QUIZ_SCENARIOS_DIR", str(DEFAULT_SCENARIOS_DIR))),
            flag_name=os.getenv("QUIZ_FLAG_NAME", "result_view").strip() or "result_view",
            simulated_mode=simulated_mode,
            rate_limit_per_min=rate_limit,
        )
