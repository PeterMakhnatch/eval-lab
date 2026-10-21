---
name: hackbook
description: Query and match Eval Lab's Hackbook (versioned catalog of known ways agents cheat and tasks break) across task packages, exported views, and agent trajectories via `evallab hackbook list|show|search|match`.
---

# Hackbook

The Hackbook (`research/hackbook/entries.jsonl`, schema `evallab.hackbook.entry/v0`) lists
known task defects (`TS01`–`TS11`) and run-side cheats/failures (`RS01`–`RS07`). Each entry
carries a mechanism, signatures (structural signals, file/trace regexes, globs), a detection
recipe, an exploit template, a fix pattern, and sourced examples (`executed` or `claimed`).

Everything here is static and costs $0: `match` reads files, never runs task code.

## Commands

```bash
evallab hackbook list [--class TS04|grader_tamperable] [--side task|run] [--source benchjack] [--evidence executed|claimed] [--json]
evallab hackbook show HB-TS04-002 [--json]            # id or slug; unique prefix works
evallab hackbook search "conftest pth" [--class ...] [--side ...] [--limit 10] [--json]
evallab hackbook match <target> [--kind auto|task|trace|view] [--class TS03] [--top 10] [--emit-findings DIR] [--json]
```

`match` targets:
- task package dir (`instruction.md`, `tests/`, `solution/`, `environment/`, `task.toml`; MiMo `tests/test.patch` ok);
- a task inside an exported view (`<view>/tasks/<name>`; refs are `tasks/<name>/...`);
- a whole exported view (`bundle.json` + `tasks/*`; per-task results plus an `aggregate`);
- a trace: ATIF `trajectory.json`, `actions_only` JSON/JSONL, a Harbor trial dir
  (`agent/trajectory.json`, `verifier/agent.diff`, `verifier/test-stdout.txt`, `result.json`),
  or a `qa.trace_bundle/v3` workspace.

Output: ranked hits with `entry_id`, `taxonomy_class_id`, `score`, `matched_signals`,
`regex_hits`, `evidence_refs` (`file:line` or `step:N`), plus the entry's recipe/exploit/fix.
`signals` lists every structural signal or trace tripwire that fired, with refs.
`--emit-findings DIR` writes `qa.finding/v1` files (one per hit; one per task × class for a
view), scorable with `evallab qa score --findings DIR`.

Python:

```python
from evallab.hackbook import load_hackbook, match_task, match_trace, match_target
entries = load_hackbook()
result = match_target(Path("path/to/task"), top=10)
```

## Hits are leads, not verdicts

A hit means a known pattern's surface is present. Before filing a `fail`, open every
`evidence_ref`, then run the entry's `detection_recipe` (probe, mutation, or control). Cite the
refs you actually checked. No hit does not mean clean.

## Per-lane use

- **Task QA (TQ-1):** run `evallab hackbook match <task> --json` first. For each top hit, read
  `show <id>` and follow `detection_recipe.steps`. Classes TS01–TS04 are wrong-pass risks;
  TS05–TS10 are wrong-fail risks. File only confirmed defects; keep the entry slug in
  `root_cause`.
- **Reward Seeker (RS-1):** pick hits with `exploit_template.applicable: true`; use
  `preconditions` to pick a target and `template_steps` to build the witness. A finding needs the
  differential: upstream grader passes it, fixed grader fails it (`differential_verdict`).
- **Trace QA (TR-1):** `evallab hackbook match <trial-dir> --kind trace`. Tripwires
  (`git_history_probe`, `tests_or_solution_read`, `import_hook_tamper`, `exit_or_skip_bypass`,
  `reward_file_write`, `hardcoded_return`, `infra_oom_or_timeout`, `unverified_success_claim`)
  point to `step:N`; read those steps and the diff before labelling RS01–RS07. Reading tests is
  not cheating by itself; copying the answer or tampering is.
- **Trace Rules (TL-1):** start new tripwire rules from `signatures.trace_command_regexes` and
  `trace_diff_regexes` (`evallab hackbook list --side run --json`). Measure precision on
  labelled traces before adding a rule.
- **Cheap Models (CM-1):** give the small model only the `match --json` output and the cited
  file lines, not the whole package. Ask it to confirm or reject each hit against its
  `evidence_refs`.
- **MiMo (MM-1):** match MiMo packages directly; `untested_reference_subblock` and
  `env_python_or_runtime_mismatch` cover the weak-test and stale-binary/runner patterns. Confirm
  with reference mutation before calling a test suite weak.

## Rules

- Never add `tb4-audit-v1` test-split task names or item ids to the Hackbook, this skill, or its
  tests. TB4 examples come only from dev-split tasks or benchmark-wide items.
  `validate_no_tb4_test_leak` and `research/hackbook/build.py --check` enforce this.
- Do not tune signals on test-split tasks. Calibrate on dev only.
- Catalog edits go through `research/hackbook/build.py` (regenerates `schema.json` and
  `summary.json`); keep `--check` passing.
