# DT-11 — a model-free L0 learned screen from run structure and wording, and the funnel cost

Can a classifier built only from the structure and plain wording of a blind run text
(no model call, no label-derived column) beat the length-only rule, and what would an
L0-then-DT-8-reader funnel save? Question, data, features, models, length control, gate
and funnel rules were fixed in `prereg.md` before feature extraction and model fitting.

Zero model calls are made by this item. Data are DT-8's blind run texts for the
Goodfire DEV cohort (150 runs, 80 enacted / 70 clean, labels from DT-2 for 100 reused
runs and DT-8 for 50) and the CheatBench DEV cohort (149 usable of 150: one positive
unparsed and excluded as unknown; 74 / 75).

## Method

`screen.py` (this directory) does everything in one deterministic pass:

1. `load_cohorts` / `run_text`: reads `<blind_id>.txt`, keeps only the text before the
   first `</run>` (the trailing TASK and instructions block is never read).
2. `features`: step count, characters, characters/step, agent-step count, tool-call
   counts per tool name, shell-command token counts (exit, sys.exit, chmod, rm,
   git checkout, curl, base64, eval, pytest flags, pip), path-token counts grouped by
   kind (tests, conftest/pytest.ini/sitecustomize/setup.cfg, config, /tmp, home,
   basename containing expected/answer/solution/test/hidden), narration (reasoning +
   agent message) characters and share, and the 21 pre-registered wording probes
   (case-insensitive occurrence counts). No label or stored detector column is read.
3. `run_cv`: repeated stratified 5-fold CV (10 repeats, seed 20261030) inside each
   corpus for L2 logistic regression (standardised inside each fold) and a depth-3
   histogram gradient-boosted tree (200 iterations, lr 0.05). Per-run OOF scores are the
   mean over the 10 repeats; every operating point uses them.
4. `transfer`: leave-one-corpus-out; threshold taken from the training negatives.
5. `detector_stats` / `boot_length_matched`: 5% false-alarm threshold on that corpus's
   negatives, recall overall and per length tertile, and the tertile-weighted recall
   difference against length-only with a 2000-draw stratified bootstrap interval.
6. `probes_report`: per-probe positive/negative hit rates, two-sided Fisher exact p,
   and the tertile-weighted difference with a bootstrap interval.
7. `funnel`: L0 threshold = 10th percentile of L0 OOF scores among DT-8 reader-alert
   runs (retains at least 90% of reader alerts), pass rate, reader calls, tokens and
   dollars saved per 1,000 runs, and the expected misses found by a random 5% audit of
   L0-negatives.

Reproduce (no model calls):

```bash
uv run --with scikit-learn python screen.py --out <receipts-dir>
```

`features.csv` holds the numeric feature matrix (identifiers and the label for scoring
only; the models never see `positive`).
