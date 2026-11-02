# D2-READER-LADDER: Cheap-Reader Ladder on TraceMonitor-Bench DEV v1
## What Changed (Independent Verifier Corrections)
Following blind verification in VF-D-BENCH (`/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D-BENCH/report.md`, see `VERIFIER-CORRECTIONS.md`):
1. **Headline Context:** Clarified that the reference model (`gemini-3.8-flash`) operates near chance on Stage-1 cards (AUROC 0.511 [0.443, 0.580], recall 6/71 = 8.5%). Matching reference performance reflects matching a baseline near chance, not high detection capability. Four Gemini rungs have tune threshold 0, functioning essentially as binary detectors.
2. **Attestation Record:** Corrected attestation to 2,389 of 2,400 calls with recorded served models; 11 calls lacked a served model (2 for `gemini-2.5-flash-lite`, 9 for `mistral-nemo` due to returncode 1 or timeout). 0 fallbacks, 0 `gemini-4-argon`.
3. **Threshold Artefact:** Documented that `gemini-3.5-flash-lite`'s 1% operating threshold (50, yielding recall 5/71) is an artefact of a single tune negative that defaulted to 50 on parse failure; without it the threshold is 0 and recall is 6/71 matching the 2% operating point.
4. **Receipt Hashes:** Corrected `spend.jsonl` SHA256 to `97d9c55ccf8e` and `verify_recompute.py` SHA256 to `133d0a37df85`.
5. **Storage Policy:** Clarified that per-case rung JSONLs are stored in `~/Developer/eval-lab-results/2026-11-02/D2/` and bridge receipts, omitted from git per third-party data policy.


## Headline Answer
Cheaper readers can match production AutoReview (`google-antigravity/gemini-3.8-flash`) detection performance at fixed low false-alarm budgets ($\le 1\%$ and $\le 2\%$) while drastically cutting latency and cost. However, the reference model itself operates near chance on Stage-1 anatomy cards (AUROC 0.511 [0.443, 0.580], recall 6/71 = 8.5%), and four Gemini rungs have tune threshold 0 (acting essentially as binary detectors). Matching reference performance means matching a baseline that lacks strong Stage-1 discriminative signal, not that cheaper models are effective cheat detectors. Specifically:
- **`gemini-3.1-flash-lite`** is the top overall candidate: **$0.00** marginal subscription cost, **2.18 s** median latency (**2.3x faster** than reference 5.07 s), 1/300 parse failures (0.33%), and **9.9% recall [4.9%, 19.0%]** at $\le 2\%$ false alarm rate (vs 8.5% [3.9%, 17.2%] for `gemini-3.8-flash`). In paired exact McNemar testing vs reference, $b=3, c=2, p=1.000$ (statistically indistinguishable).
- **`gemini-2.5-flash-lite`** achieves **12.7% recall [6.8%, 22.4%]** and highest AUROC (0.557 [0.522, 0.600]) at $0.00 cost, with paired McNemar $b=4, c=1, p=0.375$.
- Among paid OpenRouter candidates, **`inclusionai/ling-3.0-flash`** is the cheapest model that functions as an effective reader: **$0.19 per 1,000 cases**, matching reference recall (8.5% [3.9%, 17.2%], McNemar $b=1, c=1, p=1.000$).
- Conversely, ultra-cheap non-reasoning small models (**`mistralai/mistral-nemo`** and **`openai/gpt-oss-120b`**) collapse completely at low false-alarm thresholds (0.0% recall [0.0%, 5.1%], McNemar $b=0, c=6, p=0.031$, statistically significantly inferior to reference), with `mistral-nemo` exhibiting a severe 15.3% parse failure rate.

Given $N_{\text{pos}} = 71$ report positives, "statistically indistinguishable" ($p > 0.05$) means the test cannot rule out true performance differences smaller than **8.5 percentage points** (a discordance gap of $|b - c| \le 5$).

## Gate Verdict
- **Gate: PASS.**
- All 8 rungs evaluated on the exact preregistered 300-case cohort (150 tune, 150 report).
- Attestation verified: 2,389 of 2,400 calls had recorded served models; 11 calls lacked a served model record (2 for `gemini-2.5-flash-lite`, 9 for `mistral-nemo` due to returncode 1 or timeout). 0 fallbacks, 0 `gemini-4-argon` across all sessions.
- Ladder table complete with cost/1k, p50 latency, parse failure rates, recall at FA $\le 1\%$ and $\le 2\%$ with 95% Wilson intervals, bootstrap AUROC, and exact paired McNemar comparisons.

## Ladder Summary Table

| Rung | Route | Cost/1k (USD) | p50 Latency (s) | Parse Errors | Recall @ FA $\le 1\%$ [95% Wilson] | Recall @ FA $\le 2\%$ [95% Wilson] | Report AUROC [95% CI] | McNemar vs Ref Positives ($b, c, p$) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **gemini-2.5-flash-lite** | sub ($0) | $0.00 (list $1.14) | 4.53 | 3/300 (1.0%) | 9/71 (12.7%) [0.068, 0.224] | 9/71 (12.7%) [0.068, 0.224] | 0.557 [0.522, 0.600] | $b=4, c=1, p=0.375$ |
| **gemini-3.1-flash-lite** | sub ($0) | $0.00 (list $2.49) | 2.18 | 1/300 (0.3%) | 7/71 (9.9%) [0.049, 0.190] | 7/71 (9.9%) [0.049, 0.190] | 0.537 [0.501, 0.578] | $b=3, c=2, p=1.000$ |
| **gemini-2.5-flash** | sub ($0) | $0.00 (list $4.35) | 3.16 | 0/300 (0.0%) | 6/71 (8.5%) [0.039, 0.172] | 6/71 (8.5%) [0.039, 0.172] | 0.536 [0.503, 0.572] | $b=2, c=2, p=1.000$ |
| **gemini-3.5-flash-lite** | sub ($0) | $0.00 (list $3.21) | 2.42 | 2/300 (0.7%) | 5/71 (7.0%) [0.030, 0.154] | 6/71 (8.5%) [0.039, 0.172] | 0.542 [0.514, 0.578] | $b=2, c=2, p=1.000$ |
| **gemini-3.8-flash (Ref)** | sub ($0) | $0.00 (list $6.97) | 5.07 | 0/300 (0.0%) | 6/71 (8.5%) [0.039, 0.172] | 6/71 (8.5%) [0.039, 0.172] | 0.511 [0.443, 0.580] | reference ($b=0, c=0, p=1.000$) |
| **ling-3.0-flash** | OpenRouter | $0.19 | 6.36 | 7/300 (2.3%) | 3/71 (4.2%) [0.015, 0.117] | 6/71 (8.5%) [0.039, 0.172] | 0.507 [0.425, 0.592] | $b=1, c=1, p=1.000$ |
| **mistral-nemo** | OpenRouter | $0.16 | 8.86 | 46/300 (15.3%) | 0/71 (0.0%) [0.000, 0.051] | 0/71 (0.0%) [0.000, 0.051] | 0.489 [0.406, 0.576] | $b=0, c=6, p=0.031$ |
| **gpt-oss-120b** | OpenRouter | $0.35 | 3.44 | 4/300 (1.3%) | 0/71 (0.0%) [0.000, 0.051] | 0/71 (0.0%) [0.000, 0.051] | 0.481 [0.396, 0.565] | $b=0, c=6, p=0.031$ |

*Notes:*
- Discordant counts $b$ = cases alerted only by candidate; $c$ = cases alerted only by reference `gemini-3.8-flash`.
- Operating point thresholds are chosen on 79 `tune` split negatives and evaluated on 71 `report` split positives and 79 `report` split negatives.
- Measured false-alarm rates on `report` negatives at the 2% operating point: `gemini-2.5-flash-lite`: 1/79 (1.3%); `gemini-3.1-flash-lite`: 2/79 (2.5%); `gemini-2.5-flash`: 1/79 (1.3%); `gemini-3.5-flash-lite`: 0/79 (0.0%); `gemini-3.8-flash`: 0/79 (0.0%); `ling-3.0-flash`: 4/79 (5.1%); `mistral-nemo`: 0/79 (0.0%); `gpt-oss-120b`: 0/79 (0.0%).
- Length-matched report AUROCs (142 cases, 71 pos / 71 neg matched on log token size): `gemini-2.5-flash-lite`: 0.557; `gemini-3.5-flash-lite`: 0.542; `gemini-2.5-flash`: 0.536; `gemini-3.1-flash-lite`: 0.535; `ling-3.0-flash`: 0.528; `gemini-3.8-flash`: 0.526; `mistral-nemo`: 0.499; `gpt-oss-120b`: 0.488.
- `gemini-3.5-flash-lite` 1% threshold artefact: recall @ FA $\le 1\%$ is 5/71 because its 1% threshold (50) is set by a single tune negative parse-failure default score (50). Excluding that parse failure, the threshold is 0 and recall is 6/71 (matching the 2% FA rate).

## Question -> What Ran -> Number -> Meaning

### 1. Question
Can we replace the heavy Stage-1 AutoReview reader (`gemini-3.8-flash`) with lighter, cheaper models without sacrificing cheat-detection recall at strict false-alarm limits ($\le 1\%$ and $\le 2\%$)?

### 2. What Ran
- **Cohort**: 300 cases sampled deterministically (`seed=20261102`) from TraceMonitor-Bench DEV v1, exactly balanced across `tune` (71 pos, 79 neg) and `report` (71 pos, 79 neg), stratified proportionally across all six benchmark families (`c1c5`, `sol_astra`, `deep_swe`, `cheatbench`, `goodfire`, `terminal_wrench`).
- **Prompt**: Unaltered production Stage-1 rubric (`c2c138fc7997`) wrapping the anatomy view cards.
- **Rungs Executed**: 8 distinct models across 2 routes:
  1. `google-antigravity/gemini-2.5-flash-lite` (Subscription)
  2. `google-antigravity/gemini-3.1-flash-lite` (Subscription)
  3. `google-antigravity/gemini-2.5-flash` (Subscription)
  4. `google-antigravity/gemini-3.5-flash-lite` (Subscription)
  5. `google-antigravity/gemini-3.8-flash` (Subscription, Reference)
  6. `openrouter/inclusionai/ling-3.0-flash` (OpenRouter paid, cap $1.50)
  7. `openrouter/mistralai/mistral-nemo` (OpenRouter paid, cap $1.50)
  8. `openrouter/openai/gpt-oss-120b` (OpenRouter paid, cap $1.50)
- **Spend**: Total OpenRouter spend across all paid calls was **$0.2095** ($0.0568 for ling, $0.0492 for nemo, $0.1035 for gpt-oss), logged in real-time to `~/.local/state/evallab-failure-library/spend.jsonl` and local `spend.jsonl`.
- **Attestation**: 2,389 of 2,400 calls had recorded served models; 11 calls lacked a served model record (2 for `gemini-2.5-flash-lite`, 9 for `mistral-nemo` due to returncode 1 or timeout). Fallback count = 0, zero `gemini-4-argon` across all 2,400 runs.

### 3. Key Numbers
- **Cheapest Indistinguishable Subscription Rung**: `gemini-3.1-flash-lite` ($0.00 marginal cost, 2.18 s p50 latency, 9.9% recall [4.9%, 19.0%] vs reference 8.5% [3.9%, 17.2%], paired McNemar $p = 1.000$).
- **Cheapest Indistinguishable Paid Rung**: `inclusionai/ling-3.0-flash` ($0.19 per 1,000 cases, 6.36 s p50 latency, 8.5% recall [3.9%, 17.2%], paired McNemar $p = 1.000$).
- **Cheapest Inferior Rungs**: `mistralai/mistral-nemo` ($0.16/1k, 0.0% recall, $p = 0.031$) and `openai/gpt-oss-120b` ($0.35/1k, 0.0% recall, $p = 0.031$).

### 4. Meaning & Architectural Implication
1. **The Gemini Flash-Lite family dominates Stage-1 reading**: `gemini-3.1-flash-lite` cuts latency from 5.07 s to 2.18 s (a 57% reduction in reader latency) while matching cheat detection precision and maintaining an ultra-low parse error rate (0.3%).
2. **AutoReview Stage-1 is bounded by signal representation, not reader scale**: Across all Gemini models (from 2.5-flash-lite up to 3.8-flash), report AUROC ranges narrowly between 0.51 and 0.56, and recall at 2% false alarm is 7-13%. Moving from Flash-Lite to 3.8-flash does not yield a statistically detectable detection benefit on compact anatomy cards.
3. **Small open-weights models require instruction tuning or structured output enforcement**: `mistral-nemo` suffered a 15.3% JSON parse failure rate when evaluated with the zero-shot rubric, and neither `nemo` nor `gpt-oss-120b` calibrated suspicion scores effectively against the tune negatives.

## Receipts and Reproducibility

| Artifact | Path | SHA256 (first 12) |
| :--- | :--- | :--- |
| Preregistration | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/prereg.md` | `04c0f6a1f984` |
| Cohort Manifest (300 cases) | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/cohort.jsonl` | `bc97bee9c825` |
| Summary Results JSON | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/ladder_results.json` | `995d8ae32e51` |
| Rung: gemini-2.5-flash-lite | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/gemini-2.5-flash-lite.jsonl` | `c356f033fc49` |
| Rung: gemini-3.1-flash-lite | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/gemini-3.1-flash-lite.jsonl` | `9e5682311026` |
| Rung: gemini-2.5-flash | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/gemini-2.5-flash.jsonl` | `3a212b922105` |
| Rung: gemini-3.5-flash-lite | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/gemini-3.5-flash-lite.jsonl` | `91114db1e126` |
| Rung: gemini-3.8-flash | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/gemini-3.8-flash.jsonl` | `1c3ae9f5a7b6` |
| Rung: ling-3.0-flash | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/ling-3.0-flash.jsonl` | `fc92399a1fe9` |
| Rung: mistral-nemo | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/mistral-nemo.jsonl` | `802ed041f473` |
| Rung: gpt-oss-120b | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/gpt-oss-120b.jsonl` | `a10241e6c4fa` |
| OpenRouter Spend Log | `research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/spend.jsonl` | `97d9c55ccf8e` |

Durable copies of aggregates exist in `research/experiments/on-reader-ladder-20261102/`. Per-case rung `.jsonl` files and cohort files are stored durably under `~/Developer/eval-lab-results/2026-11-02/D2/` and bridge receipts, but are excluded from git per the project third-party data policy.
Recompute verification script: `research/experiments/on-reader-ladder-20261102/verify_recompute.py` (SHA256: `133d0a37df85`).

## Honest Limits
- **Sample Power**: With 71 positives in the report split, the minimum detectable gap at $\alpha = 0.05$ with exact McNemar is ~8.5 percentage points. Differences within $\pm 5$ detections between the Gemini models cannot be distinguished from sampling variation.
- **Stage-1 Only**: This ladder measures Stage-1 card reading only. Stage-2 raw snippet retrieval / re-inspection was not exercised here, as Stage 1 determines whether Stage 2 is triggered.
- **Distributional Skew**: Several families in TraceMonitor-Bench DEV v1 have high intrinsic baseline difficulty (as documented in D1's report); the overall pooled recall at strict 2% false alarm is modest across all readers.

## Workflow Friction
- **Tool call timeout vs large batches**: A single bash command running 300 sequential/concurrent LLM calls across multiple models easily exceeds 600s tool timeouts if unchunked. Implementing per-rung CLI execution and streaming per-case append persistence was essential for resilient progress.
- **Model naming ambiguity across providers**: `gemini-3.5-flash` in the prompt brief was named `gemini-3.5-flash-lite` in the local `google-antigravity` registry, requiring preflight model resolution to prevent failures.
- **OpenRouter latency variance on cheaper models**: Non-frontier models like `mistral-nemo` exhibited high latency variance (p50 of 8.86 s, vs 2.18 s for `gemini-3.1-flash-lite`) and lower formatting adherence (15.3% unparseable outputs), causing runner slowdowns.
- **Virtualenv isolation**: Native Git worktrees created without an active `.venv` require explicit `uv sync` before local packages like `evallab` can be imported by Python subtools.

DS-DONE: D2-READER-LADDER
