# Typed harness components — offline results (2026-09-16)

Model: GLM 5.3 Flash, hidden thinking off. Artifacts: `artifacts/recovery-eval.json`,
`artifacts/decompose-eval.json`; fixture: `fixtures/recovery-steps.json`
(17 failure steps mined from 27 retained codex ATIF trajectories in the primary
checkout's `runs/`, all gpt-5.6-terra/luna, August 2026 canaries).

These are consistency checks against retained behaviour. They do not show that
either module improves an agent; that needs a Harbor comparison through the Lab.

## `ErrorRecovery` on 17 real failure steps

| Check | Result |
|---|---|
| Typed validity (parses into `RepairStrategy`, closed `kind` enum) | 17/17 |
| Repair *class* matches a heuristic class of the agent's actual next command | 9/17 |
| Proposed command starts with the same program the agent actually ran next | 7/17 |
| `kind` distribution | `fix_command` 12, `inspect_state` 3, `install_or_substitute_tool` 2 |
| Cost / time | 17 calls, ≈$0.010, 229 s sequential |

Reading the 17 rows (`recovery-eval.json`), not the aggregate:

- **Diagnoses read the error literally and are right.** `jq: command not found` →
  missing binary, not a data problem (3/3). Sandbox `CreateProcess Rejected`
  traced to `rm -rf` inside a `trap` (2/2). `printf '%s'` writing literal
  `\r\n` instead of CRLF bytes → `printf '%b'`. `apply_patch` hunk mismatch →
  atomic abort, re-issue the patch. `pytest: command not found` → `python -m pytest`.
- **Most "disagreements" are format, not judgement.** The codex agent edits
  files through its `apply_patch` JS wrapper; the module proposes a Python
  heredoc doing the same edit. The heuristic counts that as a different program.
  For the `jq` cases the module substitutes `python3` where the agent chose
  `node` — same repair class, different tool.
- **Real misses:** it labelled a 300-second `are_wait_for_notification` timeout
  `inspect_state` and proposed a non-existent `check_current_state` command; the
  agent instead searched other tools. It proposed `cat -n events.py` after a
  failing watermark assertion where the agent went straight to a targeted
  Python probe — `inspect_state` was defensible but slower.
- **Bias:** 12/17 `fix_command`. With no optimizer run, the enum's finer kinds
  (`fix_path`, `fix_environment`, `retry_same`, `stop_and_report`) were never chosen.

## `TaskDecomposer` on the 9 Lab tasks under `library/tasks`

| Check | Result |
|---|---|
| Typed validity (`ExecutionPlan`, 1–8 sub-goals) | 9/9 |
| Planned/required paths grounded in the instruction or `/app` listing | 40/40 (tau3-retail-1 planned 0 paths: a tool-calling task with no files) |
| Sub-goals per task | 3–4 |
| Cost / time | 9 calls, ≈$0.003, 117 s |

Path grounding is a hallucination check, not a correctness check: a plan can
name only real paths and still miss the verifier's requirement. The next
deterministic check worth adding is whether `required_outputs` covers every
artifact the task's `task.toml` `artifacts` list or verifier reads.

## What would make this a real result

1. A Harbor A/B through the Lab's queue: the same agent with and without the
   recovery module on a development cohort, paired by task. Requires registering
   the agent profile/import path (`execution_contracts.py`, `profiles.py`) — the
   integration lane's surface.
2. A labelled recovery set: the agent's actual next step is not a gold label.
   Reviewer-written "best next action" for the 17 steps (and more from the
   GLM trajectories once bash-heavy tasks are in the retained corpus) would make
   the class-agreement number meaningful and give GEPA something to optimise.
