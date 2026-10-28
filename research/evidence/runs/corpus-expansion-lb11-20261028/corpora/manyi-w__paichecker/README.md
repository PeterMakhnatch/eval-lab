# PAIChecker: human-labelled issue/PR alignment cross-test

**These labels measure issue/PR/test semantic alignment, not weak-test false
passes, reward hacking, cheating intent, or agent-run correctness.** A positive
means at least one released misalignment family. `No Misalignment` rows are
human-labelled aligned tasks: unlike positive-only sets, these are real negatives,
not unlabelled rows converted to negatives.

This directory contains code and aggregate/hash receipts only. Keep source files,
label rows, raw task ids, issue text, patches, graders and examples in the locked
local bundle, never in git, PRs, chat, a vendor API, hosted model/judge, Laminar,
Docent, a shared drive, production database or CAS. Freeze one auditor design and
run it once; no training, tuning, few-shot/retrieval examples or playbook
extraction. Staging makes no model calls, runs no auditor and uses no Docker or
local LLM inference.

## Source and pin

- Publisher: Manyi Wang, Junjielong Xu and Pinjia He,
  [PAIChecker](https://github.com/manyi-w/PAIChecker), revision
  `0f0a2aee87a3cfb40c92120c47225c4d83cb3dd7`.
- Local source copy: `~/.local/state/evallab-cross-test/manyi-w__paichecker/0f0a2aee87a3/`.
  The pinned repository [README](https://github.com/manyi-w/PAIChecker/blob/0f0a2aee87a3cfb40c92120c47225c4d83cb3dd7/README.md)
  is retained there as `README.md`; it defines the families and cites
  [arXiv:2607.28587](https://arxiv.org/abs/2607.28587).
- The full pinned GitHub tree is retained except
  `labelled_data/swe-bench-analysis.csv`: 6,792,113 bytes, Git blob
  `dc9268f5bd7c558c56fc9155b259ca18ac530991`. That SWE-bench Verified label
  file overlaps tasks in our DEV and is explicitly listed under manifest
  `excluded`; it is neither staged nor adapted.
- Task statements, reference PR patches and tests are joined locally from the two
  pinned Hugging Face datasets below, beneath this same bundle's `graders/`.
  Their README cards and `.gitattributes` are also preserved. There are no
  separate grader bundles or nested restriction notices.

| Grader dataset | Full pinned revision | Parquet bytes | Publisher LFS SHA-256 oid |
| --- | --- | ---: | --- |
| [SWE-bench/SWE-bench_Multilingual](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual/tree/846e647b9f33c0b51b739d005d13d85493c9af09) | `846e647b9f33c0b51b739d005d13d85493c9af09` | 1,875,846 | `92abca7cb527b41a9f66d03a26ce441ff7319e3a49f985998fd56be4bb9b08b2` |
| [SWE-Gym/SWE-Gym](https://huggingface.co/datasets/SWE-Gym/SWE-Gym/tree/bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb) | `bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb` | 43,644,473 | `60569cea74bb281f7a5579467436a2bc1932c6e0c5f2f7fa0d084392abd9ad97` |

The respective source files are `data/test-00000-of-00001.parquet` and
`data/train-00000-of-00001.parquet`; local prefixes are
`graders/swe-bench__swe-bench_multilingual/` and `graders/swe-gym__swe-gym/`.

## Rights and local terms

The pinned PAIChecker `LICENSE` is MIT, copyright 2025 PAIChecker Authors.
Both pinned HF cards declare `license: mit`; `xt_stage.py plan` reports public,
ungated sources with empty gate terms. No account terms were accepted and no
licence override was used. Preserve repository and dataset-card attribution;
these declarations do not establish extra rights in underlying third-party
repositories. Our stricter local evaluation restrictions are in
`CROSS_TEST_ONLY.md`: no rehosting or vendor upload, production DB/CAS copy,
training, tuning, distillation or examples. Everything in the bundle, including
its notice and manifest, is locked read-only.

## What is labelled

The release supplies `instance_id,human_label` CSVs for SWE-bench Multilingual
and SWE-Gym. The judgement is whether the issue and linked PR are semantically
aligned, including test-imposed requirements. The pinned README defines five
patterns and `Others`; the arXiv abstract describes the issue/PR pairing problem
and a separate three-phase automated checker. **The automated checker is not run
here and its outputs are not substituted for the human-labelled files.**

Neither the pinned README nor the arXiv abstract reports annotator identities,
annotator count, annotation/adjudication protocol, or an agreement statistic.
The two CSVs contain no per-annotator ids or votes. We therefore make no claim
about inter-annotator agreement or how disagreements were resolved, and do not
infer those details from the multi-agent tool implementation.

## Schema and joined counts

`tools/adapter.py specs(bundle=...)` streams one dict per labelled task from both
sub-corpora. Its three blocks are:

- `auditor_input`: exactly `repo`, `base_commit`, `problem_statement`,
  `hints_text`, `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS`, `patch`.
  The test-name columns retain their native string-list structure. `patch` is
  the SWE benchmark's reference PR, deliberately visible for alignment checking;
  it is not the PAIChecker misalignment gold.
- `gold`: `positive` is any misalignment family; `families` is a sorted,
  deduplicated set represented as a list. No raw label cell is emitted.
- `identity`: `instance_id`, `sub_corpus`, `repo`. `repo` is the bootstrap cluster
  key and is also reproduction context in `auditor_input`. Raw ids never leave
  local memory in the adapter/profile outputs.

| Sub-corpus | Label rows | Grader rows | Joined label ids | Missing label ids | Duplicate label / grader rows | Positive | Aligned negative | Unlabelled grader ids omitted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `swe_bench_multilingual` | 300 | 300 | 300 | 0 | 0 / 0 | 73 | 227 | 0 |
| `swe_gym` | 2,287 | 2,438 | 2,287 | 0 | 0 / 0 | 1,012 | 1,275 | 151 |
| Total adapted | 2,587 | 2,738 | 2,587 | 0 | 0 / 0 | 1,085 | 1,502 | 151 |

The supplied paper denominator is 2,438 SWE-Gym rows, but this pinned label CSV
has 2,287: **151 SWE-Gym rows of the paper are absent from the file**. The
pinned SWE-Gym dataset card independently reports 2,438, and the local join finds
exactly 151 grader ids without released labels. The README and abstract do not
explain the omission. Those 151 rows are not adapted, not scored, and never
silently treated as negatives; the 2,438 paper denominator is not the adapter's
2,287-task denominator.

## Label semantics

The following family definitions come from the pinned repository README:

| Family | Meaning | Multilingual positives | SWE-Gym positives |
| --- | --- | ---: | ---: |
| `SC` | Scope creep: the PR delivers functionality beyond the original issue request. | 33 | 400 |
| `DP` | Defective PR: a bug or incomplete fix in the current PR requires later correction. | 24 | 143 |
| `IS` | Incomplete specification: later discussion supplements/revises the issue, and the PR implements those later requirements. | 13 | 318 |
| `UL` | Unspecified literal: tests require an exact fixed runtime literal not specified by the issue or discussion. | 9 | 373 |
| `FP` | Follow-up PR: the current PR supplements/corrects an earlier PR for the same issue. | 2 | 102 |
| `Others` | Clear misalignment outside those five patterns. | 1 | 13 |

Comma-separated cells are multi-family. SWE-Gym additionally has numeric subtype
suffixes (for example the taxonomy token `SC-1`); the adapter drops numeric
suffixes and counts each base family at most once per task. The pinned README
does not define those fine-grained subtype suffixes, so no subtype interpretation
or subtype-level metric is claimed. Family counts overlap and must not be summed
to obtain the 73/1,012 binary positive totals.

`No Misalignment` is exclusive and produces `positive=false,families=[]`.
It means human-labelled aligned, not proof of comprehensive tests, a weak-test
false-pass control, a correct agent solution, or an absence of every possible
benchmark defect. In particular, neither a positive nor a negative establishes
whether an incorrect solution can pass the grader. Never pool these results
with weak-test wrong-pass or cheating-intent scores.

## Fields the auditor must not see

Only the exact `auditor_input` allowlist above may reach the auditor; never
serialize the entire spec. `FIELDS_AUDITOR_MUST_NOT_SEE` explicitly includes
`human_label`, `families`, `positive`, `instance_id`, `sub_corpus`, `sub-corpus`,
`gold`, `labels`, `identity`, `label`, `label_tokens`, `label_subtypes`,
`label_counts`, `defect_text`, `fix_description`, `fixed_version`,
`after_version`, `status`, `publisher_comments`, `evidence`,
`source_tool_output`, and `label_derived_fields`. This includes **anything
derived from the label**, not just these spellings. Hashes, class counts,
sub-corpus membership, raw ids and source filenames are scorer-side, not prompt
material. The no-label-input check covers every emitted task.

The source repository's classification prompts, skill copies, example JSONL,
auxiliary evidence, and any checker status/comments are archival only, never
candidate inputs. Do not execute its source code. All unselected parquet metadata
(including timestamps, versions, images, log parsers and evaluation scripts where
present) is omitted. No later corrected PR/grader version or publisher defect
explanation is added. The reference `patch` and `test_patch` are the original
pinned benchmark artifacts, not repaired/after versions.

## Exact scoring recipe

1. Before any auditor exposure, freeze code, rules/prompts, binary conversion and
   threshold, adapter and source revisions by digest. Evaluate once locally; do
   not train or tune on either sub-corpus or inspect row-level labels to select
   rules. No model/auditor run is part of this staging receipt.
2. Score the 300 Multilingual and 2,287 SWE-Gym tasks **separately**, one binary
   prediction of issue/PR misalignment against `gold.positive` per task. Treat
   an unavailable/error verdict as missing and report coverage and missing counts,
   never silently as an aligned negative. No score uses the 151 unlabelled rows.
3. Report confusion counts (TP, FP, TN, FN), coverage and **balanced accuracy**
   as primary: `(TP/(TP+FN) + TN/(TN+FP))/2`. Also report misalignment recall,
   flag rate on human-labelled aligned negatives `FP/(FP+TN)`, and recall for
   each of `SC,DP,IS,UL,FP,Others` using that family's positive task denominator.
   Family recall here is binary flag recall conditional on family membership,
   not an unrequested subtype classifier score. With missing decisions, withhold
   full-population rates and provide covered-class rates with their denominators.
4. For each sub-corpus compute 95% percentile confidence intervals using 10,000
   **repo-cluster bootstrap** resamples, NumPy seed 0: sample its distinct
   `identity.repo` clusters with replacement, retaining all tasks from each drawn
   repo and its multiplicity. Do not treat rows from a shared repo as independent.
   Mark class-absent resamples undefined and report their count rather than
   assigning a rate. Keep multi-family membership intact within each task.
5. Report the two sub-corpus results side by side, the 151-row release gap,
   missing decisions, and undefined interval counts. Any optional pooled summary
   is secondary and must jointly cluster a shared repo across both sub-corpora;
   do not relabel an alignment score as grader false-pass detection.

## Overlap with our DEV/TEST/cloud store

The parent lane's supplied hash measurement finds **0 matches** for all 300
Multilingual ids and all 2,287 SWE-Gym ids against each of our DEV key list,
TEST key list and cloud task store, under **both `bare` and `full`**
normalisations. This staging quotes that measurement; it does not read private
keys or rerun the comparison. `bare` is exactly
`id.split('@')[0].rsplit('/',1)[-1].rsplit('__',1)[-1].casefold()`;
`full` is `id.casefold()`. The profile supplies a SHA-256 hex digest of every
normalised task id under each rule, never a raw id.

This is id-based evidence, not proof of text/patch independence, freedom from
training contamination, or disjointness from all other staged cross-test sets.
The excluded SWE-bench Verified label CSV is specifically not covered by the
zero-overlap claim.

## Known limits

- Human annotation and agreement details are not established by the permitted
  README/abstract sources; labels are used as released, without invented votes.
- 151 SWE-Gym rows are absent from the label file; their labels and omission
  reason are unavailable. No full-paper-corpus result follows from this subset.
- Fine-grained suffixes are collapsed to documented base families, and labels
  are multi-family. `Others` has only 1 Multilingual and 13 Gym positive tasks.
- Later discussion/follow-up history can be needed to judge `IS`, `DP` and `FP`.
  The adapter retains the pinned `hints_text` and reference patch but does not
  fetch extra issues, PRs, discussion or later corrections. This is a frozen
  task-material projection, not a claim of complete annotation evidence.
- The grader snapshot supplies test patches and expected test-name sets, not
  checked-out repositories, dependency environments or executed test outcomes.
  Unselected harness metadata is not an auditor input; no executable/replayed
  benchmark or weak-test false-pass control is claimed.
- Source-level and repo-cluster dependence, possible human label error, and the
  limits of id-only overlap checks remain material.

## Provenance of this staging

LB-11, 2026-10-28. Downloaded the exact GitHub codeload tree and verified every
retained file against the GitHub API's pinned Git blob SHA-1, plus SHA-256.
Reused the pQ-SELF-1 `xt_stage.py` `plan`/`fetch` functions for the pinned HF
files, verifying their LFS SHA-256 oids or regular Git blobs; moved downloaded
grader artifacts beneath `graders/` within the same bundle before writing the
final combined manifest. No extra marker-bearing bundle was created.

The manifest lists **40 files / 45,961,051 bytes**: 34 GitHub tree files and
6 HF files, excluding its generated manifest and restriction notice. Each
GitHub file has `git_blob` and `sha256`; HF source revisions, MIT declarations
and per-file LFS oids are recorded under `sources`. Manifest SHA-256:
`7c6709f726f8305e516fc8cbbee8a40d03b7409f4978ebdce907724ef6a53ab2`.
The combined bundle is locked with `xt_stage.py lock` and reverified afterward.

From this directory, with a Python 3.12+ environment containing PyArrow:

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run python tools/profile.py
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run python tools/adapter.py selfcheck
```

`profile.py` verifies the exact manifest and restriction-notice digests,
all file sizes/SHA-256/Git blobs/LFS oids, full file inventory, source pins,
read-only modes, complete joins and the adapter leakage boundary. It writes
only `results/profile.json`, with counts, digests and hashed ids, and prints
aggregates only. `verified_after_lock=true` requires `writable_paths=0`.
The sorted raw-id digest hashes UTF-8 ids sorted and newline-joined without a
trailing newline; `task_id_hashes` holds both specified normalisations. No raw
id, task text, label row, patch or grader is emitted in the receipt or logs.
