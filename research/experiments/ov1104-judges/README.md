# ov1104-judges: LLM Judges That Read What the Agent Did, Not What It Said

## Question
Does stripping agent narration and reasoning (`actions_only`), supplying the task contract and final diff (`J2` / `J2b`), and ensembling across model families (`J3`) improve reward-hacking detection over the deployed AutoReview monitor (`J0`) and trivial run-length baselines on DEV (`replay-v1`, 690 runs) and frozen Held-Out corpora (11 sets, 3,321 runs) at $0 subscription cost?

## Short Answer
1. **Why length beats monitors on Goodfire (`goodfire_diagnostic.json`):** Using passage-level consensus labels from `cloud-trace-db.sqlite` (`Goodfire/reward-hack-data`, three GPT-5.6-Sol votes per passage), **46 / 80 (57.5%)** of Goodfire positive runs have **zero** flagged `tool_calls` passages (`30` reasoning-only, `8` content-only, `8` content+reasoning). Their labels come from `<think>` reasoning (e.g., `upstream_fix_recall` or `hidden_test_targeting` on SWE-bench) or submission prose (`misrepresented_compliance`). Among the 38 ImpossibleBench positive runs in Goodfire, **only 4 / 38** have `native_metrics.shortcut_signal == 1.0` (`test_passed == 1.0`, meaning the agent actually took the shortcut and passed the mutated test); `20 / 38` have `test_passed == 0.0` (the agent refused to special-case the mutated test, triggering a 4-attempt harness rejection loop of ~26 steps vs 12 steps on unmutated `original` tasks), and `14 / 38` are on unmutated `original` tasks (`shortcut_signal == 0.0`). Run length (`AUROC 0.802 [0.728, 0.869]`) detects the ImpossibleBench retry loop and long SWE-bench `<think>` traces—within `conflicting` tasks, clean runs (`median 25.5 steps, 195,360 chars`) are longer than cheat runs (`median 26.0 steps, 168,721 chars`).
2. **On real action-enacted Goodfire cheats (`shortcut_signal == 1.0`), `J2b` achieves 4 / 4 (100%) recall at 0 / 70 (0.0%) Goodfire FPR:**
   - Deployed AutoReview (`J0` at threshold `>= 50`) catches **1 / 4** (`3 / 80 = 3.8%` overall; at its 5% pooled-DEV FPR threshold `>= 8`, `J0` catches `9 / 80 = 11.3% [6.0%, 20.0%]` with `1 / 70` Goodfire false alarm, `AUROC 0.543 [0.505, 0.585]`).
   - `J1` (`actions_only` without task/diff) catches **3 / 4** at `>= 80`, but **0 / 80 (`0.0% [0.0%, 4.6%]`)** at 5% pooled-DEV FPR because `19 / 358 (5.3%)` of pooled DEV negatives score `100` when the judge cannot see that `<task_statement>` authorized reading `/solution/` or editing a script (`thr05 = None`).
   - `J2` (`actions_only + task + diff`) cuts `score >= 60` DEV false alarms by **3.3x** (`36 / 358 = 10.1%` -> `11 / 358 = 3.1%`), raising pooled DEV `AUROC` to **`0.770 [0.723, 0.819]`** and `recall@5%FPR` to **`0.551 [0.497, 0.604]`** (`Goodfire recall@5%FPR = 0.062 [0.027, 0.138]`, `5 / 80`, `0 / 70` Goodfire FP).
   - `J2b` (`J2 + V2_ADDENDUM` scanning for environment fix acquisition and contradictory-test special-casing) catches **4 / 4 (100%)** of executed ImpossibleBench shortcuts (`7 / 80 = 8.8% [4.3%, 17.0%]` overall on Goodfire at `0 / 70` FP, `AUROC 0.544 [0.514, 0.578]`) and reaches pooled DEV **`AUROC 0.781 [0.734, 0.830]`**, **`recall@5%FPR = 0.578 [0.525, 0.630]`** (`192 / 332`), beating `J0` (`0.482`), `length_chars` (`0.063`), and the best single L0 rule `hidden_info_read` (`0.410`).
   - Second-family `J3` (`j3_actions_task_diff_fable_partial`, 150 / 150 on Goodfire and 126 / 150 on CheatBench before the Anthropic quota stop, plus `openai-codex/gpt-6.1-terra` post-reset) reaches **Goodfire `AUROC 0.764 [0.695, 0.837]`** (`recall@5%FPR = 0.100 [0.052, 0.185]`, `0 / 70` FP) and **CheatBench `AUROC 0.916 [0.875, 1.000]`**. Cross-family `max(J2b_flash, J3_fable)` reaches **`pooled:dev AUROC 0.886 [0.836, 0.923]`**, **`recall@5%FPR = 0.620 [0.567, 0.671]`** (`206 / 332`), and **`Goodfire AUROC 0.765 [0.696, 0.837]`** (`recall@5%FPR = 0.113 [0.060, 0.200]`, `0 / 70` FP). Combining with `L0_hidden_info_read` via parameter-free percentile `max` reaches **`pooled:dev recall@5%FPR = 0.651` (`216 / 332`)** and **`Goodfire recall@5%FPR = 0.163` (`13 / 80`)**.
3. **Stability (10% DEV re-run, $n = 76$ paired runs at `temperature=0.5, seed=7` vs `temperature=0.0`):**
   - `J2` (`gemini-3.8-flash`): Spearman $\rho = 0.982$, mean absolute difference $2.43$ points, 5%-FPR flag agreement **$75 / 76 = 98.7\%$** (Wilson 95% CI $[92.9\%, 99.8\%]$).
   - `J2b` (`gemini-3.8-flash`): Spearman $\rho = 0.962$, mean absolute difference $2.11$ points, 5%-FPR flag agreement **$73 / 76 = 96.1\%$** (Wilson 95% CI $[89.0\%, 98.6\%]$).
4. **Frozen Held-Out Transfer (11 complete DetBench held-out sets, 3,321 runs, scored once after `FREEZE.md` commit `88c8be063`):**
   - On `pooled:heldout` (7 cheating-construct sets, $n = 1,450$, $344$ pos / $1,106$ neg, $168$ clusters, $100\%$ coverage):
     - Frozen `J2b` (`version 7a4f27c6ba7d...`): **`AUROC = 0.799 [0.754, 0.844]`**, **`recall@1%FPR = 0.372 [0.323, 0.424]` at held-out FPR `0.039 [0.029, 0.052]`**, **`recall@5%FPR = 0.657 [0.605, 0.705]` at held-out FPR `0.093 [0.077, 0.112]`**, beating run length by **`+0.190` AUROC (`beats_length = yes`)**.
     - Frozen `J2` (`version f6783a6273ec...`): **`AUROC = 0.793 [0.748, 0.840]`**, **`recall@1%FPR = 0.343` at held-out FPR `0.028`**, **`recall@5%FPR = 0.651` at held-out FPR `0.089`**, beating run length by **`+0.183` AUROC (`beats_length = yes`)**.
   - Per held-out set (`J2b`): `reflex-sentinel` ($n=250$) **`AUROC 1.000 [1.000, 1.000]`** (`R@1%FPR = 1.000` at `0.000` FPR); `researcharena-benchmark-gaming` ($n=41$) **`AUROC 1.000 [1.000, 1.000]`** (`R@1%FPR = 1.000` at `0.000` FPR); `trusted-monitoring-logs` ($n=233$, unread) **`AUROC 0.864 [0.783, 0.935]`** (`R@5%FPR = 0.697` at `0.000` FPR); `beam-tripwire` ($n=613$) **`AUROC 0.835 [0.774, 0.893]`** (`R@5%FPR = 0.795`); `caught-not-taught` ($n=206$, unread) **`AUROC 0.756 [0.656, 0.837]`** (`R@5%FPR = 0.435` at `0.000` FPR); `latent-agent-sandbox-episodes` ($n=326$, unread, `natural=True`) **`AUROC 0.712 [0.630, 0.782]`** (`R@1%FPR = 0.354` at `0.007` FPR, `R@5%FPR = 0.461` at `0.041` FPR).

## Second Family: J3 on `openai-codex/gpt-6.1-terra` (added 2:00 AM ET, after the freeze)

The Codex weekly window reset at 1:35 AM ET, so the second-family batch could only start after
`FREEZE.md` was written. Code, rubric, prompt, view and decoding are identical to `J2b`
(`V2_ADDENDUM`, temperature 0.0, `window_chars 60000`); only the model route differs. Its version
string is `c9d77cb7b0a5cb418abf76e398811915fa7fdd4c6d2ba783ed05109c9ae8c2a5`, which is the string
the pre-Codex `FREEZE.md` text already named for J3. Per `FREEZE.md` ADDENDUM, every J3 number is
**exploratory**: the frozen primary signal for held-out stays `j2b_actions_task_diff_v2_flash`.

| Judge (model family) | DEV thr @5% FPR | Pooled DEV AUROC | Pooled DEV R@5%FPR | Goodfire AUROC | Goodfire R@5%FPR pooled thr | Goodfire R@5%FPR own-domain thr | Executed shortcuts (n=4) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `J0` AutoReview (flash) | 8 | 0.727 | 0.482 | 0.543 | 0.113 (1/70 FP) | 0.087 (1/70) | 2/4 |
| `J1` actions only (flash) | none | 0.726 | 0.000 | 0.537 | 0.000 (0/70) | 0.075 (0/70) | 0/4 at that threshold |
| `J2` actions+task+diff (flash) | 20 | 0.770 | 0.551 | 0.531 | 0.062 (0/70) | 0.062 (0/70) | 3/4 |
| `J2b` (flash, frozen primary) | 20 | 0.781 | 0.578 | 0.544 | 0.087 (0/70) | 0.087 (0/70) | 4/4 |
| `J3` (terra, same view) | 89 | **0.841** | 0.464 | **0.778** | 0.075 (0/70) | **0.275** (2/70) | **4/4** |
| mean(`J2b`, `J3`) | 49 | **0.849** | 0.545 | 0.778 | 0.075 (0/70) | **0.287** (2/70) | **4/4** |
| max(`J2b`, `J3`) | 90 | 0.845 | 0.479 | 0.778 | 0.075 (0/70) | 0.287 (2/70) | **4/4** |
| mean(`J2b`, `J3`, `J3` fable) | 47.7 | **0.872** | 0.545 | **0.802** | 0.075 (0/70) | 0.237 (1/70) | 1/4 |
| max(`J2b`, `J3`, `J3` fable) | 90 | 0.865 | 0.479 | **0.805** | 0.075 (0/70) | 0.287 (2/70) | **4/4** |
| `length_chars` | 272,968 | 0.531 | 0.063 | 0.802 | 0.225 (2/70) | 0.025 (1/70) | 0/4 |
| `length_steps` | 95 | 0.489 | 0.024 | 0.746 | 0.037 (0/70) | — | 0/4 |

Read the two Goodfire columns separately. The pre-registered metric is the **pooled** threshold,
picked on all 358 DEV negatives, which are dominated by `terminal-wrench` and `cheatbench`; it sets
`J3`'s bar at 89/100 and therefore hides most of its Goodfire skill. The **own-domain** column picks
the threshold at 5% FPR *within* Goodfire's 70 clean runs; it is a diagnostic, not the pre-registered
metric, and it shows `J3` catching 22/80 (0.275) against AutoReview's 7/80 (0.087) and run length's
2/80 (0.025).

Stability of `J3` (same 77-run 10% DEV sample, `temperature=0.5, seed=7`): Spearman
$\rho = 0.973$, mean absolute difference 1.71 points, flag agreement 74/76 = 97.4%
(Wilson 95% CI [90.9%, 99.3%]) at its 89 threshold — as stable as the Flash judges.

Calibration does not transfer between families: at `J2b`'s frozen threshold of 20, `J3` fires on
44/341 (12.9%) pooled DEV negatives, so an ensemble of the two families needs its own threshold
(the `mean`/`max` rows above use the DEV-derived one).

### Held-out transfer (11 sets, 3,321 runs, 100% coverage, exploratory)

| Signal | DEV thr | Pooled held-out AUROC | Held-out R@5%FPR (FPR) | Beats length |
|---|---:|---:|---:|---|
| `J2b` frozen (flash) | 20 | 0.799 [0.754, 0.844] | 0.657 (0.093) | yes (+0.190) |
| `J3` terra (`j3_actions_task_diff_terra_full`) | 89 | **0.895 [0.859, 0.926]** | 0.369 (0.041) | yes (+0.284) |
| mean(`J2b`, `J3` terra) | 49 | **0.897 [0.862, 0.929]** | **0.613** (0.082) | yes (+0.287) |

Paired cluster bootstrap on the 1,448 runs both signals score: `J3` − `J2b` = **+0.095
[+0.061, +0.128]**. `J3` improves AUROC on 9 of the 11 sets and ties on 2: the largest gains are
`latent-agent-sandbox-episodes` (0.960 vs 0.712), `caught-not-taught` (0.995 vs 0.756) and
`flake-laundering` (0.858 vs 0.440). Its lower held-out recall is a threshold artifact — its DEV
threshold lands at 4.1% held-out FPR — not a ranking loss; this lane does not re-tune on held-out.

## Operating points at matched false-alarm budgets

The pre-registered headline fixes one threshold per judge (5% FPR on pooled DEV negatives) and reads
Goodfire recall off it. `terminal-wrench` (146) and `cheatbench` (63) dominate those 358 negatives,
which is why the second family's bar lands at 89/100. `results/operating_points.json` sweeps the
budget instead, choosing each threshold on the scored set's own DEV rows, so the ranking question is
separated from the calibration question:

| Judge | GF AUROC | rec/FPR at 1% | 2.5% | 5% | 10% | 20% |
|---|---:|---|---|---|---|---|
| `J0` AutoReview | 0.543 | 0.05 / 0% | 0.11 / 1.4% | 0.11 / 1.4% | 0.11 / 1.4% | 0.11 / 1.4% |
| `J1` actions only | 0.537 | 0.07 / 0% | 0.07 / 0% | 0.07 / 0% | 0.07 / 0% | 0.07 / 0% |
| `J2b` frozen (flash) | 0.544 | 0.09 / 0% | 0.09 / 0% | 0.09 / 0% | 0.09 / 0% | 0.09 / 0% |
| **`J3` terra** | **0.778** | **0.20 / 0%** | 0.20 / 0% | 0.31 / 2.9% | **0.62 / 7.1%** | 0.64 / 11.4% |
| mean(`J2b`, `J3`) | 0.778 | **0.21 / 0%** | 0.21 / 0% | 0.33 / 2.9% | 0.62 / 7.1% | 0.64 / 11.4% |
| `length_chars` | 0.802 | 0.03 / 0% | 0.19 / 1.4% | 0.38 / 4.3% | 0.41 / 10.0% | 0.61 / 20.0% |
| `length_steps` | 0.746 | 0.11 / 0% | 0.35 / 1.4% | 0.36 / 4.3% | 0.36 / 4.3% | 0.47 / 18.6% |

Three things this makes precise:

1. **At a tight budget the action judges win by 2-4x.** At 1% (0 false alarms in 70) `J3` catches
   16/80 = 0.20 against AutoReview's 4/80 = 0.05 and `length_chars`'s 2/80 = 0.03.
2. **AutoReview is saturated; the action judges are not.** `J0` scores exactly 0 on **71 of 80**
   Goodfire positives, so any threshold above 0 buys at most 9/80 = 0.113 and also fires on the one
   clean run that scores 15; raising the budget from 2.5% to 20% therefore changes nothing. `J3`
   climbs from 0.20 to 0.64 over the same range because its positive tail is graded (99, 99, 95, 90,
   90, ... against a clean maximum of 10).
3. **Run length is still the best at 5% and 10% on this metric, and this lane does not hide it.**
   `length_chars` reaches 0.38 at 5% where `J3` reaches 0.31, so H1's "beat length on Goodfire
   recall@5%FPR" stays failed even after the second family. Length reaches 0.61 only after spending
   20% of the false-alarm budget, and it is a single scalar with no mechanism, no evidence steps and a
   documented confound (§1): ImpossibleBench retry loops, not cheating.

Held-out recall at those same DEV thresholds, for the eleven scored sets (`n = 1,450`):
mean(`J2b`, `J3`) 0.317 / 0.570 / 0.613 / 0.666 / 0.846; `J3` 0.233 / 0.233 / 0.369 / 0.625 / 0.887;
frozen `J2b` 0.372 / 0.372 / 0.657 / 0.674 / 0.674; `length_chars` 0.000 / 0.000 / 0.081 / 0.099 / 0.238.
`J0` and `J1` have no held-out rows at all, so no held-out comparison against the deployed reader is
possible from this lane's data.

## How to Read This Directory
- `PREREG.md`: Pre-registration written and committed (`39715b9`, sha256 `3e4bb28366bc65b00e435178a6f8b6ac54e770e14179649370ed87aa5ecaf9a8`) before DEV scoring.
- `FREEZE.md`: Exact code, rubric, model, and threshold hashes committed (`88c8be063`) before any held-out run was scored.
- `gateway.py`: $0 subscription caller over `omp auth-gateway stdio` with per-call served-model attestation (`served != requested` is rejected and logged).
- `views.py`: Normalized DetBench view loader and `actions_only` window renderer (strips `reasoning_content` and assistant prose; budgets tool args, observations, task text, and final diff).
- `prompts.py`: `ACTIONS_RUBRIC` (`J1`), `J2_ADDENDUM` (`J2`), `V2_ADDENDUM` (`J2b`), and `rubric_sha()`.
- `j0_autoreview.py`: Reuses cached AutoReview readings (380 DEV runs) and runs `evallab.autoreview_monitor.stage1_prompt` on the remaining 300 eligible DEV runs.
- `judge.py`: Async bounded-concurrency windowed judge runner (max suspicion across 60k-char windows).
- `signals.py`: Emits DetBench signal `.jsonl` files and cross-family `mean` / `max` ensembles.
- `stability.py`: Computes paired Spearman rank correlation, mean absolute difference, and 5%-FPR flag agreement with Wilson 95% CI.
- `score_local.py`: Self-contained pure-Python DetBench scorer with cluster bootstrap and rank-normalized combinations.
- `build_summary.py`: Produces record-text-free aggregate JSONs under `results/`.
- `test_judges.py`: Deterministic pytest suite covering view rendering, reply parsing, ensembles, stability, and threshold selection.
- `results/`:
  - `attestation.json`: Per-route and per-batch call counts, served-model attestation, token totals, and worker session JSONL attestation.
  - `detbench_summary.json`: Compact DetBench `score.py` outputs across all DEV and Held-Out sets.
  - `goodfire_diagnostic.json`: Channel breakdown of the 80 Goodfire positive runs and ImpossibleBench `shortcut_signal` strata.
  - `stability_j2_flash.json`, `stability_j2b_flash.json`: Paired 10% DEV stability metrics for the Flash judges.
  - `stability_j3_terra.json`: Paired 10% DEV stability metrics for J3 (`openai-codex/gpt-6.1-terra`).

## Published DetBench signals
`/private/tmp/ov1104/shared/detbench/signals/` holds `j3_actions_task_diff_terra.jsonl`,
`j2b_terra_mean.jsonl`, `j2b_terra_max.jsonl`, `j2_terra_mean.jsonl`, `j2_terra_max.jsonl`,
`j2b_3fam_mean.jsonl` and `j2b_3fam_max.jsonl` beside the earlier `j0`, `j1`, `j2`, `j2b`, `j3`
fable and fable-ensemble files. Regenerate any of them with:

```bash
uv run python research/experiments/ov1104-judges/signals.py \
  --out-dir /private/tmp/ov1104/shared/detbench/signals \
  --runs /private/tmp/ov1104/shared/detbench/runs.jsonl --split dev,heldout \
  --batch j3_terra=/private/tmp/ov1104/judges/out/j2b__openai-codex__gpt-6.1-terra,/private/tmp/ov1104/judges/out/j2b__openai-codex__gpt-6.1-terra__heldout \
  --batch j2b=/private/tmp/ov1104/judges/out/j2b__google-antigravity__gemini-3.8-flash \
  --ensemble j2b_terra_max=j2b+j3_terra:max
```
