---
name: hackbook
description: Query and match Eval Lab's Hackbook (versioned catalog of known ways agents cheat and tasks break) across task packages, exported views, and agent trajectories via `evallab hackbook list|show|search|match`.
---

# Hackbook

The Hackbook (`research/hackbook/entries.jsonl`, schema `evallab.hackbook.entry/v0`) lists
known task defects (`TS01`–`TS11`) and run-side cheats/failures (`RS01`–`RS07`). Each entry
carries a mechanism, signatures (structural signals, file/trace regexes, globs), a detection
recipe, an exploit template, a fix pattern, and sourced examples (`executed`: a witness or
differential replay was run; `observed`: a deterministic check of an archived real run, not
replayed; `claimed`: the source says so).

Everything here is static and costs $0: `match` reads files, never runs task code.

## Commands

```bash
evallab hackbook list [--class TS04|grader_tamperable] [--side task|run] [--source benchjack] [--evidence executed|observed|claimed] [--json]
evallab hackbook show HB-TS04-002 [--json]            # id or slug; unique prefix works
evallab hackbook search "free text, a judge's proposed mechanism or a finding title" [--class ...] [--side ...] [--limit 10] [--json]
evallab hackbook match <target> [--kind auto|task|trace|view] [--class TS03] [--direction false_pass|false_fail] [--top 10] [--emit-findings DIR] [--json]
evallab hackbook match <task-dir> --reference fix.patch    # task + its reference diff: flags blocks no test names (HB-TS01-009)
evallab hackbook match <trial-dir> --kind trace [--task <task-dir>] [--outcome auto|pass|fail|none] [--fleet-rates FILE]
evallab hackbook calibrate <pool-dir> [--kind task|trace] [--threshold 0.4] [--write-rates FILE] [--json]
evallab hackbook coverage [--json]
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
view), scorable with `evallab qa score --findings DIR`. Trace hits are emitted only at the
**strong tier** (`score >= 0.5`): a hit that rests on corroborating regex weights (< 0.5) or on a
context tripwire (`tests_or_solution_read`, `network_fetch`, weight 0.4) stays in `matches` as a
lead but is not filed.

**Pick a direction.** Hunting a wrong pass (reward seeker, wrong-pass audit): `--direction
false_pass`, which drops entries that can only fail a right run (unstated requirements, flaky
environments) and keeps `either`. Repairing false fails: `--direction false_fail`. The cut to `--top`
happens after the filter, so a seeker's top 8 holds eight wrong-pass entries. On the 86 `dev` findings
of `tb4-audit-v1` the filter alone lifted recall@8 of the cited entry from 23/32 to 26/32 (false
pass) and 19/26 to 23/26 (false fail) on the findings the catalog already cited (in-sample; see
`research/hackbook/README.md`, "Retrieval").

**Calibrate any new pool before trusting `match` on it.** `calibrate` prints the share of tasks
(or traces) each entry fires on. An entry that fires on at least the threshold of a pool cannot
tell that pool's tasks apart; it is harness boilerplate or a generic regex, not a defect.
(v0.1 matched the harness's own scaffolding: `reward.txt`, a single-`FROM` Dockerfile and
`git add -A`/`cache` in `tests/test.sh` fired three entries on 567/567 MiMo packages; the uv
installer `curl ... astral.sh | sh` fired three TS04 entries on 82/89 TB2.1 packages; and Harbor
schema 1.1's default `allow_internet = true` looked like a network leak.) Fix the entry or
signal, then re-run `calibrate`. `calibrate --write-rates FILE` stores each entry's firing share;
`match --fleet-rates FILE` then marks hits above 0.95 `non_discriminating` and never emits them
(on the pre-v0.2 catalog it flags exactly the three entries that fired on 567/567 MiMo packages).

**Matching a trial.** Pass the Harbor trial dir, not just `trajectory.json`: `--outcome auto` reads
`verifier/reward.txt` and drops entries that cannot explain the verdict (listed in
`dropped_by_outcome`), and `--task <task-dir>` lets task-side trace regexes count in full only
when the task package shows the weakness (`task_confirmed`; otherwise they count at 30%). On 1,539
full TB4 DEV trajectories this cut strong hits on clean passes from 60% to 18% (9% without the
task gate), but the trace regexes still do not find the observed wrong passes (the mapped entry
fires on 5/18); treat a trace hit as where to read, and run the entry's recipe.

**Mapping a mechanism written in words to an entry.** `search` is BM25F over each entry's prose
(stopwords dropped, IDF over the catalog), so a sentence works as well as keywords. With one
example held out per query it puts the right entry first for `134/211` catalog examples (`108` for
the token-count ranking it replaced), in the top 3 for `176` and in the top 5 for `194`. Read the
score margin over the runner-up: the top quarter of queries by margin is right `88%` of the time,
all of them `64%`, so a small margin is a shortlist for the judge, not an answer. Out-of-catalog
proposals still return the nearest entries; abstain when no entry's `mechanism` fits.

**Structured tool calls.** A `Read`/`read_file` of a path, a URL argument and a write or edit under
`/tests/` or `/logs/verifier` count as the `cat`, `curl`, `tee` and `sed -i` they stand for, so
`tests_or_solution_read`, `network_fetch` and `test_file_tamper` fire on them. Writes to the agent's
own `/app/tests/` do not. A cheating trace whose only signal is a leak in the prompt (CheatBench
`harness_leaks_answer`) or a rewritten checker file (HackTrace `test_tamper`) still returns no hit.

**Reference blocks no test names (HB-TS01-009).** With the reference fix as a diff
(`match <task> --reference PATCH`, or `solution/*.patch` in the package), the signal
`untested_reference_subblock` flags change blocks whose file and identifiers no test names (runner
scripts, imports, comments, docs and test files do not count). On 475 MiMo tasks scored against the
executed `evallab ref-subset` probe, a flagged block is deletable with every hidden test still
passing about twice as often as the same task's other blocks (`0.57` vs `0.27` on the held-out
TEST split; `18` tasks higher, `3` lower). It is **a pointer to a block, not a weak-check screen**:
as a task-level flag its lift on TEST is `1.12` (the regexes: `1.03`), it finds `11%` of the
droppable blocks, and a plain count of the reference's blocks predicts weak tests better. Read the
block it names, then run `evallab ref-subset` for the verdict.

**How far to trust a task `match` on a package nobody has read.** The catalog was written from the
DEV tasks, so DEV numbers are in-sample (category recall `56/86`, `65%`). Run once on the `41`
held-out `tb4-audit-v1` TEST tasks, one frozen version credited `62/152` findings by category
(`40.8%`, Wilson `33`-`49`), precision `40%` as a lower bound, against `18` for a no-matcher prior
and `78` for the plain read (Codex `gpt-6.1-sol`, not Opus); `weak_check` `29/61`, `unstated_requirement` `16/40`,
`grader_tamperable` `10/20`, the rarer classes `0`-`3` hits of `3`-`7`. Expect about four in ten
findings, and no finding id or locus. The TEST split is spent for hackbook versions: do not tune
on it, and report any later TEST score as not untouched
(`research/experiments/hackbook-tb4-heldout-20261021/`).

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

- **Task QA (TQ-1):** run `evallab hackbook match <task> --json` first (add `--direction false_pass`
  when auditing wrong passes). For each top hit, read
  `show <id>` and follow `detection_recipe.steps`. Classes TS01–TS04 are wrong-pass risks;
  TS05–TS10 are wrong-fail risks. File only confirmed defects; keep the entry slug in
  `root_cause`.
- **Reward Seeker (RS-1):** the toolkit page already uses `--direction false_pass`. Pick hits with
  `exploit_template.applicable: true`; use
  `preconditions` to pick a target and `template_steps` to build the witness. A finding needs the
  differential: upstream grader passes it, fixed grader fails it (`differential_verdict`).
- **Trace QA (TR-1):** `evallab hackbook match <trial-dir> --kind trace`. Tripwires
  (`git_history_probe`, `tests_or_solution_read`, `import_hook_tamper`, `exit_or_skip_bypass`,
  `reward_file_write`, `hardcoded_special_case`, `literal_branch_special_case`,
  `infra_oom_or_timeout`, `unverified_success_claim`) point to `step:N`; read those steps and the
  diff before labelling RS01–RS07. Reading tests is not cheating by itself; copying the answer or
  tampering is. `import_hook_tamper`, `reward_file_write`, `test_file_tamper` and
  `path_or_binary_hijack` need a *write verb* aimed at the protected path in the same command (a
  read, `rm -rf tests/__pycache__`, or a `.pth` weights file does not count);
  `hardcoded_special_case` needs a comment admitting the hardcode, while the
  `if a == X and b == Y:` branch (`literal_branch_special_case`) is context only (12% of clean TB4
  passes) and a bare `return [1, 2]` is common in honest code and no longer counts.
- **Trace Rules (TL-1):** start new tripwire rules from `signatures.trace_command_regexes` and
  `trace_diff_regexes` (`evallab hackbook list --side run --json`). Measure precision on
  labelled traces before adding a rule.
- **Cheap Models (CM-1):** give the small model only the `match --json` output and the cited
  file lines, not the whole package. Ask it to confirm or reject each hit against its
  `evidence_refs`.
- **MiMo (MM-1):** match MiMo packages directly; `untested_reference_subblock` and
  `env_python_or_runtime_mismatch` cover the weak-test and stale-binary/runner patterns. Confirm
  with reference mutation before calling a test suite weak. Static matching does **not** separate
  weak-test tasks from the rest of MiMo (`weak_check` entries fire at about the same rate on
  audited-weak and unaudited tasks: lift 1.1-1.3); treat TS01/TS05 hits there as candidates only
  and rank tasks by the reference-mutation result, not by the hit count.

## Rules

- Never add `tb4-audit-v1` test-split task names or item ids to the Hackbook, this skill, or its
  tests. TB4 examples come only from dev-split tasks or benchmark-wide items.
  `validate_no_tb4_test_leak` and `research/hackbook/build.py --check` enforce this.
- Do not tune signals on test-split tasks. Calibrate on dev only.
- Catalog edits go through `research/hackbook/build.py` (regenerates `schema.json` and
  `summary.json`); keep `--check` passing.
- Every `dev` finding of `tb4-audit-v1` must be cited by at least one entry's `examples`
  (`build.py --check` fails otherwise). Text that the seeker page prints (`title`, `mechanism`,
  `exploit_template`) stays generic: no task name, file name or number from a finding; that belongs
  in `examples`, which the page never shows.
