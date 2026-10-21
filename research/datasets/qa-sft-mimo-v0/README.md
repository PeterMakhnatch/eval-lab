# qa-sft-mimo-v0 — SFT data for task QA (MiMo)

Leak-free supervised examples teaching a cheap model to QA benchmark tasks:
given a task package reference, emit `qa.finding/v1` findings (or none).

## Contents

- `train.jsonl` / `dev.jsonl` / `test.jsonl`: one JSON object per example,
  split by `task_id` (sha256 bucket, ~80/10/10; no task in two splits).
- `build_dataset.py`: stdlib producer from published sources only ($0).
- `manifest.json`: counts, split sizes, source digests, exclusion check.

Example shape: `input` is the QA agent's task view **by digest only**
(package/harbor/verifier digests + record id; fetch exact bytes via the
task-variant custody contract). No hidden tests, solutions, reference
patches or trajectories are inlined — this file carries no answer-key
material. `target.findings` are `qa.finding/v1` objects
(`src/evallab/qa/models.py`, `load_findings_dir`-compatible).

## Labels

- **Positives** (atlas weak tasks): target findings converted from the
  mimo-weak-tests-v2.1 atlas witnesses in mimo-defect-map-v1 — the executed
  wrong program, the instruction requirement it violates, exact package.
  Label provenance: source-supported historical adjudication, not fresh
  replay; the atlas is a positive-only catalog, so absence elsewhere is
  not a negative.
- **Negatives** (executed-clean pool): target is an empty finding list,
  justified by executed evidence recorded per example (mimo-clean-v7
  strict repeat-3 controls reference `[1,1,1]` / nop `[0,0,0]` on the
  exact package, no open supported finding; repaired rows source-qualified).

## Hard rules

- **No tb4-audit-v1 data** (tasks, findings or trajectories) is included;
  the producer never reads that key and fails if any emitted row
  references it. Check recorded in `manifest.json`.
- The frozen 33 held-out MiMo task IDs stay excluded from training under
  every package version; do not train on examples for those IDs (they are
  retained for dev/test measurement only where they fall).
- This release grants no training authorization by itself; the cheap-model
  lane's admission, leakage and budget checks still apply.

## Licence and limits

Code/dataset lineage: FineEnvs/MiMo-V2.6-RL-harbor-code (Apache-2.0 /
MIT notices as in mimo-weak-tests-v2.1). Positives are retrospective and
source-overlapping with the hackbook catalog seed; negatives certify only
the stated executed controls, not semantic cleanliness. Sizes and split
counts are in `manifest.json`.
