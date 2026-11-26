"""Local SQLite store for quiz exposure de-duplication and guardrail logging.

Stores zero PII, zero IPs, and zero user agents — only the random anonymous
visitor UUID (`vid`), assigned arm, choices, timestamps, `simulated`, and
`synthetic` flags.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Arm = Literal["dashboard", "verdict"]
Choice = Literal["ship", "hold"]
Cohort = Literal["combined", "real", "synthetic"]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS visitors (
    vid TEXT PRIMARY KEY,
    arm TEXT NOT NULL CHECK (arm IN ('dashboard', 'verdict')),
    exposed_at TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0,
    synthetic INTEGER NOT NULL DEFAULT 0,
    s1_choice TEXT CHECK (s1_choice IN ('ship', 'hold')),
    s1_answered_at TEXT,
    s2_choice TEXT CHECK (s2_choice IN ('ship', 'hold')),
    s2_answered_at TEXT,
    completed_at TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class QuizDatabase:
    """SQLite persistence layer for quiz visitors and responses."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA_SQL)
            self._migrate_synthetic_column(conn)

    @staticmethod
    def _migrate_synthetic_column(conn: sqlite3.Connection) -> None:
        """Idempotently add `synthetic` column to existing DBs and backfill sim- rows."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(visitors)").fetchall()}
        if "synthetic" not in cols:
            conn.execute(
                "ALTER TABLE visitors ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE visitors SET synthetic = 1 WHERE simulated = 1 OR vid LIKE 'sim-%'"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_visitor(self, vid: str) -> dict[str, Any] | None:
        """Fetch visitor row by ID, or None if not exposed yet."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM visitors WHERE vid = ?", (vid,)).fetchone()
            return dict(row) if row is not None else None

    def record_exposure_once(
        self,
        vid: str,
        arm: Arm,
        *,
        simulated: bool = False,
        synthetic: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Insert exposure row if absent; return (row, created_now)."""
        now = _now_iso()
        is_syn = 1 if (synthetic or simulated or vid.startswith("sim-")) else 0
        is_sim = 1 if (simulated or is_syn) else 0
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO visitors (vid, arm, exposed_at, simulated, synthetic)
                VALUES (?, ?, ?, ?, ?)
                """,
                (vid, arm, now, is_sim, is_syn),
            )
            created = cur.rowcount == 1
            row = conn.execute("SELECT * FROM visitors WHERE vid = ?", (vid,)).fetchone()
            assert row is not None
            return dict(row), created

    def record_choice_once(
        self, vid: str, scenario: Literal["s1", "s2"], choice: Choice
    ) -> bool:
        """Record first answer for s1 or s2; return True only on first submission."""
        choice_col = f"{scenario}_choice"
        ts_col = f"{scenario}_answered_at"
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE visitors
                SET {choice_col} = ?, {ts_col} = ?
                WHERE vid = ? AND {choice_col} IS NULL
                """,
                (choice, now, vid),
            )
            return cur.rowcount == 1

    def mark_completed(self, vid: str) -> bool:
        """Mark visitor completed if both scenarios answered and not yet marked."""
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE visitors
                SET completed_at = ?
                WHERE vid = ?
                  AND s1_choice IS NOT NULL
                  AND s2_choice IS NOT NULL
                  AND completed_at IS NULL
                """,
                (now, vid),
            )
            return cur.rowcount == 1

    def count_real_exposures(self, *, since: str | None = None) -> int:
        """Return count of non-synthetic (`synthetic=0 AND simulated=0`) exposures."""
        sql = "SELECT COUNT(*) FROM visitors WHERE synthetic = 0 AND simulated = 0"
        params: tuple[Any, ...] = ()
        if since:
            sql += " AND datetime(exposed_at) >= datetime(?)"
            params = (since,)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

    def summarize(
        self,
        *,
        simulated: bool | None = None,
        cohort: Cohort | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate exposure, completion, and metric counts."""
        clauses: list[str] = []
        params: list[Any] = []
        if cohort == "real" or simulated is False:
            clauses.append("synthetic = 0 AND simulated = 0")
        elif cohort == "synthetic" or simulated is True:
            clauses.append("(synthetic = 1 OR simulated = 1)")
        if since:
            clauses.append("datetime(exposed_at) >= datetime(?)")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    arm,
                    COUNT(*) AS exposures,
                    SUM(CASE WHEN s1_choice = 'ship' THEN 1 ELSE 0 END) AS shipped_ambiguous,
                    SUM(CASE WHEN s2_choice = 'hold' THEN 1 ELSE 0 END) AS held_clear_win,
                    SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completions
                FROM visitors
                {where}
                GROUP BY arm
                """,
                tuple(params),
            ).fetchall()

        by_arm: dict[str, dict[str, int]] = {}
        totals = {"exposures": 0, "shipped_ambiguous": 0, "held_clear_win": 0, "completions": 0}
        for row in rows:
            arm_stats = {
                "exposures": int(row["exposures"] or 0),
                "shipped_ambiguous": int(row["shipped_ambiguous"] or 0),
                "held_clear_win": int(row["held_clear_win"] or 0),
                "completions": int(row["completions"] or 0),
            }
            by_arm[row["arm"]] = arm_stats
            for k in totals:
                totals[k] += arm_stats[k]

        return {"totals": totals, "by_arm": by_arm}
