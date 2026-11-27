"""Anonymised CSV exporter for Verdict quiz responses and look logs.

Writes CSV files to `data/export/`:
  - `visitors_anonymised.csv` (salted ephemeral hash replacing raw `vid`, hour-rounded timestamps)
  - `summary_by_arm.csv` (per-arm counts and rates, separating production vs simulation)
  - `looks_anonymised.csv` (integrity + effect look timeline from `data/looks.jsonl`, if present)

Guards against accidentally mixing real and simulated responses or reading
Production outcomes prior to the registered freeze time (`Nov 29, 2026 09:00 IST`).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import secrets
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verdict.config import EXPERIMENT_STARTED_AT_UTC, FREEZE_UTC, FREEZE_UTC_ISO


def _anon_id(vid: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{vid}".encode()).hexdigest()[:16]


def _round_hour_utc(raw_ts: str | None) -> str:
    if not raw_ts:
        return ""
    try:
        dt = datetime.fromisoformat(raw_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt.strftime("%Y-%m-%dT%H:00Z")
    except ValueError:
        return ""


def export_csvs(
    *,
    db_path: Path,
    looks_path: Path,
    out_dir: Path,
    mode: str = "production",
    since: str | None = EXPERIMENT_STARTED_AT_UTC,
    until: str | None = None,
    now: datetime | None = None,
    allow_pre_freeze: bool = False,
) -> dict[str, Path]:
    """Export anonymised visitor, summary, and look-log CSVs."""
    current = now or datetime.now(UTC)
    if mode == "production" and current < FREEZE_UTC and not allow_pre_freeze:
        raise PermissionError(
            "Pre-freeze guard: refusing to export Production outcomes before "
            f"{FREEZE_UTC.isoformat()} without --allow-pre-freeze."
        )
    effective_until = FREEZE_UTC_ISO if (until is None and mode == "production") else until

    out_dir.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(16)  # Ephemeral per-export salt; never persisted.

    rows: list[sqlite3.Row] = []
    if db_path.exists():
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(visitors)").fetchall()}
            has_synthetic = "synthetic" in cols
            syn_expr = "synthetic" if has_synthetic else "simulated"
            clauses: list[str] = []
            params: list[Any] = []
            if mode == "production":
                clauses.append(f"{syn_expr} = 0 AND simulated = 0")
            elif mode == "simulation":
                clauses.append(f"({syn_expr} = 1 OR simulated = 1)")
            if since:
                clauses.append("datetime(exposed_at) >= datetime(?)")
                params.append(since)
            if effective_until:
                clauses.append("datetime(exposed_at) <= datetime(?)")
                params.append(effective_until)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""
                SELECT
                    vid,
                    arm,
                    exposed_at,
                    simulated,
                    {syn_expr} AS synthetic,
                    s1_choice,
                    s2_choice,
                    completed_at
                FROM visitors
                {where}
                ORDER BY exposed_at ASC
                """,
                tuple(params),
            ).fetchall()
        finally:
            conn.close()

    visitors_csv = out_dir / "visitors_anonymised.csv"
    with visitors_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "anon_id",
                "arm",
                "simulated",
                "synthetic",
                "exposed_hour_utc",
                "s1_choice",
                "s2_choice",
                "completed",
            ]
        )
        for row in rows:
            is_syn = bool(int(row["synthetic"]) or int(row["simulated"]))
            writer.writerow(
                [
                    _anon_id(str(row["vid"]), salt),
                    row["arm"],
                    "true" if int(row["simulated"]) else "false",
                    1 if is_syn else 0,
                    _round_hour_utc(row["exposed_at"]),
                    row["s1_choice"] or "",
                    row["s2_choice"] or "",
                    "true" if row["completed_at"] else "false",
                ]
            )

    # Aggregate separately by (synthetic, arm) so real and synthetic are never merged silently.
    agg: dict[tuple[bool, str], dict[str, int]] = {}
    for row in rows:
        is_syn = bool(int(row["synthetic"]) or int(row["simulated"]))
        key = (is_syn, str(row["arm"]))
        bucket = agg.setdefault(
            key,
            {
                "exposures": 0,
                "completions": 0,
                "shipped_ambiguous": 0,
                "held_clear_win": 0,
            },
        )
        bucket["exposures"] += 1
        if row["completed_at"]:
            bucket["completions"] += 1
        if row["s1_choice"] == "ship":
            bucket["shipped_ambiguous"] += 1
        if row["s2_choice"] == "hold":
            bucket["held_clear_win"] += 1

    summary_csv = out_dir / "summary_by_arm.csv"
    with summary_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "dataset",
                "arm",
                "exposures",
                "completions",
                "shipped_ambiguous_count",
                "shipped_ambiguous_rate",
                "held_clear_win_count",
                "held_clear_win_rate",
            ]
        )
        for (is_sim, arm), stats in sorted(agg.items()):
            exp_n = stats["exposures"]
            ship_rate = f"{stats['shipped_ambiguous'] / exp_n:.4f}" if exp_n else ""
            hold_rate = f"{stats['held_clear_win'] / exp_n:.4f}" if exp_n else ""
            writer.writerow(
                [
                    "simulation" if is_sim else "production",
                    arm,
                    exp_n,
                    stats["completions"],
                    stats["shipped_ambiguous"],
                    ship_rate,
                    stats["held_clear_win"],
                    hold_rate,
                ]
            )

    written: dict[str, Path] = {"visitors": visitors_csv, "summary": summary_csv}

    if looks_path.exists():
        looks_csv = out_dir / "looks_anonymised.csv"
        with looks_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "ts",
                    "kind",
                    "source",
                    "experiment_id",
                    "env",
                    "total_exposures",
                    "integrity_status",
                    "verdict",
                ]
            )
            for raw in looks_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_exp = rec.get("total_exposures")
                if total_exp is None and isinstance(rec.get("sqlite_exposures"), dict):
                    total_exp = rec["sqlite_exposures"].get("total")
                writer.writerow(
                    [
                        rec.get("ts", ""),
                        rec.get("kind", ""),
                        rec.get("source", ""),
                        rec.get("experiment_id", ""),
                        rec.get("env", "simulation" if rec.get("simulated") else ""),
                        "" if total_exp is None else total_exp,
                        rec.get("integrity_status", ""),
                        rec.get("verdict", ""),
                    ]
                )
        written["looks"] = looks_csv

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Export anonymised Verdict CSVs to data/export/.")
    parser.add_argument("--db-path", default="data/quiz.db")
    parser.add_argument("--looks-path", default="data/looks.jsonl")
    parser.add_argument("--out-dir", default="data/export")
    parser.add_argument(
        "--mode",
        choices=("production", "simulation", "all"),
        default="simulation",
        help="Which visitor cohort to export (default: simulation; use production after freeze)",
    )
    parser.add_argument(
        "--allow-pre-freeze",
        action="store_true",
        help="Override pre-freeze protection when explicitly needed",
    )
    args = parser.parse_args()

    try:
        written = export_csvs(
            db_path=Path(args.db_path),
            looks_path=Path(args.looks_path),
            out_dir=Path(args.out_dir),
            mode=args.mode,
            allow_pre_freeze=bool(args.allow_pre_freeze),
        )
    except PermissionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for label, path in written.items():
        print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
