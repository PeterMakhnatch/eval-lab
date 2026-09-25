# Trial diagnosis precision audit — 2026-09-25

> **Agent-reviewed, NOT human labels.** `annotator_kind=code`
> (`trial-diagnosis-audit-20260925`). Nothing here enters the
> `labels.py` human-label path, and no entry is promoted to ground truth.
> Per-trial verdicts: `trial-diagnosis-precision-audit-2026-09-25.json`
> (same directory).

## Corpus and method

- **Corpus:** 213 real trials diagnosed with zero crashes: 49 harness-pilot
  mini-swe-agent trials (5 jobs), 4 HAR-71 Terminus 2 trials, 20 exp05
  Terminus-via-Reef trials (`research-context/reef/experiments/work/05/view/`,
  read-only), and 140 lab `runs/` trials. Outcomes: 160 scored, 41
  infra-failed, 12 unscored. Zero modes attach to unscored/infra trials.
- **Sample:** 46 trials, stratified across sources (20 harness-pilot, 4 HAR-71,
  18 lab runs, 4 exp05), pass/fail (18 passes, 24 scored fails, 4
  infra/unscored), and every proposed mode. Each trajectory was read via
  per-step command/output packs; every proposed mode got agree, weak-agree,
  or disagree with a reason. exp05 `error-count`/`top-words` carry external
  ground truth (experiments README `## 05`); both were recovered as
  `unclassified_failure` with matching terminal evidence.
- Detector under audit: `trial_diagnosis/v1`
  (`src/evallab/trial_diagnosis.py`), taxonomy
  `trial_diagnosis/failure_mode/v1`.

## Per-label precision

| Mode | Fired (wild) | Sampled | Agree | Weak-agree | Disagree |
|---|---|---|---|---|---|
| `unrecovered_error` | 18 | 8 (+10 excerpt-scanned) | 18 | 0 | 0 |
| `unclassified_failure` | 23 | 14 | 14 | 0 | 0 |
| `silent_tool_output` | 7 | 7 (2 fully read, 5 excerpt-verified) | 1 | 6 | 0 |
| `tool_use_loop` | 4 | 4 | 4 | 0 | 0 |
| `planning_no_edit` | 2 | 2 | 1 | 1 | 0 |
| `wrong_tool_arguments` | 0 | 0 | — | — | — |
| `state_persistence_assumption` | 0 | 0 | — | — | — |
| `no_tool_use` | 0 | 0 | — | — | — |
| `empty_terminal_reply` | 0 | 0 | — | — | — |

Weak-agree means the cited evidence is real but only mildly diagnostic
(e.g. a `find` with zero matches, a silent `python -c`). The four
zero-fire modes are covered by 25 fixture-based unit tests, not by wild
observations — stated as a limit, not as precision.

Outcome separation held everywhere: all 14 sampled passes and all 4 sampled
infra/unscored trials are modeless, and all 53 unscored/infra trials in the
full corpus carry zero modes.

## False positives found and fixed during this audit

1. `wrong_tool_arguments` fired on an agent script's own `TypeError`
   traceback (django-12308). Tracebacks are now excluded: they name
   agent-authored code failing, not a tool call with wrong arguments.
2. `state_persistence_assumption` fired on the word "NameError" inside
   `sed`-displayed source comments, and on task-domain repro output. The
   branch now requires a failing step, a `NameError: name 'X'` match, and an
   earlier agent command that defines `X`.
3. `silent_tool_output` fired on heredoc writes, `sed -i`, `cp` backups,
   `mkdir`/`cd` chains, comment-only commands, and `git checkout`
   restores. Expected-silence now covers mutations, chained all-quiet
   segments, comment-only commands, and git restore verbs.
4. `planning_no_edit` fired on MCP-app trials with no files (gaia pair).
   The mode is now gated on file-task evidence, and SQL writes plus
   later-in-step calls count as edits (ledger-candidate's `UPDATE` had been
   missed as the 6th call of its step).

## Notable misses (recall gaps, deliberately out of scope)

- File corruption via edit misuse (`sed -i '1218,1225p'`, django-13417).
- Malformed harness replies rejected as invalid JSON (har71 baselines).
- Invalid-JSON output with right counts (exp05 `error-count`).
- Wrong aggregation logic, e.g. `grep -F` stopword filtering (exp05 `top-words`).
- Editing the wrong copy of the code (requests-1142: pip-vendored vs `/testbed`).
- Chained-`cd` state loss (only bare `cd` is detected).

## Incidental lab finding (not this slice)

The outline's error taxonomy does not unwrap mini-swe-agent
`{"returncode": N, "output": ...}` envelopes: real failed shell trials
report `total_errors = 0` and `unrecovered_at_terminal = False`
(verified on django-10999 and a har71 trial). Diagnosis recomputes
terminal-error state locally from envelope-aware views; the outline itself
is unchanged (projection parity). A lab-side fix belongs to the Platform
lane, not this PR.

## Provenance

Coverage inputs enumerated 2026-09-25 from `~/Developer/harness-pilot/jobs`
(5 jobs), `.worktrees/har71-terminus-harness-20260924/runs` (4 jobs),
`research-context/reef/experiments/work/05/view` (3 jobs, read-only), and
the primary checkout `runs/` (140 entries, read-only). No trial directory
was modified. Full per-trial table: `derived/trial-diagnosis/coverage.json`
(ignored worktree scratch; counts reproduced in the PR receipt).
