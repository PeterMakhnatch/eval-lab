# HAR-119: harness-vs-model loops, and out-of-sample scores for trial-explanation tools

Owner: Traces. Budget: $1 of model calls. No sandboxes, no trials.

## Part 1: are the "model loops" partly the harness? ($0)

`loop_kind.py` reads the 38 Python runs (HAR-104's 10 plus HAR-110's 28). For each one it joins:
- HAR-114's `token_flow` row (loop onset and last useful edit, from `research/experiments/har114-tokenflow/runs.jsonl`);
- a per-step read of the stitched trajectory (probe-03 `assemble_trial`).

It then classifies each loop as a completion-claim loop, repetition, or none. The rules are in the script's docstring.

Rerun:

```
python3 loop_kind.py <har114 runs.jsonl> trial_dirs.txt probe03_capabilities.jsonl out
```

- `trial_dirs.txt`: the 38 trial folders. They are local and not in git.
- `probe03_capabilities.jsonl`: probe-03 run over those folders; used for the stop reason.
- `out/`: output directory. It holds `loop_kind.jsonl` (one row per run), `summary.json` and `table.md`.

Result, posted on HAR-119 and HAR-116 at 2026-09-30 23:35Z:
- **Loops:** 8 of 25 looped runs (32%) are completion-claim loops.
- **Tokens:** 12.28M of 50.97M post-edit input tokens (24.1%) are in the claim phase.
- **Thresholds:** both are above the card's 25% and 20% bars.
- **Where the claim loops are:**
  - HAR-104 plain: 4 of 5 loops, 50% of post-edit tokens.
  - HAR-110 GEPA search trials: 4 of 6 loops.
  - HAR-110 named arms: 0 of 14. Those runs never reached "Are you sure".

## Part 2: blind hand labels and tool scores

| File | Contents |
|---|---|
| `select_runs.py` | Selection rule for the 12 runs. |
| `selection.json` | The frozen selection, with its sha256 in `labels/MANIFEST.sha256`. |
| `RATER_GUIDE.md` | The label fields and definitions. |
| `labels/rater_a/`, `labels/rater_b/` | One JSON file per trial, frozen. The stable path for scoring any per-trial explanation. |
| `labels/MANIFEST.sha256`, `labels/FROZEN_AT` | The freeze record. |
| `predictions/` | Eval Lab, Scout and Docent outputs, plus `MAPPING.md` and `NOTES.md`. |
| `score.py` | Writes `scores.json` and `scores.md`; refuses to run if a label changed. |

Scout's bulky intermediates (normalized trials, probe-03 rows, Scout databases, 27 MB) are kept out of git, in `derived/trace-lab/har119/scout_work/`.

### How it was run

1. **Selection.** I picked the 12 runs with a seeded hash: 4 per arm (plain, seed and candidate), including both v2 passes, from the 24 fresh HAR-110 split-v2 runs.
2. **Raters.** Two blind raters labelled each run: A (RaterA1–3) and B (RaterB1–3), each a separate agent context. They read only the trial folder and the task folder, and none reports opening anything off-limits.
3. **Freeze.** Labels were frozen at 2026-10-01T00:00:02Z, before I read any tool prediction.
4. **Tools.** The tools ran blind to the labels (`predictions/NOTES.md`). The field mapping in `predictions/MAPPING.md` was written at 23:42Z, before any tool ran. Spend was $0: Docent's hosted reading used 578k input tokens, with no billing signal.
5. **Adapters.** After the freeze, and before any tool-vs-label comparison, I added two unit conversions (documented in `score.py`):
   - Eval Lab's stitched step equals the native `step_id` on all 12 runs.
   - Docent's cited block is mapped to a step by `docent_block_map.json`.

### Results

`scores.md` has the full table. `_vs_agreed` scores only the cells where both raters agree.

| field | rater A vs B | Eval Lab | Scout | Docent | Part 1 loop rule |
|---|---|---|---|---|---|
| stop reason | 12/12 | 12/12 | 12/12 | not expressed | — |
| first failure (±2 steps) | 9/12 | 4/9 | 2/9 | 3/9 | — |
| blame | 11/12 | not expressed | 11/11 | 11/11 | — |
| loop present | 11/12 | 7/11 | 7/11 | not expressed | 7/11 |
| loop kind | 11/12 | not expressed | not expressed | not expressed | 7/11 |
| loop onset (±5) | 6/6 | 2/4 | 1/6 | not expressed | 3/5 |
| pass copied (passes only) | 2/2 | 2/2 | not expressed | not expressed | — |

### Reading

**Facts you can trust:**
- **Stop reason:** every source agrees on all 12 runs.
- **Copied pass:** Eval Lab's `pass_may_be_copied` matched both raters on the two passes, but n = 2.

**Opinions, each with a known error rate:**
- **First failure:**
  - The raters agree 9 of 12 times, within 2 steps.
  - Out of sample, Eval Lab's #560 rule matches the agreed cells 4 of 9 times. In-sample it scored 8/10 (HAR-109).
  - Docent: 3 of 9. Scout: 2 of 9.
- **Loops:**
  - **Detection:** every detector over-fires. Eval Lab, Scout and the HAR-114 `token_flow` onset that Part 1 uses each match the agreed loop/no-loop call 7 of 11 times. Mostly they flag a "loop" on held-out runs both raters call loop-free, such as 000495-plain, 000587-plain and 000495-seed.
  - **Kind:** none of the shipped tools can tell a completion-claim loop from repetition.
- **Blame:** the high scores are uninformative. On this sample both raters say `model` on every failure, so a constant "model unless it is an earned pass" answer would also score 11/11. Nothing here tests the harness, task or infra calls.
- **The one blame disagreement** is the copied pass CFCbfps: A said `model`, B said `none`. The rater guide does not define blame for a copied pass; RATER_GUIDE v2 should.
- **Part 1 is a lower bound.** On these 12 runs the loop rule disagrees with the raters 4 times, and all 4 go against completion-claim:
  - it misses X2dMzMw, where the model claimed done at step 12 and then echoed filler for 110 turns;
  - on three loop-free runs, it calls HAR-114's onset "repetition", which inflates the denominator.
- **This matters for HAR-116's loop-break.** It fires on that same HAR-114 onset, so it would nudge runs that are still working. 000495-plain, for example, has its onset at step 46 and its last edit at step 84.

## Part 3: RUBRIC v2.1

`../har112/RUBRIC_v2.1.md` (sha256 in `RUBRIC_v2.1.sha256`) folds in the four HAR-112 amendments. Each one cites a grounding task:

| Rule | Change | Grounding task |
|---|---|---|
| R5 | Serialized, dict and JSON fields count like output strings | 002536 |
| R7 (new) | "One/few" vs an exact-N call count is an item; it is guessable only if the minimal fix hits N | 000413 |
| R2 | Write the sibling's minimal fix out when the analogy is debatable | 001248 |
| R6 (new) | Names that mechanically extend a verified repo pattern are covered | 000234 |
