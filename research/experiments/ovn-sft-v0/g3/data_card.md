# G3 SFT set: data card

- Rows: 145 (one model call each), from 5 trials on 5 tasks.
- Sequence tokens: 1,569,955 kept of 1,569,955 (stride 1); per row min/median/max [1023, 9211, 31403].
- Fidelity: prompt tokens exact 145/145, completion tokens exact 145/145, captured messages identical 145/145.
- conversations.jsonl sha256:71a9f7073a0ce4e2a12865fc2a0a74881986ec30067a18bf7e78a528a3875c94

## Sources

| source | rows | sequence tokens | target tokens |
|---|---|---|---|
| captured | 145 | 1,569,955 | 39,913 |

## Trials

`format_warning_steps_kept` (Traces, HAR-128): kept steps whose observation carries a Terminus-2 warning (missing duration, missing newline, parse error); the assistant output that caused it is trained on.

| job | trial | source | cut_step_id | format_warning_steps_kept |
|---|---|---|---|---|
| HAR-120-har120-000552-a2-r2 | har120-000552-a2-r2__ptsTP5m | captured | 30 | None |
| HAR-120-har120-000941-a2-r2 | har120-000941-a2-r2__VrxFqqG | captured | 33 | None |
| HAR-120-har120-002416-a1-r2 | har120-002416-a1-r2__ccd9JcE | captured | 30 | None |
| HAR-120-har120-002555-a1-r2 | har120-002555-a1-r2__Uki9j6d | captured | 41 | None |
| HAR-120-har120-002938-a1 | har120-002938-a1__UpSjiuM | captured | 16 | None |

## Rows per task

| task | rows |
|---|---|
| format-code-task-000552 | 29 |
| format-code-task-000941 | 32 |
| format-code-task-002416 | 29 |
| format-code-task-002555 | 40 |
| format-code-task-002938 | 15 |

## Exclusions

| job | trial | task | reason |
|---|---|---|---|
| HAR-120-har120-000838-a1 | har120-000838-a1__YSe3G7t | format-code-task-000838 | traces:no_completion_in_kept_window |
| HAR-120-har120-002552-a1-r2 | har120-002552-a1-r2__ZftRzg3 | format-code-task-002552 | traces:not_clean (Genuine pass, but the kept window has 8 identical empty-message greps (head#53-60) that drew the loop nudge; no single cut drops them and keeps the fix (both raters).) |
