# Experiments

RUNNER-owned. Specs are the unit of submission; this journal is the
human-readable thread. Generated jobs stay in the worktree `runs/` directory
and are not committed.

| Path | What it is |
|---|---|
| `PROGRAM.json` | Versioned machine-readable ledger (`validate_program.py`) |
| `STATUS.md` | Human page: RECENT / RUNNING NOW / NEXT / TASK DECISIONS |
| `JOURNAL.md` | Running scientific thread: what / why / status / results / links |
| `specs/` | One study per directory; JSON files are `ExperimentSpec` documents |
| `preambles/` | Extra-instruction files for studies the runner cannot yet express |
| `baselines/` | Free oracle/nop matrices (`evallab matrix` only) |
| `local-controls.json` | Original event-summary oracle/nop matrix (kept; tests load it) |
| `repaired-data-sft-v2/` | Evidence-bound clean/token-matched/correction data admission and named-arm task-paired analysis |

Validate the current ledger and its known-bad regression fixtures with:

```bash
uv run python research/experiments/validate_program.py
uv run pytest -q research/experiments/tests/test_validate_program.py
```

Standing policy that admits work, copied from `policy/standing-approvals.yaml`
and not stretched:

- `local-controls` — any `oracle` / `nop` spec
- `canary` — `task` matches `canary/*`, agent `codex` or `claude-code`, attempts ≤ 3
- `researcher-followups` — `task` matches `registered/*`, attempts ≤ 5, and
  `requires` includes `schema_valid`, `dedup_pass`, `calibrated_judges_only`

Nothing in this checkout is registered. Using `registered/*` or putting a
non-member under `canary/` would be a policy stretch; those questions go to
Peter in the handoff.

Harbor 0.21 accepts `--extra-instruction-path`. `evallab.runner.build_command`
does not forward it, and `ExperimentSpec` forbids unknown fields. Preamble A/B
is designed here and is not executable through the queue until BUILDER adds
that field.

## Matrix solution controls

`evallab matrix <matrix.json>` accepts a per-run `solution` path relative to
the repository root, only for nonbillable `oracle` runs. For example:

```json
{"name": "task-mutant", "agent": "oracle", "expect_reward": 0.0,
 "solution": "library/tasks/task/controls/mutant.sh"}
```

The selected script replaces `solution/solve.sh` in a temporary task snapshot;
the source task is not modified. Declared task `steps` are unsupported by this
override contract and are refused before execution or script provenance is
published, including steps that would otherwise fall back to the root solution.
There is no implied per-step script mapping.

Oracle qualification requires completed agent-execution timing and the retained
`agent/oracle.txt` log (which may be empty). Harbor writes `agent/exit-code.txt`
on nonzero script exit, without necessarily failing the Harbor process. That
failure, unreadable exit evidence, missing execution artifacts, trial exceptions,
and missing or non-finite rewards are infrastructure outcomes with no qualifying
rewards, never successful negative controls.

This behavior was inspected in both the installed Harbor **0.21.0** source and
the PR's `uv.lock`-pinned Harbor **0.22.0** wheel (SHA-256
`4c4c6571b3d160ed0cb45b82918136751fb08e7b8596412723ac00dde12eeabb`).
Both prioritize a declared step's solution directory and write the exit marker
only on failure. Source inspection is not a Docker-backed control run or a
claim that the installed CLI has been upgraded.

The `.executor/<matrix_id>.matrix.json` receipt retains completed job rows and
their script/staged-task digests. A refused later invocation cannot replace a
successful row or its receipt bytes; restore the original script and use
`--reuse-existing` to validate the retained job without dispatching it again.
Each invocation's per-run outcome, including refusals, is appended separately to
`.executor/<matrix_id>.matrix.invocations.jsonl`. Reuse still checks the actual
job evidence and any executor failure state; a preserved receipt does not turn
failed or incomplete execution into a pass.

## Repaired-data SFT v2

`repaired-data-sft-v2/build.py` consumes hash-bound native result/ATIF inventories,
the frozen all33 held-out authority, hardened-v6 and its exact repaired-child
lineage, and retained native replay/control evidence. Run from the locked root
QA environment with the isolated SFT client's pinned tokenizer dependencies:

```bash
HF_HUB_OFFLINE=1 uv run --locked \
  --with transformers==5.12.1 --with modal==1.5.5 \
  --with huggingface-hub==1.9.2 --with jinja2==3.1.6 \
  python research/experiments/repaired-data-sft-v2/build.py \
  --inputs inputs.json --tokenizer-dir /absolute/pinned/local/snapshot \
  --epochs 1 --require-d --out /absolute/new/output
```

Its module help documents `repaired-data-sft-v2.inputs/1`. B requires at least51
distinct, nonzero-loss rows passing both exact controlled graders. C is a seeded
randomized **feasible subset**, not a uniform sample; supervised assistant-token
exposure matches exactly over common complete epochs, or admission refuses.
D targets only genuine new corrected assistant turns after source-preserving
failed-test feedback and a native repaired-grader 0→1 transition. All33 frozen
parents and resolved descendants are excluded from every training population.
`--plan` writes prospective accounting only; it never exports trainable arms.
No truncation, row padding, B-as-C fallback, or fabricated D is permitted.

`repaired-data-sft-v2/analyze.py --plan PLAN.json --out REPORT.json` validates each
arm's complete authorized raw task/attempt map before applying the requested
cohort and selected-attempt view. The `repaired-data-sft.analysis-plan/v2` contract
is documented in its module help. Primary comparisons use measured common
tasks, exact sign fractions and paired-**task** bootstrap difference CIs; full
schedule rates and pin regrades remain separately labelled. Missing/error rows
are unmeasured, never imputed failures. Existing report files are not overwritten.

`m1-cohort.json` freezes the accepted v1 report's26-task attempt1 comparison
cohort for new Base/A cells. This cohort is conditioned on prior C measurability;
it is not a new unbiased all33-task population, nor permission to narrow the
all33 training exclusion or the original B/C raw schedules.
