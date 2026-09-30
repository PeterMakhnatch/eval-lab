# HAR-111: can a cheap checker find broken tasks? Not yet: it fails the gate

**Question.** Can a script, plus one cheap model, read a task's instruction and hidden tests and say whether a correct fix to the stated problem would still fail? The answer is a label: sound, suspect or broken.

**Answer.** Not reliably enough to label the 1,180-task pool unattended. The checker catches the broken tasks, but too many of its "broken" calls land on sound tasks. The cause is structural: it cannot see the repository, so a test name that already exists in the code looks like a new, unstated requirement. Following the card's gate, the full-pool run was **not** done.

## The gate, and how the checker did

The card's gate: catch 002259 and 002407, and fewer than 1 in 4 "broken" calls wrong.

| Checker setup (GLM-5.3-Flash) | Label set | Exact agreement | Catches 002259 / 002407 | Wrong "broken" calls (on hand-sound tasks) | Gate |
|---|---|---|---|---|---|
| Rules only | 30 validation | 4/30 | yes / yes | 3 of 4 | fail |
| v1 prompt, low reasoning | 30 validation | 19/30 | yes / yes (both only "suspect") | 1 of 1 | fail |
| v2 prompt, low reasoning | 30 validation, seen | 17/30 | yes / yes | 2 of 6 | fail |
| v2 prompt, high reasoning | 30 validation, seen | 14/30 | no (answer truncated at 4K) / yes | 0 of 3 | fail |
| **v2, 3 samples at T=0.7, broken if ≥2 say broken** | 30 validation, seen | 19/30 | yes / yes | 2 of 7 (1702, 002961) | fail (29%) |
| same, frozen before the holdout | **20 holdout, unseen** | 13/20 | n/a | 1 of 2 (002848) | fail (50%) |

- "Seen" means the prompt or vote rule was chosen after looking at errors on those 30, so those rows are optimistic.
- The holdout row is the honest one. Its single hand-broken task, 002628, was called broken by all 3 samples.

## How much do two careful labelers agree?

A second blind labeler relabelled the same 30 tasks (`hand_rater2/`).

| Pair | Exact agreement | Rater A's "broken" calls that rater B called sound |
|---|---|---|
| Rater 1 vs rater 2 | 20/30 | 2 of 5 (000927, 002757) |
| Rater 2 vs rater 1 | 20/30 | 1 of 5 (001868) |
| Checker (3-sample vote) vs rater 1 | 19/30 | 2 of 7 |
| Checker (3-sample vote) vs rater 2 | 20/30 | 4 of 7 |

- The checker agrees with each labeler about as often as the labelers agree with each other.
- The boundary between suspect and broken is itself fuzzy. The raters disagree on 1634 (suspect vs sound) and 002407 (suspect vs broken).
- Both raters call 002259 broken.
- Only two of the checker's "broken" calls are on tasks **both** raters call sound: 1702 and 002961. The same failure shows on the holdout, where 002848 is hand-sound.

## Why the false "broken" calls happen

All three have the same cause: the tests use a name the instruction never mentions, but the name already exists in the repository.
- 1702: `resolve_max_length`, `via_property` (the task is a regression repair of existing code).
- 002961: `sort_by`, `desc`, `create_index`.
- 002848: `networkCheck`, `extractTarGZ`, `src/DownloadModels.py`. The labeler verified these against upstream.

The checker sees the instruction and the test diff, but not the code at the base commit. Stricter voting does not fix this. Requiring all 3 samples to say broken removes the false calls, but on the 30 it then catches only 1 of the 5 hand-broken tasks.

**Fix that would likely pass the gate [INFERENCE]:** give the checker a lookup of the repository at the base commit (e.g. a `git grep` for each flagged name), and drop "new name" items whose name already exists. Code tasks keep the repo inside the pinned Docker image (`/testbed`), so this means reading each image's `/testbed`, preferably on remote compute. Proposed as a follow-up.

## Cost

- Spend: $0.19 of the $3 cap, over 261 logged GLM-5.3-Flash calls (`checker/spend.jsonl`).
- Per task: about $0.0006 with low reasoning, $0.0014 with high, and $0.002 for the 3-sample vote.
- The full pool would cost about $2.3 with the 3-sample vote. [INFERENCE: extrapolated from these 50 tasks.]
- GLM-5.3-Flash API notes:
  - `thinking.type=disabled` is rejected (error 1210);
  - top-level `reasoning_effort` accepts only low, high or max;
  - without it, reasoning used the whole 4K output budget and returned nothing.

## Files

- **Rubric:** `RUBRIC.md`, written before any labels or checker.
- **Hand labels:** `hand/` (30 validation), `hand_holdout/` (20 holdout), `hand_rater2/` (second rater on the 30). The `.sha256` next to each set is its freeze manifest.
  - 30: frozen 2026-09-30T06:03:28Z. sha256 of the manifest is `bcb2a5f7…abf1`; the file was later renamed from `hand_labels.sha256` to `hand.sha256` with contents unchanged.
  - 20 holdout: frozen 06:22:47Z, `1c6d2018…4a85`.
- **Checker:** `checker/rules.py` (rule pass), `checker/checker.py` (model pass, prompts v1 and v2), `checker/zai.py` (client with a hard spend cap and ledger), `ensemble.py` (3-sample vote). The code frozen before each scoring is in `checker_v1.sha256` and `checker_v2.sha256`.
- **Scoring:** `score_checker.py OUT_DIR [--labels hand|hand_holdout|hand_rater2]`. It verifies the freeze before scoring.
- **Checker outputs:**
  - `val_out*` and `val_v2_t07_*` on the 30;
  - `hold_v2_t07_*` on the holdout;
  - `dev_out` on 10 tuning tasks outside both label sets.
- **Selection:** the 17 random validation tasks and the 20 holdout tasks are pool ranks 0–16 and 27–46 of `sha256("har111:" + task_id)` over the HAR-108 pool (`research/experiments/har108-python-census/pool.json`) minus the 10 HAR-104 tasks. Ranks 17–26 are the tuning tasks.

## Handoff to Data (HAR-108)

`census_labels.jsonl` has one row per labelled task: 50 tasks, of which 47 are in the Python pool. Each row carries:
- the hand label and its test-line evidence;
- the second-rater label, for the 30 validation tasks;
- the checker label, with its three sample labels.

Only the **hand labels** are fit for the task-health table. The checker labels are included for comparison and should not be used as verdicts.
- Broken according to both raters: 000093, 002198, 002259.
- Broken according to one rater: 002628 (holdout, single-rated), 000927, 002757, 001868, 002407.
