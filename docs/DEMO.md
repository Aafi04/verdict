# DEMO (target 2:50)
0:00 Hook: a results screen saying "86% chance to win" next to an interval that crosses zero. "Would you ship this?"
0:20 The live quiz and how many people have taken it. Explain the hypothesis in one sentence.
0:50 Flagsmith dashboard: raw experiment results (show as_of timestamp, exposures per arm).
1:10 Terminal: `verdict check result_view_experiment`. Walk the memo: verdict, top reasons, check table.
1:50 `verdict apply` dry-run, then typed approval on the dev environment; show the flag change and the audit log line. (If the verdict is HOLD, show that apply REFUSES - this is also a strong moment.)
2:15 Seeded-fault experiment (labelled simulated): Verdict REJECTS for sample ratio mismatch although the dashboard shows a "winner".
2:35 Autonomy table: what it does alone vs what needs a human.
2:45 Close: honest result of the real experiment + limitations.

Recording tips: record the video by Nov 29 afternoon; pre-seed the terminal; keep font large; no live debugging.
