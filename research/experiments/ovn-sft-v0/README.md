# ovn-sft-v0: stock distill vs LoRA-SFT distill on held-out Python tasks

The overnight experiment of 2026-10-01 (plan: `research-context/inbox/sft-overnight-20260918/OVERNIGHT-2026-10-01.md`; cards HAR-126 to HAR-134). This directory holds the data side (HAR-127).

## G1: the frozen eval set

[`eval_tasks.csv`](eval_tasks.csv)
**sha256 `504b913a7c0fbd3bb523580db1c687014a22ee23bb3209990f5b6f3a47ea3967`**

- Frozen 2026-10-01, before any training data was selected. The list never changes after this.
- Columns:

  | column | meaning |
  |---|---|
  | `task` | the task |
  | `digest` | the package to run (the ledger's `run_digest`) |
  | `run` | `original`, `leak-closed` or `repair` |
  | `repo` | census project key |
  | `image_mib` | image size |

- Built by [`select_eval.py`](select_eval.py) from the Python task ledger ([`../python-task-ledger/`](../python-task-ledger/), merged in #592):
  - 20 `usable` held-out tasks, lightest image first (394–2944 MiB);
  - one per repository;
  - HAR-116's 15 tasks excluded.
- Packages: 16 run the original. 2 run a validated repair (002017, 002209: `env-keep-build-outputs@1`). 2 run a leak-closed variant (001695, 001809) whose record is still `candidate`; its nop evidence is the original's, because the PyPI blocklist acts only through the agent harness.
- Labels: none of the 20 has a hand label. The HAR-112 checker labels each one `sound` or `suspect`, never `broken`.

### Disjointness from training

Checked against all 1,047 training-split tasks. Every training candidate is a training-split task, so this covers the HAR-120 proposal and the SFT passes from HAR-104/110/116/120. A held-out task is skipped if any of these holds:

1. **Same repository.** Its repository key matches a training task's. Keys are the census `project_key`, compared by last path segment, ignoring case and treating `-` and `_` as the same. Matching the raw `project_key` or the `split_group` also counts.
2. **Shared module.** Its project modules share anything with any training task's project modules, or with a training task's repository key. Project modules come from `evallab.hidden_patch.project_modules`: what the hidden tests import, plus the packages the test files live in.
3. **Used by a training task.** A training task's instruction or hidden tests use one of its modules (`import m`, `from m`, or `m.name`). This catches a same-repository training task whose census repository is unknown:
   - 000145 and 000147 (pytorch-lightning) were skipped because of 000141's `pytorch_lightning.Trainer`;
   - 000337 (albumentations) was skipped because of 000338/000339's `import albumentations`.
4. **Disjointness can't be shown.** Its repository is unknown (`project_key` is its own task id), or its tests name no project module.
5. **Near-duplicate instruction.** Word 5-shingle Jaccard against any training instruction is at or above 0.3.

[`contamination.json`](contamination.json) records why each of the 31 skipped held-out tasks was skipped. It also records each eval task's modules and its nearest training instruction. The highest eval-vs-train Jaccard is 0.0215, so no instruction comes near the threshold.

Reproduce from the checkout root with `uv run python research/experiments/ovn-sft-v0/select_eval.py`. It needs the code snapshot at `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/` and rewrites the same bytes.
