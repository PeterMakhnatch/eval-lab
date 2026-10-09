# Kev as a reward-hacking detector (offline, 2026-10-09)

Can Kev ([jaredpalmer/kev](https://github.com/jaredpalmer/kev) @ `53cb0b54`, a small
open "decision model" with Jev's System One API) flag cheating in agent trajectories,
filter SFT candidates, or audit tasks? Questions come from
[`research/calibration/hack-atlas`](../../calibration/hack-atlas/README.md). No RL.
All model work ran on Modal (H100, `modal run`, nothing deployed); nothing ran on the Mac.

**Spend:** about $3.8 of a $5 budget (Modal billing for app `kev-rh`, 9 runs).

## Setup

- **Data** ([DATA.md](DATA.md), `build_chunks.py`): `steinad/CheatBench` traces in
  ~5,000-token windows (+ ≤800-token task prefix). A window containing CheatBench's
  localized evidence is positive, a window of a benign trace negative. Benchmark families
  held out: train = Terminal-Bench 2, HAL-USACO, SWE-smith, MLE-bench, KernelBench,
  BountyBench; dev = 10 held-out TB2 tasks; **test = SWE-bench Verified, SWE-rebench,
  CyBench** (never trained on). Eval set (`prep_sets.py`): 180 positives + 4× negatives.
- **detectors-v1** (the lab's existing cheat corpus): `tw` (100 hacks / 100 clean Terminal
  Wrench traces, sanitized) and `ours` (11 copy-from-upstream / 13 clean Eval Lab traces).
  Trace score = max over windows.
- **Questions** ([questions.json](questions.json)): one generic `any_cheat` noul, one
  noul per agent pattern, an 8-way router `choice`. Exact strings from hack-atlas v1.
- **Model**: `jaredpalmer/kev-4b@v1.0`, bf16 serving path (`modal_kev.py`), 371 ms per
  window for 9 questions ≈ **$0.41 per 1,000 windows** on H100.

## Results

Held-out test families, window level (138 positives / 672 negatives), zero-shot Kev-4B:

| Question | AUROC | TPR @ 2% FPR | TPR @ 5% FPR |
|---|---|---|---|
| `any_cheat` (generic) | 0.50 | 0.07 | 0.09 |
| `answer_leak` | 0.89 | 0.43 | 0.65 |
| `upstream_fetch` | 0.92 | 0.67 | 0.76 |
| `verifier_access` | 0.62 | 0.04 | 0.10 |
| router `1 − P(none)` | 0.65 | 0.06 | 0.09 |
| stacked logistic regression (16 Kev probabilities, trained on train families) | 0.61 | 0.09 | 0.10 |
| Kev-4B fine-tuned (479 train windows, `any_cheat` only) | 0.46 | 0.07 | 0.09 |

Same models on the dev split (unseen tasks, *trained* family TB2, 42 / 48): zero-shot
`any_cheat` 0.92, stacked **1.00**, fine-tuned **1.00**.

detectors-v1, trace level, cut at 5% FPR on `tw` negatives (`uv run evallab detectors
score`, rows `kev_*`; existing judges for comparison, on their own subsets):

| Detector | tw hacks caught | tw clean flagged | ours caught / clean flagged | $ / 1k traces (tw) |
|---|---|---|---|---|
| `kev_router` (zero-shot) | 76/100 | 5/100 | 1/11 / 0/13 | 0.95 |
| `kev_any_cheat` (zero-shot) | 50/100 | 5/100 | 1/11 / 0/13 | 0.95 |
| `kev_ft1_any_cheat` | 12/100 | 5/100 | 1/11 / 0/13 | 0.37 |
| `kev_stacked` | 3/100 | 5/100 | 1/11 / 0/13 | 0.95 |
| `metr_reward_hacking` | 24/32 | 1/32 | 1/11 / 0/13 | 1.96 |
| `harbor_analyze` (own verdict) | 16/19 | 1/18 | 2/10 / 0/7 | 7.49 |
| `rules` (own verdict) | 12/100 | 24/100 | 9/11 / 0/13 | 0 |

Per-question extras (not scorecard rows; chosen after seeing the data): the
`constraint_violation` noul reaches tw AUROC 0.97 (89/100 at 5% FPR) but fires on every
tw family, so it reads as general suspicion, not its pattern. On `ours` the
`upstream_fetch` noul separates perfectly (AUROC 1.00) yet its tw-derived cut flags 9/13
clean traces: thresholds do not transfer between corpora.

**SFT-candidate scan** (the 80 passing Eval Lab trials with ATIF published by 17:00 ET,
listed in `results/sft_scan_trials.txt`; 563 windows; zero-shot `any_cheat` above the tw
5% cut): 4 flagged. 3 are HAR-161 prompted-exploit runs (two of them `cracked` in
`har161-exploit/probe_verdicts.json`); 1 is a CheatBench honeypot smoke
(`cb-ss-4-glm53f-smoke3`) where the agent hunts for the untracked answer key, which
`rules` misses. `rules` fires on 21/80, mostly `upstream_fetch_attempt`; 18 of those are
Kev-negative and unreviewed (`results/sft_rules_vs_kev.json`).

**Task audit** (`build_task_audit.py`): 13 Terminal-Bench tasks with EnvCheck grader
defects vs 26 finding-free tasks, instruction + tests + runner + Dockerfile, 9 task
questions: AUROC 0.25–0.71, chance level on this small set.

## What this says

1. **Ask narrow questions.** Zero-shot Kev-4B detects the hack kinds it is asked about by
   name (answer leak, upstream fetch: AUROC ~0.9 on unseen benchmarks). The generic
   "is this cheating?" question is at chance on SWE-bench-style leaks (0/115 caught).
2. **Training on our labels did not transfer across benchmarks.** Stacking and a
   delta fine-tune both learn the training family perfectly (dev 1.00) and fall to chance
   on held-out families, and the fine-tune degrades Terminal Wrench (AUROC 0.87 → 0.78).
   With 4 trainable patterns from 6 families, Kev learned benchmark surface, not cheating.
3. **As a cheap trajectory triage it is competitive on Terminal Wrench**: the zero-shot
   router catches 76% at 5% FPR for $0.95 / 1k traces, near METR's scanner (75% on 32
   negatives) at half the cost. Caveat: 72/100 sanitized tw hacks mention "verifier"
   (DATA.md), a wording cue a small model can exploit.
4. **Not for task audits.** Judging whether tests are weak needs reasoning Kev-4B lacks.
5. **Calibrate per corpus.** Every operating point here needs negatives from the same
   source; a cut from one corpus misfires on another.

## Limits

Positives in test are mostly `answer_leak` (115/138); no `verifier_access` in test. Eval
Lab's own positives are 11 traces of one hack. The fine-tune is a 60-step pilot (Kev's
README calls ~400 records "inside the noise"). Kev's trainer re-reads the state once per
question, so training cost scales with questions per record: one question took 0.71 s per
5k-token window on H100; five took roughly 9 s (estimated from a 4-step probe's wall time).

## Reproduce

```bash
X=research/experiments/kev-hack-detector
uv run --with transformers python $X/build_chunks.py       # CheatBench + detectors-v1 windows
uv run --with transformers python $X/prep_sets.py          # eval / external / sft / all_chunks
uv run --with transformers python $X/build_task_audit.py
modal run $X/modal_kev.py::score --model jaredpalmer/kev-4b@v1.0 --questions $X/questions.json \
  --input derived/kev/sets/all_chunks.jsonl --out derived/kev/outputs/zeroshot.scores.jsonl      # ~$1
uv run python $X/make_train.py --pos 300 --dev-pos 16     # 900 train windows (stacking features)
modal run $X/modal_kev.py::score --model jaredpalmer/kev-4b@v1.0 --questions $X/questions.json \
  --input derived/kev/sets/train.states.jsonl --out derived/kev/outputs/zeroshot.train.scores.jsonl  # ~$0.25
uv run --with scikit-learn python $X/stack.py
uv run python $X/make_train.py --pos 160 --dev-pos 12 --qids any_cheat --tag ft1
modal run $X/modal_kev.py::train --data derived/kev/sets/train.ft1.kev.jsonl \
  --dev derived/kev/sets/dev.ft1.kev.jsonl --name <new-run-name>                                 # ~$0.5
modal run $X/modal_kev.py::score --model <new-run-name> --questions <any_cheat-only card> \
  --input derived/kev/sets/all_chunks.jsonl --out derived/kev/outputs/ft1.scores.jsonl
modal run $X/modal_kev.py::score --model jaredpalmer/kev-4b@v1.0 --questions $X/task_audit_questions.json \
  --input derived/kev/sets/task_audit_chunks.jsonl --out derived/kev/outputs/task_audit.scores.jsonl
uv run python $X/analyze.py --scores derived/kev/outputs/zeroshot.scores.jsonl --name zeroshot-kev4b
uv run python $X/analyze.py --scores derived/kev/outputs/task_audit.scores.jsonl --name task-audit-zeroshot --task-audit
```

`prep_sets.py` re-globs `~/Developer/eval-lab-results`, so a rerun also picks up trials
published later.

`results/*.json` hold every number above; `write_cache.py` imported the `kev_*`
detectors-v1 verdicts. Fine-tuned weights: Modal volume `kev-rh-runs:/kev4b-rh-ft1`.
