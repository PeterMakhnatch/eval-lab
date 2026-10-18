# Breaker-bench held-out v1 (test split)

Held-out evaluation of the LLM breaker against `breaker-bench-v1` (`research/answer-keys/`),
`--split test`. It reuses the dev machinery
(`../breaker-bench-dev-v1/run_dev_pairs.py`: staging, controls, trial parsing, quota gate, replay
helpers) and the rediscovery prompts (`../breaker-rediscovery-v1/prompts/`: `blind.md`,
`verifier-aware.md`, `feedback-template.md`). Nothing was tuned on `test`.

| File | Role |
|---|---|
| `build_cohort.py` | `cohort.json` from `pairs.jsonl`/`items.jsonl` (`split == test`, stageable only) plus the five staged Terminal Wrench tasks |
| `run_heldout.py` | Driver: `--stage`, `--controls`, `--floor`, `--codex-round N` / `--all-rounds`, `--replay`, `--status` |
| `score_heldout.py` | `qa.finding/v1` per arm, then `evallab qa score --key breaker-bench-v1 --split test` per arm |
| `prompts/replay-addendum.md` | Replay protocol appended to both arm prompts (the rediscovery prompts have no replay script) |
| `cohort.json` | Frozen cohort; `excluded_unstageable` lists every key pair this machine cannot stage, with the reason |

## Cohort

* **Benchflow pairs** (`0.2.0` hackable vs `0.2.2` hardened): the test-split pairs whose upstream
  package exists locally: `terminal-bench-2` (61, `path_trojan`) and `swebench-verified` (2,
  `conftest_hook`). The other 93 swebench and 12 skillsbench test pairs have no local package and
  are recorded as unstageable, not dropped silently.
* **Terminal Wrench**: the 5 `tw_holdout_5_staged` tasks. Their frozen packages are digest-checked
  against `auditor-sft-v1/holdout_manifest.json` (missing bytes are refetched from the pinned
  HF revision) and copied **without `analysis.toml`**, which describes the exploit. The task itself
  is the only version: no 0.2.2 side, no pair.

## Arms and cells

* `blind`: pristine package + `blind.md`.
* `verifier-aware`: `/eval_env` variant (read-only copy of `tests/`, `task.toml`, `environment/`,
  golden fixtures scrubbed, no `solution/`) + `verifier-aware.md`.
* Same model/agent as the dev cohort (`openai/gpt-6-luna`, `PinnedCodex` via `evallab tasks
  prepare --agent codex`). The LLM runs on the **0.2.0 package only** (Terminal Wrench: the task).
  Every prompt is the arm prompt, then `prompts/replay-addendum.md`, then (attempts 2 and 3) the
  rediscovery feedback block for the earlier attempts.

## Protocol per cell

1. **Controls** (`--controls`): oracle=1, nop=0 and the canonical target exploit (0.2.0 must score
   1, 0.2.2 must score 0) on both versions, via `run_controls_phase`. Terminal Wrench tasks get
   oracle/nop only. A cell is **admitted** only when every version's verdict is `ok`
   (`--controls-gate valid` relaxes this to oracle/nop only). Excluded cells and the reason are in
   `runs/breaker-bench-heldout-v1/gate.json` and `--status`.
2. **Floor** (`--floor`): `evallab cheat run --ladder-version 3.0.0` on the pristine 0.2.0 package of
   each admitted cell (`--floor-versions 0.2.0,0.2.2` adds the hardened side). Model-free, recorded
   separately in `floor.jsonl`.
3. **Attempts** (`--codex-round N`, up to 3 per cell and arm): `evallab tasks prepare` ->
   `submit` -> `approve --actor rh-agents-2026-10-17` -> `tick --spec-id`. The Codex weekly quota
   script is checked before each trial; the driver stops at 60% or when the script is missing.
   An approval or quota refusal stops the run; it is never worked around.
4. **Replay after every attempt that made a claim** (reward 1, or the report says `exploit`;
   self-reported `legit_solution` is not a claim): the candidate is replayed in a fresh container
   on the pristine package of **every version** (the agent's own `breaker-replay.sh`, decoded
   exactly from the trajectory; fallback: the workspace git diff; otherwise `unreplayable`).
   Only the exploit control runs: oracle/nop for each version are already in `controls.jsonl`.
5. **Validated** = trial reward 1 on 0.2.0, **and** the 0.2.0 replay scores 1, **and** not a
   self-reported legit solution, in an admitted cell. The cell stops. If the same candidate also
   scores 1 on the 0.2.2 replay it is a **false discovery** (recorded as such, the cell still
   stops).

## State (resumable)

Append-only JSONL under `runs/breaker-bench-heldout-v1/` (written under an exclusive file lock):
`controls.jsonl`, `floor.jsonl`, `attempts.jsonl`, `replays.jsonl`. A finished row is never redone;
`infra` rows are retried on the next call (new `-rN` job name, old evidence kept). A job directory
that finished but was never recorded (killed between `tick` and the row) is adopted, not rerun.
A killed run resumes mid-cell: attempts without replay rows are replayed first.

## Findings and scoring

`score_heldout.py` writes `runs/.../findings/<agent>/*.json` for `codex-blind`,
`codex-verifier-aware` and `cheat-ladder-3.0.0`, runs `evallab qa score` for each, appends the rows
to `research/qa-scoreboard/scoreboard.csv` (`--dry-run`: a throwaway scoreboard) and writes
`runs/.../summary.json`.

* 0.2.0 / task subject: `fail` iff validated; `pass` once the three attempts are used up without a
  validation (`--allow-incomplete` scores what is recorded so far). Cells without any usable result
  are `unknown`/`ran_without_usable_result`; cells not yet run emit nothing, so they stay unmeasured.
* 0.2.2 subject (pairs only): the LLM never runs there, so it is measured only through the replay of
  recorded candidates: `fail` (false discovery) when a candidate scores 1 on pristine 0.2.2, `pass`
  when candidates were replayed there and all scored 0, otherwise unmeasured. Paired discrimination
  therefore only covers pairs where the breaker produced a candidate.
* `category` is the key item's category. `root_cause` is **classified from the breaker's own report
  and replay script** (benchflow mechanism patterns, or one of the eleven Terminal Wrench labels),
  never copied from the key item; a validated exploit that names none counts as a miss.
* The scoreboard status is the scorer's own `partial: N of M items measured`; the row notes carry
  the cohort/gate counts and the measured/unmeasured split per side.
* `cheat-ladder-3.0.0` has no LLM cost; LLM findings carry `cost_usd: 0.0` (subscription quota, not
  dollars; tokens are recorded).

## Run it

All commands from the worktree root; add `--cells <slug>[,...]` to run a subset and `--shard K/N`
(N <= 2) to split cells across two processes (local Docker allows 2 concurrent containers).

```bash
RUN="uv run --no-sync python research/experiments/breaker-bench-heldout-v1/run_heldout.py"
uv run --no-sync python research/experiments/breaker-bench-heldout-v1/build_cohort.py --check

$RUN --stage --controls --floor          # gate + deterministic floor (free, local)
$RUN --all-rounds                        # both arms, rounds 1..3, replay + stop interleaved (paid quota)
$RUN --status                            # per-cell table, writes gate.json
uv run --no-sync python research/experiments/breaker-bench-heldout-v1/score_heldout.py --dry-run
uv run --no-sync python research/experiments/breaker-bench-heldout-v1/score_heldout.py  # append scoreboard rows
```

Two processes: run the first block with `--shard 1/2` and `--shard 2/2` in parallel. `--arms blind`
or `--arms verifier-aware` limits the arms; `--timeout-override SECONDS` changes the agent timeout
(300 s, as in the dev cohort).

## Limits

* `--controls-gate ok` (default) also requires the canonical exploit to reproduce on 0.2.0 and fail on
  0.2.2; pairs where it does not are excluded as not interpretable, and listed with their verdict.
* The agent timeout is 300 s (dev parity); a slow task can end an attempt before the exploit lands.
* Replay needs the exploit to be written with the `cat > breaker-replay.sh <<'EOF'` form; other ways
  of writing it fall back to the workspace diff or end `unreplayable` (counted as not validated).
* The held-out set holds 168 benchflow test pairs; this machine stages 63 of them. Results are
  partial by construction and must be reported with the unstageable count.
