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
D consumes hash-bound EnvCheck `reset-context-feedback` requests: the original
task prompt, actual incomplete patch, and an independently recomputed
`source-free-outcome-projection/v1`. Raw failing stdout is retained evidence,
not model input; historical conversations are not restored. A genuine completed
same-model correction must change the patch and produce a native repaired-grader
replay 0→1 transition on the same exact qualified grader with valid controls.
Only the genuinely new final main-agent assistant target carries loss (`last`).
Native original and repaired outcomes remain separately labelled; this is
mechanical repaired-grader evidence, not independently established semantic truth.
Original-positive B/C admission rejects every non-null native exception even
when the native result also records reward1. All33 frozen parents and resolved
descendants are excluded from every training population.
`--plan` writes prospective accounting only; it never exports trainable arms.
No truncation, row padding, B-as-C fallback, or fabricated D is permitted.

`counterfactual.grader_controls(..., matrix_runner=...)` routes both fresh
nop/reference executions through the caller's native matrix front door, just as
subject replay does. Use a new owned output directory for required fresh same-run
controls: supplying a runner does not bypass existing control-cache validation,
authorize spend, or turn cached controls into fresh executions.

Spec-driven MiMo oracle replays also require an effective egress lock (explicit
`egress_lock=true` on Modal) and a registered variant for the exact
solution-overridden snapshot. Derive these inputs with `derive_task` and
`qa-trace-patch-replay@1`, retaining the repaired physical grader as parent and
unchanged verifier bytes. The candidate records describe replay inputs only:
they are not validated tasks, measured controls/outcomes, or SFT rows. Registering
them does not replace fresh same-run controls or change model-run lineage policy.

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

### Same33 hardening rounds: native data-effect consumer

`heldout-v7/rounds.py` consumes the existing canonical pass inventory and the
published window3 native snapshot. It is a model-free, $0 Docker calibration
consumer, not a release, scoring, training, or paid-dispatch authority.
`campaign.admit` and the staged paid grants are unchanged. Original-v7 baseline
admission is rechecked through `campaign.admit`; a candidate manifest alone
cannot authorize a round.

The round assembler reuses `mimo-training-20261016/freeze.py`. For round N it
requires the existing `envcheck.repairs-v7/v1` handoff named exactly
`repairs-round-N.json`, with a byte-bound `parent_release`, a nonempty entirely
qualified selection, and equal complete/required/selected counts. If a producer
includes a `round_number` annotation, it must be the matching integer, not a
boolean. No new annotation is required on the producer schema. The predecessor
must be independently accepted and released: v7 for N=1, v7.(N-1) thereafter.
The assembled immutable `heldout-v7.N` keeps the exact frozen33 membership, order,
training exclusions, selected package/Harbor/verifier identities, and complete
VariantRecord ancestry. Unchanged selections remain from that predecessor;
earlier repairs are not reverted to v6. The new candidate closes all scoring,
training and dispatch permission flags and resets independent acceptance.
Existing output paths are never overwritten.

Use the owned worktree's already-installed native Python, and choose a new
output directory for each attempt. The current real input locators are:

```sh
PY=.venv/bin/python
BRIDGE=/Users/petermakhnatch/Developer/research-context/bridge
OUT=runs/mimo-m1-data-curve-20261019
$PY research/experiments/heldout-v7/regrade.py inventory \
  --inputs "$BRIDGE/2026-10-17-v7/classifiers-v7-source-inputs.json" \
  --out "$OUT/source-inventory.json"
$PY research/experiments/heldout-v7/rounds.py inventory \
  --canonical-inventory "$OUT/source-inventory.json" \
  --canonical-labels "$BRIDGE/2026-10-17-v7/regrade-v7.json" \
  --fresh-result "$BRIDGE/2026-10-19-night/trainer-window3-native-result.json" \
  --out "$OUT/round-inventory-bound.json"
$PY research/experiments/heldout-v7/rounds.py report \
  --inventory "$OUT/round-inventory-bound.json" \
  --out "$OUT/readiness-report-nullable-pairs.json"
```

Before a real round, set `ROUND`, `FROZEN_SOURCE_DIR`, `ACCEPTED_PREDECESSOR`,
and `QUALIFIED_REPAIRS` to the actual handed-off round and accepted immutable
inputs. The first round's frozen source directory is the accepted original-v7
`mimo-training-20261016` directory; its five frozen source hashes must still
match. Do not substitute the pending night v7.1 candidate for a released
predecessor, rename an old repair file to invent a round, or rerun only changed
tasks.

```sh
$PY research/experiments/heldout-v7/rounds.py assemble \
  --frozen-source-dir "$FROZEN_SOURCE_DIR" --predecessor "$ACCEPTED_PREDECESSOR" \
  --repairs "$QUALIFIED_REPAIRS" --round-number "$ROUND" \
  --out "$OUT/heldout-v7.$ROUND.json"
$PY research/experiments/heldout-v7/rounds.py plan \
  --inventory "$OUT/round-inventory-bound.json" \
  --manifest "$OUT/heldout-v7.$ROUND.json" --out "$OUT/round-$ROUND-plan.json"
EVALLAB_DOCKER_LOCK_SLOTS=1 $PY research/experiments/heldout-v7/rounds.py run \
  --plan "$OUT/round-$ROUND-plan.json" --work "$OUT/round-$ROUND-native" \
  --out "$OUT/round-$ROUND-report.json"
```

For N>1, pass `--previous-report` to `plan` with the previous target-round
report, whose candidate bytes must be bound by this round's independently
accepted predecessor. Without that measured predecessor report, before/after
pairs remain unmeasured rather than borrowing original-v7 grades. `report` can
recover retained partial evidence with the exact `--plan` and `--work`:

```sh
$PY research/experiments/heldout-v7/rounds.py report \
  --inventory "$OUT/round-inventory-bound.json" --plan "$OUT/round-$ROUND-plan.json" \
  --work "$OUT/round-$ROUND-native" --out "$OUT/round-$ROUND-recovered.json"
$PY research/experiments/heldout-v7/rounds.py curve \
  --report "$OUT/round-1-report.json" --report "$OUT/round-2-report.json" \
  --out "$OUT/round-1-2-curve.json"
```

The run visits every physical source pass, including retained graders and
different UUIDs with identical patches. It uses `cf.replay` and the existing
matrix/Executor front door and shared Docker FIFO, one serial oracle job at a
time (primary plus separate verifier: at most two containers). No model call,
alternate scheduler/store, released scoring admission, or inherited grant is
introduced. Every cell gets an immutable attempt and actual native observation,
or an explicit missing/error disposition. A valid boolean grade requires a
fresh native dispatch, unchanged source and exact target identities, raw
receipt/config/lock/result custody, successful process/oracle proof and positive
executed cases. Zero-case, timeout, infrastructure and failed-shell results
remain unmeasured, never reward0. Failed attempts are retained; retries use new
work/output paths rather than overwriting them.

Reports keep canonical70 and fresh window3 separate, including the full
plan/census/labels/model-configuration snapshot bindings. No task swap maps an
out-of-scope fresh program onto a heldout grader. `paired` and `unpaired` expose
the flip denominator; flip/retention counts are null if no paired grades exist.
The optional `inventory --gold-labels` input has schema
`mimo.hardening-gold/v1` and labels keyed by `source_uid` with a byte-bound
`independent_adjudication`. Each adjudication must have schema
`mimo.independent-correct/v1`, `independent:true`, `verdict:correct`, a named
reviewer, and the exact source UUID pair, task ID and `diff_sha256`. Producer
pass labels or a correct alternate for another program are not gold for these
submissions. Gold-correct rejection rates are null at denominator0.

Exercised local evidence is retained under
`runs/mimo-m1-data-curve-20261019/`:

| Artifact | SHA-256 | Observed coverage |
| --- | --- | --- |
| `source-inventory.json` | `d8160894f8201d6d06b9a067b79b98cd14bc21c7d2b7bd435e46d629b45db01c` | All-arm native inventory; original canonical70 preserved |
| `round-inventory-bound.json` | `68554c4af3aab1c2171e533f28b2a2fd4b7759677e61854f7668cd4ae70093d0` | 70 canonical + all3 published fresh pass candidates; 73 physical cells |
| `readiness-report-nullable-pairs.json` | `4bf2bfccae40889c5291ae732e6541d3675d7acdfd4f523b0e2cb30a2ac906f3` | 0 target-round measurements, 73 missing dispositions, 0 pairs; flips null |

The original-v7 differential labels are 64 real/6 wrong, not independent
semantic gold. Canonical sources retain16 native exceptions. Window3 retains
all76 planned dispositions and8 native endpoints, with3 published reward1
candidates, all carrying `AgentTimeoutError` on task000089 outside frozen33.
All3 remain explicit unsupported-scope/source-error rows; native-clean passes
and collector-admitted rows are0. The gold-correct denominator is0.
No `repairs-round-N.json` was available for the exercised assembly; that command
failed closed with the exact missing path. Planning the pending night v7.1 and
turning a readiness report into a measured curve also failed closed. No new
native target-round job, per-round flip result, or independent PASS is claimed.
The remaining numerical prerequisites are a genuinely qualified round handoff,
its accepted predecessor (night v7.1 is still pending), and heldout-compatible
fresh programs rather than silently substituting tasks.

Applied deep-library techniques are fixed immutable accumulated rounds (A1),
separate legitimate-route retention rather than task-only gold transfer
(A2/A3), paired retrospective old-program/new-grader measurements rather than
prospective model-behavior claims (A6), and reference/process/executed-case
anti-fake-hardness gates (A8). The evidence does not reproduce paper scores or
certify semantic correctness/honesty. Deterministic boundaries are in
`test_mimo_training_split.py`, `test_heldout_v7_native.py` and
`test_heldout_v7_rounds.py`; the integration owner runs those tests and normal
repository gates after handoff, not concurrently with worker edits.
