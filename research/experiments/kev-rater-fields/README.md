# Can Kev fill the trace-rater's fields? (zero-shot pilot, 2026-10-28)

Question: can `jaredpalmer/kev-4b@v1.0` (a small Jev-compatible decision model)
pre-fill the fields a blind trace-rater writes by hand, so raters only read
the runs Kev is unsure about?

**Answer: yes for `loop_kind`, partly for `pass_copied`, no for the rest.**
Zero-shot Kev-4B labels loop kind about as well as a second rater. Two fields
are already handled by code: a rule over `result.json` beats Kev on stop
reason and blame. Kev fails on SFT cleanliness and on locating the first failure.
Total Modal spend: $0.47 (`spend.jsonl`).

## Results (`results/agreement.json`)

Agreement with the rater consensus, i.e. runs where both raters gave the same
label (disagreements excluded):

| Field | n | Kev-4B | Rule over run facts | Majority class | Rater A vs B (all 154) | Verdict |
|---|---:|---:|---:|---:|---:|---|
| `loop_kind` (digest, 3-way) | 133 | **0.895** (κ 0.84) | — | 0.383 | 0.864 (κ 0.79) | auto-fill |
| `stop_reason` | 153 | 0.281 | **0.980** | 0.556 | 0.994 | use the rule |
| `blame` | 151 | 0.325 | **0.947** | 0.762 | 0.981 | use the rule |
| `pass_copied` (passes) | 29 | AUROC 0.889; 0.793 at P≥0.5 | — | 0.690 | 1.000 | flag for review (n small) |
| `sft_clean` (passes) | 20 | AUROC 0.438 | — | 0.600 | 0.870 (20/23) | rater only |

Window-level scores (5k-token windows, max over the run, no threshold tuned):

| Question | Result | Baseline |
|---|---|---|
| Looping vs not looping, from max(`repetition_here`, `completion_claim_here`) | AUROC 0.850 | run length alone 0.637 |
| Repetition vs completion-claim among looping runs | AUROC 0.996 | — |
| `pass_copied` from max `upstream_fetch` (hack-atlas card) | AUROC 0.911 | — |
| First-failure window: argmax `first_failure_here` contains a rater's ref | 40/133 (0.30) | random window 0.25; both raters' refs share a window 0.82 |

Why each field went the way it did:

- **`loop_kind`** is pattern recognition over the step outline, which Kev does
  well. Its 14 mistakes fall in four confusion cells (three of 4, one of 2). The
  pre-registered triage cut (top probability ≥ 0.8) covers **0** runs: with the
  carried temperature (2.41) the highest top probability is 0.785. The
  ranking still works; see the post-hoc checks below.
- **`stop_reason`/`blame`** depend on arithmetic and rules over the run facts.
  Kev calls 82 of 85 token-ceiling runs `request_ceiling` (it does not compare
  95 calls with a limit of 120). For `blame` it predicts `none` for 44 runs the
  raters blamed on the model, and `harness` 16 times, which the raters never chose.
  The rule misses 3 `stop_reason` runs: two at 118 calls that the raters
  called token ceiling, and one budget-exception run they called loop break. All
  8 `blame` misses are passes the raters blamed on the model (a copied pass is
  `model`), so the rule needs `pass_copied` as an input.
- **`sft_clean`** combines four conditions on 20 runs; Kev is at chance.
- **First-failure location** needs judgement about the whole run ("the
  first step it never recovered from"), and a single window cannot see that.
  Kev is barely above a random window.

### Post-hoc checks (`loop_kind_posthoc`, chosen after seeing the results)

- **Not a leak from the loop-break message.** On the 102 consensus runs without a
  `LoopBreakStop` exception (its message contains "repetition"), accuracy is
  0.873. It labels all 18 loop-broken completion-claim runs correctly, despite
  that message.
- **Like a third rater.** Against each rater on all 154 runs (hard cases
  included) Kev agrees 0.844 (A) and 0.831 (B), against A-vs-B 0.864. On the 21
  runs where the raters split, Kev sides with A 11 times and with B 9 times.
- **Usable confidence ranking.** Accuracy by thirds of top probability is
  0.773, 0.932 and 0.978 (top third: top p ≥ 0.583). A triage cut must be set
  on new held-out labels, not on these.

### Limits

One agent, one harness and one task family (MiMo-9B, Terminus-2, Python bug
fixes); n is small for the pass-only fields (29 and 20). Labels come from
frozen blind reviews; the rater guide's own fields were used unchanged. Kev
saw the same facts the raters read in `result.json`, plus an outline that
drops middle steps on long runs.

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
