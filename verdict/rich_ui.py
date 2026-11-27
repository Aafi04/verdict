"""Optional Rich terminal rendering for Verdict CLI (falls back gracefully if unavailable)."""

from __future__ import annotations

import json
from typing import Any

from verdict.memo import MemoInput
from verdict.models import ProposedChange, VerdictResult

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

_VERDICT_COLORS = {
    "SHIP": "bold green",
    "HOLD": "bold yellow",
    "REJECT": "bold red",
}

_STATUS_COLORS = {
    "PASS": "green",
    "WARN": "yellow",
    "HOLD": "yellow",
    "FAIL": "bold red",
}


def render_rich_check(memo: MemoInput) -> bool:
    """Print a Rich-formatted Verdict panel and checks table; return False if Rich unavailable."""
    if not HAS_RICH:
        return False

    console = Console()
    v_style = _VERDICT_COLORS.get(memo.verdict, "bold white")
    border_style = {"SHIP": "green", "HOLD": "yellow", "REJECT": "red"}.get(memo.verdict, "white")

    header = Text()
    header.append(f"VERDICT: {memo.verdict}", style=v_style)
    if memo.simulated:
        header.append(" [SIMULATED]", style="bold yellow")
    header.append(f"\nExperiment: {memo.experiment_name}\n", style="bold")
    header.append(f"{memo.summary}\n")
    if memo.simulated or (memo.synthetic_n is not None and memo.synthetic_n > 0):
        header.append(
            "\nSynthetic data: primarily synthetic (sim- identities, synthetic=1); "
            "shows pipeline behaviour, not human evidence.\n",
            style="yellow",
        )
    if memo.combined_n is not None and memo.real_n is not None and memo.synthetic_n is not None:
        header.append(
            f"Cohorts: combined N={memo.combined_n} "
            f"(real N={memo.real_n}, synthetic N={memo.synthetic_n}) | "
            f"combined={memo.verdict} | real-only={memo.real_only_verdict}\n",
            style="cyan",
        )
    if memo.disagreement_detail:
        header.append(f"DISAGREEMENT: {memo.disagreement_detail}\n", style="bold red")

    header.append(f"\nPrimary: {memo.primary_metric}\n", style="bold")
    header.append(
        f"  Lift: {memo.lift}  |  95% CI: {memo.interval}  |  Win prob: {memo.win_probability}\n"
    )
    header.append(f"  Sample: {memo.sample_status}\n")
    header.append(f"Guardrail: {memo.guardrail_status}")

    console.print(Panel(header, title="Verdict Decision Memo", border_style=border_style))

    if memo.checks:
        tbl = Table(title="Deterministic Checks", show_lines=False, header_style="bold cyan")
        tbl.add_column("Status", width=8)
        tbl.add_column("Check", style="bold", width=22)
        tbl.add_column("Detail")
        for chk in memo.checks:
            c_style = _STATUS_COLORS.get(chk.status, "white")
            tbl.add_row(Text(f"[{chk.status}]", style=c_style), chk.name, chk.detail)
        console.print(tbl)

    if memo.next_action:
        console.print(Panel(memo.next_action, title="Recommended Action", border_style="blue"))
    return True


def render_rich_approval_prompt(
    *,
    title: str,
    verdict: VerdictResult | None,
    change: ProposedChange,
    expected_phrase: str,
    dry_run: bool,
) -> bool:
    """Render Rich panel for `verdict apply` or `verdict rollback`; return False if unavailable."""
    if not HAS_RICH:
        return False

    console = Console()
    body = Text()
    if verdict is not None:
        v_style = _VERDICT_COLORS.get(verdict.verdict, "bold")
        body.append(f"VERDICT: {verdict.verdict}", style=v_style)
        body.append(f"  (hash={verdict.verdict_hash[:12]}...)\n", style="dim")
    body.append(f"Proposed change: {change.description}\n", style="bold")
    body.append(f"Payload: {json.dumps(change.payload)}\n", style="cyan")
    if dry_run:
        body.append("\nDRY-RUN ONLY: no changes written to Flagsmith.\n", style="bold yellow")
        body.append(f"To execute, re-run with --execute and type: {expected_phrase}")
    else:
        body.append(
            f"\nHUMAN APPROVAL REQUIRED: type exact phrase '{expected_phrase}' below.",
            style="bold yellow",
        )
    console.print(Panel(body, title=title, border_style="yellow"))
    return True


def render_rich_execution_result(title: str, payload: dict[str, Any], audit_path: str) -> bool:
    """Render Rich confirmation panel after approved execution."""
    if not HAS_RICH:
        return False

    console = Console()
    body = Text()
    body.append(f"{json.dumps(payload, indent=2)}\n\n", style="green")
    body.append(f"Audit entry recorded in {audit_path}.", style="dim")
    console.print(Panel(body, title=title, border_style="green"))
    return True
