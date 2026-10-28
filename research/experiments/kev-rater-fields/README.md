# Can Kev fill the trace-rater's fields? (zero-shot pilot, 2026-10-28)

Question: can `jaredpalmer/kev-4b@v1.0` (a small Jev-compatible decision model)
pre-fill the fields a blind trace-rater writes by hand, so raters only read
the runs Kev is unsure about?

## Plan (written before any Kev output)

- **Cohort:** all 154 runs that already carry frozen two-rater blind labels in
  `research/explorations/trace-lab` (HAR-119, HAR-128 g2/g6, HAR-116 loopfix).
  All are MiMo-V2.6-Distill-Qwen-9B on Terminus-2 solving Python bug-fix tasks,
  so the result says nothing yet about other agents or domains. No new labels.
- **States** (`build_states.py`): a ≤6k-token digest per run (task, the
  `result.json` facts a rater sees, a one-line-per-step outline, last steps in
  full) and ~5k-token windows per run in the `kev-hack-detector` shape.
- **Cards:** `questions_trace.json` (choice: stop_reason, blame, loop_kind;
  noul: pass_copied, sft_clean) on digests; `questions_window.json`
  (first_failure_here, repetition_here, completion_claim_here, and the
  hack-atlas `upstream_fetch` card verbatim) on windows. Zero-shot; the cards
  are not revised after scores exist.
- **Metrics** (`analyze.py`): agreement with rater consensus, rows where the
  raters disagree excluded and counted; inter-rater agreement as ceiling,
  majority class as floor; deterministic rule baselines for stop_reason and
  blame; threshold-free AUROC for window aggregates, with run length as the
  baseline a max-over-windows score must beat; first-failure window hit rate
  against first/last/random windows.
- **Decision rule:** a field can be auto-filled if Kev's accuracy is ≥ 0.85 and
  at least 5 points above both the majority class and any rule baseline. It can
  triage if accuracy where Kev's top probability is ≥ 0.8 reaches 0.90 on at
  least 40% of runs. Otherwise rater-only.
- **Spend:** Modal H100 for app `kev-rh`, cap $2 (Peter, 2026-10-28), recorded
  in `spend.jsonl`. Nothing runs on the Mac.

## Reproduce

```bash
X=research/experiments/kev-rater-fields
uv run --with transformers python $X/build_states.py
cd research/experiments/kev-hack-detector
modal run modal_kev.py::score --model jaredpalmer/kev-4b@v1.0 \
  --questions ../kev-rater-fields/questions_trace.json \
  --input ../../../derived/kev-rater-fields/digest.jsonl \
  --out ../../../derived/kev-rater-fields/digest.scores.jsonl
modal run modal_kev.py::score --model jaredpalmer/kev-4b@v1.0 \
  --questions ../kev-rater-fields/questions_window.json \
  --input ../../../derived/kev-rater-fields/windows.jsonl \
  --out ../../../derived/kev-rater-fields/windows.scores.jsonl
cd - && uv run python $X/analyze.py
```
