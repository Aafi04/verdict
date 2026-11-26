"""Flagsmith Python SDK wrapper for the quiz app."""

from __future__ import annotations

import logging
from typing import Protocol

from flagsmith import EventProcessorConfig, Flagsmith

from quiz.db import Arm

logger = logging.getLogger("verdict.quiz.sdk")


class QuizSdk(Protocol):
    """Protocol for Flagsmith experiment evaluation and event tracking."""

    def evaluate_and_expose(self, identifier: str) -> Arm: ...

    def track_event(self, event_name: str, identifier: str) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class FlagsmithQuizSdk:
    """Live Flagsmith SDK wrapper using Flagsmith==6.7.0 experiment + event APIs."""

    def __init__(self, base_url: str, env_key: str, flag_name: str = "result_view") -> None:
        self.flag_name = flag_name
        clean = base_url.rstrip("/")
        self._client = Flagsmith(
            environment_key=env_key,
            api_url=f"{clean}/api/v1/",
            enable_events=True,
            event_processor_config=EventProcessorConfig(
                events_api_url=f"{clean}/api/",
                max_buffer_items=100,
                flush_interval_seconds=1.0,
            ),
        )

    def evaluate_and_expose(self, identifier: str) -> Arm:
        try:
            flag = self._client.get_experiment_flag(self.flag_name, identifier=identifier)
            val = str(getattr(flag, "value", "") or "").strip()
            if val in ("dashboard", "verdict"):
                return val  # type: ignore[return-value]
            logger.warning("Unexpected flag value %r for %s; defaulting to dashboard", val, self.flag_name)
            return "dashboard"
        except Exception:
            logger.exception("Flagsmith evaluation failed; falling back to dashboard")
            return "dashboard"

    def track_event(self, event_name: str, identifier: str) -> None:
        try:
            self._client.track_event(event_name, identifier=identifier)
        except Exception:
            logger.exception("Flagsmith track_event failed for event=%s", event_name)

    def flush(self) -> None:
        try:
            self._client.flush_events()
        except Exception:
            logger.exception("Flagsmith flush_events failed")

    def close(self) -> None:
        try:
            self._client.close(timeout_seconds=5)
        except Exception:
            logger.exception("Flagsmith close failed")


class NoopQuizSdk:
    """Offline/fallback SDK when FLAGSMITH_ENV_KEY is unset."""

    def evaluate_and_expose(self, identifier: str) -> Arm:
        return "verdict" if sum(identifier.encode("utf-8")) % 2 else "dashboard"

    def track_event(self, event_name: str, identifier: str) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None
