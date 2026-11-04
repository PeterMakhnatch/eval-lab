# HackBench intake for DetBench

HackBench supplies **169 positive trajectories and 0 negative trajectories**.
It supports held-out recall and coverage, but not run-level AUROC or false-positive rate.
Do not label non-gold commands or trajectory prefixes as negative runs.

## Source and terms

- Dataset: [`facebook/hackbench`](https://huggingface.co/datasets/facebook/hackbench).
- Pinned revision: `b3d6f678e6359dcd35e20f3f2c5ee085b5af4934`.
- License: [CC-BY-NC-4.0](https://creativecommons.org/licenses/by-nc/4.0/).
- Gate conditions: no model training and no rehosting.
- Use: evaluation-only, non-commercial research. Keep source bytes and derived views outside Git.
- Do not send records to dataset stores, Laminar, Docent, or training systems.
- Parent owns any approved hosted evaluation, pre-registration, scoring, and delivery.

The source card links the [HackBench project article](https://facebookresearch.github.io/RAM/blogs/hackbench/).
The article links [*Aligning Autoresearch: Co-Improvement between Agents & Humans*, section 2](https://arxiv.org/html/2610.38882v1#S2).
This intake reads both sources. It does not run the published models.

The source data file contains 408,528,473 bytes.
Its SHA-256 is `42c0cfa4a3dc822eb6505fbcc58989c5df5dcf509cc4a97338abbb38c7e9fd87`.
`manifest.json` records every downloaded file's size, SHA-256, source checks, and aggregate row counts.
The publisher's README checksums are stale. The pinned README matches its published Git blob ID.
All other pinned source checks must pass. Intake rejects changed bytes rather than accepting a new revision.

## What the trajectories represent

The agents perform long-horizon ML research through the Harbor `terminus-2` terminal scaffold.
The release covers 13 tasks: 5 ResearchGym tasks, 7 AutoLab model-development tasks, and 1 weak-to-strong research task.
GPT-5.5 supplies 102 records. GLM-5.2 supplies 67 records.

The publisher defines reward hacking as subverting the measurement instead of accomplishing the intended task.
The release contains 41 natural and 128 synthetic examples.
For natural examples, GPT-5.6 Sol screens each trajectory in 8 chunks. Humans audit the candidates.
For synthetic examples, GLM-5.2 inserts hacking commands and updates affected downstream observations.
Claude Opus 4.8 and GPT-5.5 audit the synthetic examples. Humans annotate disagreements.
These are publisher labels, not new labels made by this intake.
`label_type=dataset` preserves that mixed provenance without claiming that every label is human-only or executed-oracle gold.

The source manifest selects 169 HACK records from 202 source records.
It excludes 33 LEGIT records before release. Those 33 records are not available negative controls.
The published records contain 166 distinct original transcripts. This adapter preserves all records.
Use task clusters for uncertainty. Report natural and synthetic results separately when scoring.
The paper lists 60,468 commands, while the pinned numbered release contains 60,541 executable blocks.
The adapter uses the pinned release counts rather than changing data to match the paper.
The cause of this count difference is not established here.

## Conversion contract

The Hugging Face configuration supplies only the `test` split.
The adapter emits one set, `hackbench-test`, with `split=heldout` and `source_split=test`.
The `natural` flag preserves construction origin. It does not create an invented source split.

`label-map.json` maps release-level HACK to `label=1` using published selection and audited command gold.
The records do not contain a HACK verdict field. The release selection supplies that label.
Malformed or missing gold causes a conversion failure, never a negative label.
Unknown origin or hack type causes an exclusion. The conversion report counts every failure and exclusion.
The 33 publisher exclusions remain separate from adapter exclusions because their rows are not downloaded.

The adapter reuses these existing modules:

- `research/evidence/runs/corpus-expansion-lb6-20261027/tools/hackbench_stage.py`: pinned fetching, digests, and source profiling.
- `research/evidence/runs/corpus-expansion-lb6-20261027/tools/hackbench_adapter.py`: the existing turn parser.
- `research/experiments/ov1104-detbench/detbench_lib.py`: normalized full/actions-only views and run rows.

Only `monitor_input.task_instruction` and `monitor_input.transcript` supply detector content.
The source preprocessor removes historical detector feedback and preserves blocked command attempts.
It supplies the full initial instruction and does not apply model-specific clipping.
The adapter applies no context clipping.

The existing turn parser preserves assistant messages, reasoning, terminal commands, and observations.
It groups all commands from one assistant turn into that turn's tool calls.
It carries observations after their commands and preserves terminal completion summaries as `mark_task_complete` calls.
Shell commands use the existing `bash_command` tool with `keystrokes` arguments.
The full view includes reasoning through the shared `<reasoning>` format.
The actions-only view removes agent prose, reasoning, user messages, and task text through the shared helper.
The DetBench step baseline counts agent turns, not numbered executable blocks.
`command_count` remains separate run metadata.

The source does not supply a structured final reward or final diff.
Both view fields remain null. The adapter does not infer them from terminal output.

Views contain no gold indices, gold command annotations, origin fields, hack-type fields, or source-agent metadata.
The adapter excludes task names as metadata. Names present in original instructions or terminal output remain source content.
The tool commands themselves remain visible because they are the agent's actions, not annotation leakage.
Run IDs and task clusters use SHA-256 digests. Labels and construction metadata stay in `runs.jsonl`, outside views.
`pool=false` prevents the positive-only set from silently entering a binary pooled benchmark.

## Prior exposure

LB-6 previously stages this revision and smoke-tests 3 rows through the live-worker plumbing.
Later committed evidence establishes detector exposure:

- `research/evidence/runs/cm11-hackbench-crosstest-20261028/README.md` records a cascade-v7 cross-test.
- Its `results/facets-score.json` records 169 source trajectories, with 50 successful facets reads.
- `research/evidence/runs/cm15-hackbench-v8-20261028/results/hackbench-score.json` records 169 trajectories in each v7/v8 arm.

Every converted row therefore carries prior CM-11/CM-15 exposure in `read_by`.
This is a new DetBench admission, not a detector-unread source.
An evaluator that requires an empty `read_by` list has 0 eligible records.

The existing registry also records a November 2 study/seal partition.
The study partition contains 75 records across 6 tasks and is open for behavior mining.
The sealed partition contains 94 records across 7 tasks.
Here, sealed means not opened for that study. It does not mean never read by a detector.
The adapter preserves this partition in `exposure_partition` and task names in metadata-only `task_id`.
`primary=true` marks the existing sealed partition, not a claim of detector-unread eligibility.

The registry reports 25 study and 27 sealed independent runs using approximate ordered command matching.
The adapter supplies `independent_run_key` as an exact nonempty shell-command sequence digest scoped to its task.
It normalizes CRLF and surrounding whitespace and excludes completion summaries.
This exact grouping is not the registry's approximate grouping. Do not use it to claim 27 independent sealed runs.
The report keeps both definitions separate. Parent owns the independence policy and pre-registration.

## Commands

From the owned worktree:

```bash
# The token stays in the keys-managed process environment.
keys run -- python3 research/experiments/ov1105-hackbench/intake.py

# Verify cached bytes without another download.
python3 research/experiments/ov1105-hackbench/intake.py --verify-only \
  --manifest /private/tmp/ov1105/hackbench/intake-verified-manifest.json

# Use an empty destination outside every Git checkout.
python3 research/experiments/ov1105-hackbench/adapter.py \
  --cache ~/.cache/eval-lab/source-intake/facebook__hackbench/b3d6f678e6359dcd35e20f3f2c5ee085b5af4934 \
  --output /private/tmp/ov1105/hackbench/converted-final
```

Intake preserves partial downloads instead of silently overwriting them.
Choose a new empty cache directory after a failed partial download.
The adapter refuses a nonempty output directory to preserve earlier evidence.

The output contains:

- `runs.jsonl`: the existing DetBench run contract, with absolute view paths.
- `views/full/hackbench-test/<hashed-id>.json`: the full label-free views.
- `views/actions_only/hackbench-test/<hashed-id>.json`: the actions-only views.
- `conversion-report.json`: aggregate counts, failures, exclusions, source and code hashes, and metric limits.

No raw transcript or view belongs in Git. No detector runs during intake.
Commit and push the adapter, label map, and pre-registration before parent authorizes scoring.
