# AutomationBench Verified V0.0: original-side Task QA cross-test

This directory contains adapter/profile code and aggregate counts, digests and hashed
identifiers only. No task, saved answer, defect description, label row, patch or
upstream grader code is copied into git. **Cross-test only, local only:** freeze one
Task QA design and run it once; no training, tuning, few-shot/retrieval examples,
playbook extraction, vendor upload, production database/CAS copy or rehosting.
No auditor, model, task tool, grader or local Docker was run during staging.

## Source and pin

- Audit publisher: **Parsewave**, [AutomationBench Verified V0.0 on Kaggle](https://www.kaggle.com/datasets/parsewave/automationbench-verified-v0-0).
- Kaggle package **version 20**, last updated **2026-10-06T11:22:31.547Z**.
- Original benchmark: [Zapier AutomationBench](https://github.com/zapier/AutomationBench),
  pinned to **`4a8e1061254004d9dac807054eed33fad7d1ff14`**. The manifest's
  `revision` is this upstream commit, not an invented Kaggle Git revision.
- Archive object: `gs://kaggle-data-sets/12304865/20385717/bundle/archive.zip`.
  Signed query parameters are neither retained nor required for offline use.
- ZIP central-directory SHA-256:
  `510bae8199f07740b91df82543fdb55eab17b5d938b8d240927ffec68974a62d`.
- Local bundle:
  `~/.local/state/evallab-cross-test/parsewave__automationbench-verified/4a8e10612540/`.
- Manifest SHA-256:
  `8b156850cc7b6f72f4d31b52eb135b44c69ff44301136d2fd3f77c9e9fd0eeb7`.

The Kaggle download endpoint redirects to a range-capable GCS object without an
account/token. A temporary standard-library seekable HTTP-range file object let
`zipfile` inspect the directory and fetch only the selected members. Requests retry
with backoff and refresh the redirect after HTTP failures. The retained central
directory and its full per-member CRC32/size inventory are pinned in the manifest.
The entire upstream source was fetched through codeload and checked against the
recursive Git trees API at the exact commit, including every file's Git blob SHA-1.
No upstream file was silently skipped; no VCS metadata was staged.

| Staged or excluded material | Files / members | Uncompressed bytes |
| --- | ---: | ---: |
| Selected Kaggle package | 25,055 | 641,820,339 |
| Of which `evidence_verified/` | 25,044 | 622,311,361 |
| Complete pinned upstream source | 572 | 17,373,317 |
| Retained package metadata, ZIP directory and upstream tree | 3 | 4,568,096 |
| Manifest-listed payload | 25,630 | 663,761,752 |
| Excluded traces and display artefact | 1,239 | 169,505,621 |

The compressed archive is 149,603,235 bytes; the selected evidence alone compresses
to 112,267,368 bytes. Extraction stopped at the requested 250 MB uncompressed-size
boundary and resumed only after explicit authorization for all 642 MB. Exclusions
are exactly `traces/**`, `traces.csv`, `trace_regrades.csv` and `chart.gif`; their
members, sizes and CRC32s remain accounted for in the directory inventory. The
selected top-level files, complete `grader/`, `evidence_verified/` and `tools/` are
retained, including the fixed exports and patch **for scorer-side provenance only**.

All extracted files passed directory CRC32 checks and received final-byte SHA-256s.
All 572 source files passed Git blob checks. The publisher evidence manifests have
24,834 present member declarations, all digest-matching, plus **three `.pyc`
declarations absent from the publisher ZIP itself**. These three inventory gaps are
recorded with hashed identifiers; nothing is synthesized to fill them. All 210
publisher patch hashes match the retained patch. A read-only `git apply --check`
confirmed that it applies to the untouched pinned upstream; no fixed grader tree
was created or executed.

## Rights and local terms

The audit data is **CC BY 4.0**, matching Kaggle's `licenseName` of “Attribution 4.0
International (CC BY 4.0)”. Attribute Parsewave and link the published source and
licence. The upstream AutomationBench task material and code are **MIT**; retain
Zapier AutomationBench contributors' copyright/permission notices. Both scopes are
recorded under manifest `licences`, while top-level `licence` is `cc-by-4.0`.
`LICENSE-AutomationBench.txt`, the upstream `LICENSE`, and the original Kaggle
metadata are retained locally.

The bundle's `CROSS_TEST_ONLY.md` imposes stricter local-use rules than these
permissive source licences: no cloud/hosted API, model/judge or local model use by
this staging lane, no corpus upload to Laminar/Docent, no rehosting, no git copy,
and no production database or content-store copy. Only the four code/aggregate
receipt files in this directory are published. No gate or account terms were
accepted; accountless public download was sufficient.

## What is labelled

Parsewave AI-flagged candidate problems, then hand-reviewed the findings and fixes.
`fixes.csv` has **210 rows, 210 unique task names, all `status=verified`**. These are
publisher-verified task/grader defects, not independent third-party adjudications
of Parsewave's decisions. Inter-rater agreement and independent adjudicator counts
are not reported. The publisher's evidence folders retain original/fixed tasks,
selected saved probe answers, and their scores on both graders; none is an auditor
label source except the publisher's verified membership/family labels.

| Verified defect family (`issue_group`) | Tasks |
| --- | ---: |
| Task vs. Evaluator Mismatch | 72 |
| Attribute mismatch | 52 |
| Classification errors | 36 |
| Text-matching gaps | 23 |
| Missing or duplicate steps | 17 |
| Miscellaneous | 7 |
| Broken tool routes | 3 |

There are **600 scored tasks and 200 simple tasks**. The verified subset contains
**197 scored and 13 simple tasks**; 403 scored and 187 simple tasks are unchanged.
Each of the six scored domains has 100 tasks; `simple` has 200. Verified counts by
domain are finance 65, hr 50, marketing 44, operations 17, support 14, simple 13,
and sales 7. The uncertainty/bootstrap cluster is **domain**, seven clusters.

## Schema and original-side reconstruction

- `tasks.csv`: 800-row task inventory with task name, example ID, domain, scored
  flag and audit metadata. `fixes.csv`: the 210-row verified-label/family table.
- `automationbench_verified.jsonl`: 600 **fixed-version** exports.
  `automationbench_verified_simple.jsonl`: 200 fixed-version simple exports.
  Their fields are `example_id`, `domain`, `task_name`, `fix_status`, `prompt`,
  `answer`, `info`; `info` is serialized JSON. Neither export supplies auditor input.
- Every one of the 210 evidence folders contains `original_task.json` with
  `example_id`, `task_name`, `prompt` and `info`. `info` includes original initial
  state, assertions, tool routes and an identity field, plus optional original
  benchmark metadata. The fixed Python task, README, manifest, saved answers and
  scores are kept separate.
- The pinned upstream's seven original dataset builders reconstruct **800 tasks**.
  Their deterministic noise helpers matter: using only literal task functions
  would omit injected initial-state material. The adapter loads just these data
  constructors/helpers, avoiding application imports and task execution. Its only
  representation adaptation is `Dataset.from_list(rows)` to `rows`, before the
  Hugging Face allocation; original selection, noise, ordering and serialization
  remain intact. The builders' `answer` field is empty for all 800 tasks; original
  assertions and state are the answer specification, not invented reference text.

For the 210 verified tasks the adapter uses the evidence's **original** prompt and
info, taking the original answer field from the pinned upstream. All **210/210**
original evidence records match upstream reconstruction on example ID, prompt and
complete info, including noise. The 590 unchanged tasks use the original upstream
builders, never the publisher's fixed-version exports. The original rubric's 32
source files plus its original tool/schema/state support (454 files total) are
provided as in-memory text, without executing them.

| Original-side availability | Verified 210 | All 800 |
| --- | ---: | ---: |
| Reconstructible original task | 210 | 800 |
| Original grader material available | 210 | 800 |
| Both original sides available | 210 | 800 |
| Undefined original side | 0 | 0 |

`specs(bundle=...)` yields exactly 800 dicts. Pass **only** `auditor_input`, which
contains original `prompt`, `answer`, `info`, `grader_material`, and upstream
repository/revision identity. Separate blocks are `gold` (label state), `labels`
(state/family/type/domain/scored), `identity` (task name/example ID/domain/cluster),
`reconstruction`, and `auditor_input_status`. Missing originals are explicitly
marked unavailable, never reconstructed from fixed content or guessed. The checked
bundle has `auditor_input_status=ready` for all 800.

## Label semantics

Exactly two label states are exposed:

- **`defective_verified`: 210** — publisher hand-verified task/grader defects.
- **`unchanged_not_certified`: 590** — not fixed in this release, **not certified
  clean** and never a bare `negative` label.

There are zero certified-negative labels. Profile bookkeeping therefore reports
210 positives, zero certified negatives, and 590 not-certified/unlabelled tasks.
A flag rate on the 590 is an upper bound on false alarms **only if you assume they
are clean**, which the publisher does not claim. Do not use unchanged membership
as evidence of task correctness, natural defect prevalence or grader validity.

## Fields the auditor must not see

`FIELDS_AUDITOR_MUST_NOT_SEE` enumerates every `fixes.csv` column: `task_name`,
`example_id`, `domain`, `scored`, `status`, `note`, `issue_group`, `issue_type`,
`finding`, `what_was_wrong`, `what_the_fix_does`, `fix_changes`, `test_answers`,
`wrong_answers`, `correct_answers`, `wrong_passed_by_original`,
`wrong_passed_by_fixed`, `correct_failed_by_original`, `correct_failed_by_fixed`,
and `evidence_folder`.

Also prohibited: `fixed`, `fix_status`, gold/labels/identity/cluster fields,
reconstruction status, evidence folder names and paths, evidence READMEs/manifests,
`fixed_task.py`, fixed/after task or grader material, `fixed_rubric.patch` and its
hash, fixed JSONL exports, saved test answers, `runs/`, `scores.csv`, either grader's
saved scores, publisher comments and package README/description. Identifiers stay
scorer-side even if their spelling does not encode a defect. Original `info.task_name`
is removed, with no positive-dependent path/name marker replacing it.

The input-boundary selfcheck enforces an allowlist of envelope/info metadata fields
and reports **zero audit-label fields in `auditor_input`**. Original business data
such as application-record `status` or assertion `scored` is preserved: these are
part of the original task contract, not values copied from audit CSV columns.
Only original source directories feed `grader_material`; neither patch nor fixed
Python/evidence commentary is eligible. Do not pass the entire spec to an auditor.

## Exact scoring recipe

1. Before first use, freeze and hash auditor code/prompts, adapter, threshold and
   this recipe. Run once on the 800 original-side inputs. Never select a threshold,
   tune a prompt or alter rules after seeing this cross-test's labels/results.
2. Score one task-level flag per available input. Report missing/failed evaluations
   and undefined originals separately, never as no-flag outcomes. Join labels only
   in the scorer, after the frozen auditor returns its output.
3. Primary requested summary: **conditional balanced accuracy**
   `0.5 * (recall_defective_verified + 1 - flag_rate_unchanged_not_certified)`.
   This treats unchanged tasks as clean *only as an explicit working assumption*;
   it is not validated clean-negative balanced accuracy. Always report verified
   recall, the unchanged flag rate, and the assumption alongside it. Do not call
   the latter a measured true false-positive rate.
4. Report verified recall per defect family, task-level confusion counts under the
   same explicit assumption, and both flags/no-flags in the 590 unchanged tasks.
   Include scored/simple and domain coverage; provide scored-only results as a
   secondary view rather than silently dropping the 13 verified simple tasks.
5. Use 95% percentile intervals from **10,000 domain-cluster bootstrap resamples,
   seed 0**, sampling the seven domains with replacement and retaining all their
   tasks/labels together. Report undefined family-specific resamples explicitly.
   Seven clusters limit precision. Publish counts/digests/hashed IDs only.

## Overlap with our DEV/TEST/cloud store

The parent measured all **210 verified `task_name` values** by hash: **0 matches**
in our DEV key list, **0** in TEST, and **0** in the cloud task store, under both
`bare` and `full` normalization. These provided measurements were not recomputed
against private identifiers by this builder. They do not establish overlap status
for the other 590 tasks, example IDs, semantic near-duplicates, authors or training
corpora.

The profile provides SHA-256 ID lists for all 800 tasks under both normalizations,
and separate 210-verified-only lists. `task_id_hashes` uses task names;
`example_id_hashes` separately uses stringified example IDs. `bare` is
`id.split('@')[0].rsplit('/',1)[-1].rsplit('__',1)[-1].casefold()`; `full` is
`id.casefold()`. Example IDs alone are not assumed unique; task names are the
800 unique join identities, and domain is the bootstrap cluster key.

## Known limits

- AI-flagged then hand-reviewed by Parsewave, not independent third-party
  adjudication; no inter-rater agreement is reported.
- Some fixes change prompts, initial data or tool routes, not only graders. The
  package reports **122 of 206 compared tasks change task inputs**. A detector
  result here concerns original task/grader mismatch, not just assertion syntax.
- Saved probe answers are adversarially selected. Their wrong-pass/false-fail
  counts do **not** estimate natural prevalence or model/agent performance.
- **13 of 210** verified defects belong to the 200 originally unscored simple
  tasks; reported coverage must not hide them.
- Zapier-style workflow automation is a different domain from Terminal-Bench or
  SWE-bench; transfer performance and source independence are not established by
  identifier non-overlap alone.
- The 590 unchanged tasks are not certified clean; specificity and conventional
  balanced accuracy remain conditional on an unsupported clean-set assumption.
- Three publisher `.pyc` inventory declarations are absent upstream in the ZIP;
  all original task JSON and pinned original grader source are present, so these
  gaps do not require guessing an original side.
- Source presence and reconstruction do not independently validate the publisher's
  defect findings, a fix's correctness or the natural frequency of those defects.

## Provenance of this staging

The bundle is outside git and all other corpus stores. `STAGE-MANIFEST.json`
records every payload's final size/SHA-256, source CRC32 or Git blob, Kaggle version,
object path, complete ZIP member inventory, exclusions, both licence scopes, and
publisher inventory gaps. `CROSS_TEST_ONLY.md` and the manifest are the only files
excluded from their own payload inventory. After writing these receipts, all files
and directories were made read-only and `tools/profile.py` re-verified them.

The profile checks manifest membership, final-byte SHA-256s, ZIP CRC32s, upstream
Git blob/tree pins, retained central-directory pins, present publisher SHA-256s,
known absent `.pyc` declarations, label joins, original reconstruction and zero
writable paths. It emits aggregate distributions, digest summaries and hashed IDs
only, with `verified_after_lock=true` and `writable_paths=0`.

Reproduce offline without making the bundle writable:

```sh
EVALLAB_DISABLE_LOCAL_DOCKER=1 PYTHONDONTWRITEBYTECODE=1 python3 tools/adapter.py selfcheck
EVALLAB_DISABLE_LOCAL_DOCKER=1 PYTHONDONTWRITEBYTECODE=1 python3 tools/profile.py
```

These commands never print corpus text, never dump specs and never run an auditor.
