# HAR-112: repo-aware broken-task checker

**Question:** can a cheap model tell which RL coding tasks are broken? A task is broken when the hidden tests need something the instruction never says and the repo doesn't already provide.

**Answer:** yes, when it checks names against the task's repo at the base commit. Checker v3 passes the HAR-111/112 gate on a fresh blind holdout, and has labelled the 1,180-task HAR-108 pool.

## What changed from HAR-111

HAR-111's checker (v2) failed the gate. Its wrong "broken" calls were all names the tests use that already exist in the repo (1702, 002961, 002848, 002222), and the model can't see the repo. v3 adds:

1. **Repo extraction ($0, no sandbox).** [`repo_extract.py`](repo_extract.py) streams the task image's layers from Docker Hub and keeps the small text files under `/workspace/repo` (code tasks) or `/app` (terminal tasks). It skips `.git`, binaries and files over 1 MB.
   - Output lands in `derived/trace-lab/har112/repos/<task>/`.
   - Measured: about 3 GB streamed per image, 1–18 minutes each, roughly 80 images an hour with 8 workers.
2. **Typed items.** [`checker_v3.py`](checker_v3.py) asks GLM-5.3-Flash (T=0.7, `reasoning_effort=low`) for each unstated item with a `kind` (new_name, exact_string, extra_behavior, contradiction, other) and the exact `names`/`strings` the tests use. Its severity rules mirror [RUBRIC_v2](RUBRIC_v2.md), so the checker and the hand labels answer the same question.
3. **Repo filter.** A `new_name` or `exact_string` item is dropped when every name and string in it exists in the repo, and the file:line is recorded as evidence. "Exists" means defined, imported, or used as a parameter, keyword, key or path, or present as a literal. The model is not trusted to judge this.
4. **Repo on demand.** The repo is only needed when a sample flags a not-inferable name or string. Model samples are cached under `<out>/raw/`, so re-judging never re-spends.
5. **Cascade, pre-registered before the holdout.** Sample 1 decides unless it says broken after the repo filter. In that case, samples 2 and 3 run and the label is the median.

Frozen at 2026-09-30T07:57:55Z ([`checker_v3.sha256`](checker_v3.sha256): v3 code, the extractor, and the HAR-111 modules it imports), before any holdout scoring. The original receipt describes the later `repo_extract.py` change as lint-only (unused import removal and loop-variable rename). The manifest records both the as-run hash (`repo_extract.py.as_run`) and the committed one.

**HAR-132 provenance limit:** the blob matching the recorded as-run extractor
hash `a55494be81ebb2dfdc40865165c7165b377209343248099f99482495073b6204`
was not found in the inspected checkout or PR #570 revisions. The committed
extractor hash and frozen label/scorer hashes verify, and the scores and pool
export reproduce; the claimed as-run-to-committed code equivalence cannot be
independently byte-verified from those retained sources. The freeze manifest
is unchanged.

Disclosure: the rater agents' one-line label lists for 6 of the 8 batches (one of them partial) reached me a few minutes before this freeze. I made no checker change between seeing them and freezing. The last change, the broken-only cascade, went in before any holdout label existed.

## Fresh holdout

- **Tasks:** 40 pool tasks never used for tuning, seeded ranks 47–86 of `sha256("har111:"+id)` ([`holdout2_tasks.txt`](holdout2_tasks.txt)). The checker was tuned only on the 60 HAR-111 tasks ([`tune_tasks.txt`](tune_tasks.txt)).
- **Labels:**
  - Two blind raters labelled all 40 under RUBRIC_v2 with repo access ([`hand_a/`](hand_a), [`hand_b/`](hand_b)).
  - A third rater settled the 10 disagreements after verifying the disputed items against the repo ([`hand_adj/`](hand_adj)).
  - Freezes: A/B at 08:30:32Z, adjudication at 08:37:50Z (`*.sha256`). [`score_v3.py`](score_v3.py) refuses to score if any file changed.
- **Final labels:** 23 sound, 9 suspect, 8 broken.
- **Caveat:** 001651's extract finished only after rater A labelled it. A re-checked the label against the repo's real sources and recorded `repo_complete_at_label: false`. 000204 and 002445 were re-verified on complete repos, and no label changed.

`python3 score_v3.py hold2_v3 tune_v3`:

| final \ checker | sound | suspect | broken |
|---|---|---|---|
| sound | 16 | 6 | 1 |
| suspect | 0 | 8 | 1 |
| broken | 2 | 2 | 4 |

- **Gate: PASS.**
  - Must-catch: 002259 → broken and 002407 → broken. These are tuning tasks, so the result shows the regression is fixed, not that v3 generalises.
  - Holdout "broken" calls: 6. 1 is on a hand-sound task (000234), so 17% were wrong, under the 25% bar.
  - Strictly, counting any label other than broken as wrong: 2 of 6 (33%). The second is 000202, which the hand labels call suspect.
- **Recall:** 4 of 8 hand-broken tasks were called broken. 2 more were called suspect (001262, 002003) and 2 sound (001940, 000832).
- **Small sample:** 6 broken calls is a small number. [INFERENCE] A different 40-task draw could plausibly land on either side of 25%.

**Run-to-run stability (measured after the gate).** The pool run later re-labelled the same 40 tasks with fresh samples from the same frozen checker.
- The two runs agree on 30/40 labels.
- The second run made 5 broken calls, 2 on hand-sound tasks (000204, 000234): 40%.
- Across both runs: 11 broken **call instances**, 3 on hand-sound (27%); these are not counts of distinct tasks.
- So the gate pass is real but marginal. At T=0.7, the one-sample-unless-broken cascade leaves label noise near the 25% line.
- Four tasks are flagged broken in both runs: 000234, 000323, 000566 and
  001248. **Among hand-sound tasks**, only 000234 is broken in both. Both
  raters call it sound, because the missing method names (`get_chain_id`,
  `get_full_shard_id`) follow the repo's `get_shard_id` convention. The checker
  treats any unstated new name as not_inferable. That is the main remaining
  mismatch between checker and rubric (see v2.1 below).

**Ablation, on the same cached samples, without the repo filter (`label_without_repo`):**
- 10 broken calls, 4 of them on hand-sound tasks (000234, 001768, 002425, 002445): 40% wrong. That fails the gate.
- The filter removed 3 false alarms and 1 true case, 002003. There `order_results` exists, but the tests need a new third positional argument. Checking that a name exists can't catch a changed signature.

**Tuning-set scores, same frozen checker, reported as seen data:**

| label set | exact | broken calls | on hand-sound |
|---|---|---|---|
| v1 hand, 30 | 15/30 | 6 | 1 (000383) |
| v1 second rater, 30 | 19/30 | 6 | 1 (1634) |
| v1 holdout, 20 | 12/20 | 3 | 1 (002222) |

- In 002222 the fix that forwards kwargs to the ufunc also handles `dtype=`. Seeing that needs reading the code, not just a name lookup.

## Rater agreement (20/30 in HAR-111)

**Cause.** Every one of the 10 HAR-111 disagreements traced to rubric wording (counts overlap):
- the severity line between "plausibly guessable" and "cannot reasonably be inferred": 7;
- sibling functions and variants: 4;
- quoted or linked spec formats: 2;
- exact substrings and output channel: 2;
- how much repo exploration to assume: 2;
- pre-existing names: 1.

v1 even listed "a sibling function" as an example of suspect while its broken definition covered the same case.

**Fix: [RUBRIC_v2](RUBRIC_v2.md)** (sha256 in [`RUBRIC_v2.sha256`](RUBRIC_v2.sha256), the version the raters used). It is an ordered decision procedure with worked examples:
- R1: names in the repo are given, and the labeler must check the repo.
- R2: siblings are items, and a grep decides severity.
- R3: preservation checks are covered.
- R4: quoted specs cover only what they say.
- R5: exact strings and channels.
- A literal minimal-fix tie-break.

**Result:**
- Agreement is 30/40 (75%) on the fresh holdout, up from 20/30 (67%).
- Only 1 of the 40 was a sound-vs-broken split (000566).
- The 10 v2 disagreements were settled by R2 siblings (4), the minimal-fix tie-break between suspect and broken (3), R5 (2) and R3 (1).

**v2.1 amendments (proposed from the adjudicators' notes; not used for these labels):**
- R5 also covers exact serialized fields in API return values (e.g. a `notes` string), not only CLI output.
- Quantity requirements on the named function ("called once" vs "one or a few calls") are items. They are guessable if the instruction's wording admits the tested count, and not_inferable if a minimal fix would call it a different number of times.
- R2 remains the largest source of disagreement. Where the analogous change is ambiguous, raters should write the sibling's minimal fix out explicitly, as the tie-break does.
- New names the instruction never gives, and the repo lacks, but that follow an adjacent repo naming pattern (`get_shard_id` → `get_chain_id`): v2 doesn't say. The raters called them covered, while the checker calls them not_inferable. v2.1 should pick one rule, and the checker prompt should follow it.

## Pool (1,180 tasks, HAR-108)

[`pool_labels.jsonl`](pool_labels.jsonl): one line per task, with the label, the per-sample labels, the deciding items (test reference and quote), and the items the repo check dropped with file:line evidence. Produced by `python3 export_pool.py derived/trace-lab/har112/pool_v3 pool_labels.jsonl`.

| label | tasks |
|---|---|
| sound | 590 |
| suspect | 415 |
| broken | 175 (14.8%) |

- 292 tasks flagged a not-inferable name or string, so their repos were extracted (266 in the pool pass, about 486 GiB streamed, $0).
- Without the repo filter, 312 tasks would be broken. The filter turned 137 broken calls into suspect or sound.
- 242 tasks went to three samples.
- The not-inferable item kinds behind the broken calls:
  - extra_behavior only: 50;
  - exact_string + extra_behavior: 29;
  - extra_behavior + new_name: 22;
  - new_name only: 19;
  - exact_string only: 13;
  - exact_string + new_name: 11;
  - smaller combinations: the rest.
- 4 tasks first returned unparseable JSON. Their bad samples were discarded and re-drawn once; all 1,180 now have a label.

**How to use these labels.** [INFERENCE from the holdout numbers above] Read "broken" as roughly 3 in 4 right when judged against sound. Treat it as a triage flag for exclusion or repair review, not ground truth. Recall on hand-broken tasks is about half, so "sound" and "suspect" still contain broken tasks.

## Spend

- **Model:** $1.3373 in total, over 1,958 GLM-5.3-Flash calls ([`spend.jsonl`](spend.jsonl)). That covers tuning, the holdout, the pool and the retries.
- **Sandbox:** $0. The repos came from Docker Hub registry streaming, not Daytona or Docker.
- **Cap:** $2.

## Files

- `repo_extract.py`, `checker_v3.py`, `score_v3.py`, `export_pool.py`: code.
- `checker_v3.sha256`, `checker_v3.frozen_at`: the checker freeze.
- `hand_a/`, `hand_b/`, `hand_adj/` and their `.sha256` files: the holdout labels.
- `tune_v3/`, `hold2_v3/`: checker outputs. `raw/` holds the cached model samples.
- `pool_labels.jsonl`: one line per pool task.
- `spend.jsonl`: every model call, with tokens and cost.
