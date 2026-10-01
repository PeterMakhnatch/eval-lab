# ovn-sft-v0: stock, LoRA-SFT and GEPA on held-out Python tasks

The overnight experiment of 2026-10-01 (plan: `research-context/inbox/sft-overnight-20260918/OVERNIGHT-2026-10-01.md`; cards HAR-126 to HAR-135). This directory holds the frozen data inputs and integrated results.

Start with [RESULTS.md](RESULTS.md), the [one-page validity verdict](VALIDITY.md),
or the [G5 execution record](G5-RUN.md). The primary analysis is reproducible
with `uv run python research/experiments/ovn-sft-v0/har133_analysis.py`.

## G1: the frozen eval set

[`eval_tasks.csv`](eval_tasks.csv)
**sha256 `3b997fdcff048061fd8a05d446948d4d6425bf670eb0d6710989c50dcd0a9219`** (v2)

- Frozen before G2 and eval outcomes; unchanged through G5. Earlier dry-run sample selection at 04:08Z preceded the first eval commit at 04:09:33Z. Those samples were outside both cohorts and the dry-run adapter was deleted; [RESULTS section 8](RESULTS.md#8-dated-deviations-limitations-and-interpretation) records the chronology caveat rather than claiming that all selection followed the freeze.
- **v2 supersedes v1** (`504b913a…`, PR #597), which was withdrawn before G2 started. Cdx 3's HAR-133 audit found that v1's 002209 is a pandas task: its tests patch `pandas/tests/io/test_parquet.py` and its instruction asks for `pandas.to_parquet`. The census filed it under fastparquet, and `project_modules` reports only fastparquet, which is a dependency. Pandas has training-split tasks (002208 and others), so v1 broke repository disjointness.
  - v2 adds check 3 below and re-runs the same selection.
  - The only change is 002209 → 001833 (safedelete, the next lightest qualifying task).
- Columns:

  | column | meaning |
  |---|---|
  | `task` | the task |
  | `digest` | the package to run (the ledger's `run_digest`) |
  | `run` | `original`, `leak-closed` or `repair` |
  | `repo` | census project key |
  | `image_mib` | image size |

- Built by [`select_eval.py`](select_eval.py) from the Python task ledger ([`../python-task-ledger/`](../python-task-ledger/), merged in #592):
  - 20 `usable` held-out tasks, lightest image first (394–2949 MiB);
  - one per repository;
  - HAR-116's 15 tasks excluded.
- Packages:
  - 17 run the original.
  - 1 runs a validated repair (002017: `env-keep-build-outputs@1`).
  - 2 run a leak-closed variant (001695, 001809) whose record is still `candidate`. Its nop evidence is the original's, because the PyPI blocklist acts only through the agent harness.
- Labels: none of the 20 has a hand label. The HAR-112 checker labels each one `sound` or `suspect`, never `broken`.

### Disjointness from training

Checked against all 1,047 training-split tasks of the census split (the sealed split agrees on all 1,180, per Cdx 3). The HAR-120 proposal and any G3 sample must also be training-split tasks; HAR-110's local "held-out" names, such as 001181, don't count, and G3 rejects every globally held-out trial. A held-out task is skipped if any of these holds:

1. **Same repository.** Its repository key matches a training task's. Keys are the census `project_key`, compared by last path segment, ignoring case and treating `-` and `_` as the same. Matching the raw `project_key` or the `split_group` also counts.
2. **Shared module.** Its project modules share anything with any training task's modules, repository keys or test-file packages, or vice versa. Project modules come from `evallab.hidden_patch.project_modules`: what the hidden tests import, plus the packages the test files live in.
3. **Tests live in a training package.** A hidden test patch file's top-level directory is a training task's module, repository key or test-file package. This catches a task whose tests import only a dependency, such as 002209: `pandas/tests/…`. Generic directories that name no package (`tests`, `src`, `usercase-test-coderl` and similar, listed in `GENERIC_ROOTS`) don't count.
4. **Used by a training task.** A training task's instruction or hidden tests use one of its modules or test packages (`import m`, `from m`, or `m.name`). This catches a same-repository training task whose census repository is unknown:
   - 000145 and 000147 (pytorch-lightning) were skipped because of 000141's `pytorch_lightning.Trainer`;
   - 000337 (albumentations) was skipped because of 000338/000339's `import albumentations`.
5. **Disjointness can't be shown.** Its repository is unknown (`project_key` is its own task id), or its tests name no project module.
6. **Near-duplicate instruction.** Word 5-shingle Jaccard against any training instruction is at or above 0.3.

**Residual check.** I scanned each eval instruction and test patch for any training-identity name used as `import X`, `from X` or `X.name`. The only hits are dependencies the eval projects use (numpy, pandas, boto3, botocore) and generic words (`tests`, `cli`, `data`). None is the eval task's own package.

[`contamination.json`](contamination.json) records why each of the 33 skipped held-out tasks was skipped. It also records each eval task's modules and its nearest training instruction. The highest eval-vs-train Jaccard is 0.0215, so no instruction comes near the threshold.

Reproduce from the checkout root with `uv run python research/experiments/ovn-sft-v0/select_eval.py`. It needs the code snapshot at `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/` and rewrites the same bytes.
