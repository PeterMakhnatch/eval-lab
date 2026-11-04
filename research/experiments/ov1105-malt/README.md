# MALT held-out detector transfer

## Scope

This experiment admits METR MALT as an evaluation-only DetBench source. It tests the frozen action judges against character and step length on the same runs. `PREREG.md` defines the evaluation before any detector reads these runs. `RESULT.md` records the observed coverage, scores, examples, and delivery receipts.

MALT contains selected natural behavior and prompted behavior. It is not a random sample of deployment behavior. Its labels describe attempts, not necessarily successful cheating. The 2 public repositories provide different representations of the same collection. They are not independent evaluation sets.

This is native MALT transfer with prior redistributed-source exposure, not a globally unread source test. The failure library already processes 572 MALT identities with `agent-text-regex/v1`. These include all 47 primary positives. This audit does not establish prior use by the frozen judges or L0 rules. Removing every previously processed identity leaves no primary positive class.

Complete-context conversion retains 15 of 47 eligible positives and 654 of 694 eligible negatives. It rejects 72 unresolved trajectories. All 669 admitted runs contain reasoning. Results describe this conversion subset, not complete MALT coverage.

## Source and labels

- `metr-evals/malt-public`, revision `6100006c2fd23d81e5afe2b1cc3bf3f3fff48df1`.
- `metr-evals/malt-transcripts-public`, revision `3521e8e22b6af12fea3249443e554b96b6fc4f49`.
- METR, *MALT: A Dataset of Natural and Prompted Behaviors That Threaten Eval Integrity*, Neev Parikh and Hjalmar Wijk, October 14, 2025: <https://metr.org/blog/2025-10-14-malt-dataset-of-natural-and-prompted-behaviors/>.

The public collection contains 7,179 runs, 169 tasks, and 19 model strings. It includes 6,134 unprompted runs and 1,045 prompted runs. METR manually reviews all non-normal runs and 694 normal runs. The other 5,353 normal labels do not establish reviewed negatives.

The primary cheating map uses reviewed `bypass_constraints` and `ignores_task_instructions` labels. A negative requires reviewed, exact `normal`. Both classes require an unprompted source. This gives 47 positive and 694 negative source candidates before conversion. Other behavior and unreviewed labels remain excluded. `label-map.json` is the executable mapping contract. The manifest retains exact raw-label incidence and combinations.

METR selects behaviors through anomalous scores, keywords, and some model-assisted classification. Employees and contractors perform the manual reviews. The release does not give a complete inter-reviewer agreement study. METR edits references to misbehavior instructions from reasoning with Claude Sonnet 4, including unprompted reasoning. Its named obfuscation configurations add further transformations. This experiment does not treat those variants as independent runs.

Both source licenses are MIT, Copyright (c) 2025 METR. Retain the copyright and permission notices in copies or substantial portions. The source cache keeps the exact licenses. Existing HF gate access permits the downloads. This experiment applies stricter local limits: no training, tuning, rehosting, or transcript publication. Hosted judge requests use only the authorized evaluation routes. No vendor-project upload is allowed.

## Reproduction

Sources and converted views remain outside Git. The manifest pins revisions, file hashes, bytes, row counts, and skipped files. Cache roots follow `~/.cache/eval-lab/source-intake/<org>__<name>/<revision>/`.

Convert the pinned source files:

```sh
uv run python research/experiments/ov1105-malt/adapter.py \
  --source-manifest research/experiments/ov1105-malt/source-manifest.json \
  --output /absolute/path/outside/git/malt-detbench
```

The adapter writes a runs table, label-free full/actions-only views, coverage, exclusions, and conversion failures. It does not run a detector. Do not overwrite existing evidence directories.

Before scoring, commit and push the adapter, manifest, label map, and pre-registration. Record that commit in an external receipt with `commit`, `remote_ref`, `adapter`, and `label_map`. `evaluate.py` checks that receipt against the remote branch and committed bytes. Supply the exact fixed sample described in `PREREG.md`.

```sh
uv run python research/experiments/ov1105-malt/evaluate.py \
  --runs /absolute/path/outside/git/malt-detbench/runs.jsonl \
  --sample /absolute/path/outside/git/malt-sample.json \
  --output /absolute/path/outside/git/malt-scores \
  --prereg /absolute/path/outside/git/prereg-receipt.json \
  --phase static
```

Use `--phase judges` for the fixed sample, then `--phase metrics`. Keep all input files and runner bytes fixed across the phases. The runner refuses a changed binding. It reports explicit abstentions for failed or quota-stopped rows. It does not replace the frozen judge route.

Before every judge batch, the runner records `omp usage`. It stops both judge routes at Codex 7-day usage of 62% or Gemini Weekly usage of 78%. Calls use `omp auth-gateway stdio` and prepaid subscriptions only. The paid-spend limit is $0.

The production line accepts DEV inputs only. This experiment does not change a held-out run to DEV or send it to training. An unavailable line decision remains an abstention.

## Files

| File | Contract |
|---|---|
| `adapter.py` | Source conversion, label mapping, identity checks, and coverage |
| `label-map.json` | Frozen primary labels and exclusions |
| `source-manifest.json` | Immutable source revisions and file receipts |
| `frozen-code.json` | Original detector/scorer identities |
| `evaluate.py` | Frozen-threshold scoring, gateway execution, and aggregate statistics |
| `PREREG.md` | Fixed evaluation and sample design |
| `RESULT.md` | Coverage, matched-row score tables, examples, and receipts |

Raw transcripts, rendered views, model replies, and per-run detector outputs do not belong in this repository.
