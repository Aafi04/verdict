"""Append-only JSONL logging for experiment looks and human-approved mutations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verdict.models import Experiment, Exposure, VerdictResult

DEFAULT_LOOKS_PATH = Path("data/looks.jsonl")
DEFAULT_AUDIT_PATH = Path("data/audit.jsonl")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def count_prior_looks(experiment_id: str, looks_path: Path = DEFAULT_LOOKS_PATH) -> int:
    """Count prior check runs logged for `experiment_id`."""
    return sum(
        1
        for rec in _read_jsonl(looks_path)
        if str(rec.get("experiment_id")) == str(experiment_id)
    )


def append_look(
    experiment: Experiment,
    exposure: Exposure,
    verdict: VerdictResult,
    looks_path: Path = DEFAULT_LOOKS_PATH,
) -> dict[str, Any]:
    """Append an experiment check entry to looks.jsonl."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "source": "verdict.cli",
        "experiment_id": experiment.id,
        "experiment_name": experiment.name,
        "simulated": experiment.simulated,
        "total_exposures": exposure.total,
        "planned_sample": experiment.planned_sample,
        "counts": exposure.counts,
        "verdict": verdict.verdict,
        "verdict_hash": verdict.verdict_hash[:12],
    }
    looks_path.parent.mkdir(parents=True, exist_ok=True)
    with looks_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def read_looks(
    looks_path: Path = DEFAULT_LOOKS_PATH, limit: int = 20
) -> list[dict[str, Any]]:
    """Return the most recent `limit` entries from looks.jsonl."""
    records = _read_jsonl(looks_path)
    return records[-limit:] if limit > 0 else records


def append_audit(entry: dict[str, Any], audit_path: Path = DEFAULT_AUDIT_PATH) -> None:
    """Append a human-approved write action record to audit.jsonl."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
