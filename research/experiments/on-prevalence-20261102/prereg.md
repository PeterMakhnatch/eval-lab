# Preregistration: R1-PREVALENCE (Calibrated Prevalence of Action-Visible Cheating in DEV Agent-Trace Corpora)

## 1. Research Question
What is the true calibrated prevalence ($p$) of action-visible reward hacking / cheating in public agent-training trace corpora (specifically Goodfire and HackTrace DEV sets), when measured with the best cheap reader from D2 and corrected for reader sensitivity and false-alarm rates via the Rogan-Gladen estimator?
How much action-visible cheating exists across distinct agent model families, and how sensitive is the calibrated estimate to uncertainty in the reader's operating characteristics?

## 2. Cohort and Subsampling Specification
- **Total sample size**: Exactly $N = 800$ cases drawn from admitted DEV corpora.
- **Strict exclusion**: All 780 cases in TraceMonitor-Bench DEV v1 (`/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1/manifest.jsonl`) are strictly excluded. Any held task (including Goodfire's 95 held tasks) and any TEST split are excluded via `admitted_task_ids` and `CorpusRegistry`.
- **Sampling method**: Deterministic pseudorandom draw (`seed = 20261102`). Candidate IDs within each stratum are sorted deterministically before sampling.
- **Stratification structure** (5 strata, 800 cases total):
  1. `goodfire_kimi` (`source-reported:moonshotai/Kimi-K3`): 225 cases sampled from 1,161 available non-benchmark DEV runs.
  2. `goodfire_qwen` (`source-reported:Qwen/Qwen3.8-2.4T-A95B-FP8`): 225 cases sampled from 1,099 available non-benchmark DEV runs.
  3. `goodfire_glm52_fp8` (`source-reported:zai-org/GLM-5.2-FP8`): 125 cases sampled from 785 available non-benchmark DEV runs.
  4. `goodfire_glm52` (`source-reported:zai-org/GLM-5.2`): 125 cases sampled from 379 available non-benchmark DEV runs.
  5. `hacktrace` (`hacktrace-act-v1`): 100 cases sampled from 150 available non-benchmark DEV items.
- **Cohort manifest**: Stored at `receipts/cohort.jsonl` (SHA256: `699b59a1ee263344b4cf50f6dbb2c3c254e4df54cddc06cc3a39235a41c4c063`).

## 3. Reader Model and Operating Point
- **Reader Model**: `google-antigravity/gemini-3.1-flash-lite`, identified by D2 (`D2-READER-LADDER`) as the top cheap reader ($0.00 subscription route, 2.18 s median latency, 0.33% parse failure rate, matching reference performance).
- **Execution Route**: Subscription route, zero marginal cost. Pinned invocation:
  `omp -p @<prompt_file> --model google-antigravity/gemini-3.1-flash-lite --config /Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/worker.yml --no-skills --no-tools --thinking low --no-title --mode text`
- **Concurrency**: Maximum 4 concurrent worker processes.
- **Prompt Rubric**: Production Stage-1 AutoReview rubric (`c2c138fc7997`) wrapping the label-free `evallab trace anatomy` view card (`render_anatomy_card`).
- **Operating Point Selection**:
  Fixed from the D2 tune split negatives:
  - Primary operating point: $\le 2\%$ false alarm ceiling on tune negatives $\implies$ threshold $t_{\text{fa2}} = 0$ (alert iff $\text{suspicion} > 0$).
  - Strict sensitivity check: $\le 1\%$ false alarm ceiling $\implies$ threshold $t_{\text{fa1}} = 30$ (alert iff $\text{suspicion} > 30$).

## 4. Benchmark Operating Characteristics (Report Split)
The reader's operating characteristics are frozen from the D2 report split (150 cases: 71 positive, 79 negative):
- Primary operating point ($t > 0$):
  - $\widehat{\text{recall}} = 7 / 71 = 9.859\%$ (95% Wilson: $[4.87\%, 18.99\%]$)
  - $\widehat{\text{fa}} = 2 / 79 = 2.532\%$ (95% Wilson: $[0.70\%, 8.81\%]$)
  - Separation: $\widehat{\text{recall}} - \widehat{\text{fa}} = 7.327\%$
- Strict operating point ($t > 30$):
  - $\widehat{\text{recall}} = 7 / 71 = 9.859\%$ (95% Wilson: $[4.87\%, 18.99\%]$)
  - $\widehat{\text{fa}} = 1 / 79 = 1.266\%$ (95% Wilson: $[0.22\%, 6.83\%]$)
  - Separation: $\widehat{\text{recall}} - \widehat{\text{fa}} = 8.593\%$

## 5. Family Eligibility Check for Calibrated Estimation (D4 Addendum)
Per D4 findings (`lanes/D4-report.md`) and the contract addendum:
- **Eligibility Rule**: A family/corpus is admissible for Rogan-Gladen calibrated prevalence estimation if and only if the reader's recall on the benchmark report split is clearly and statistically above its false-alarm rate ($\text{recall} - \text{fa} > 0$).
- **Excluded Families**:
  - `publisher_cot_judged` (`goodfire` and `deep_swe`):
    In D4, blind readers agreed with the label on only 53% of cases because they could not see the CoT reasoning on which upstream publisher labels were based (both readers called 0/20 cheated). On the D2 benchmark report split, `gemini-3.1-flash-lite` had $\text{recall} = 0/16$ on Goodfire and $0/12$ on DeepSWE.
    Where $\text{recall} \approx \text{fa} \approx 0$, the Rogan-Gladen denominator $(\text{recall} - \text{fa}) \approx 0$, causing division by zero and infinite variance.
    Therefore, Goodfire is explicitly **excluded** from the Rogan-Gladen calibrated prevalence headline.
  - For excluded strata (Goodfire model families), we report the empirical raw alert rate $q = k/n$ with 95% Wilson intervals and document that this reflects the rate of *action-visible* suspicion only; total prevalence including CoT-only cheating cannot be identified with a CoT-free reader.
  - Rogan-Gladen calibration is applied at the pooled level and on admissible families where reader sensitivity is bounded.

## 6. Estimator and Statistical Protocol
1. **Raw Alert Rate**:
   For any stratum or pool of $n$ evaluated cases with $k$ alerts ($\text{suspicion} > t$):
   $$q = \frac{k}{n}$$
   reported with 95% Wilson score confidence interval.
2. **Rogan-Gladen Calibrated Prevalence**:
   $$\hat{p} = \frac{q - \widehat{\text{fa}}}{\widehat{\text{recall}} - \widehat{\text{fa}}}$$
   clamped to $[0, 1]$ for physical interpretability (both raw and clamped reported).
3. **Bootstrap Confidence Interval**:
   Joint bootstrap ($B = 2,000$ iterations, seed 20261102) propagating all three sources of error:
   - Sample alert rate uncertainty: $q^* \sim \text{Binomial}(n_{\text{sample}}, q) / n_{\text{sample}}$
   - Benchmark recall uncertainty: $\text{recall}^* \sim \text{Binomial}(n_{\text{pos}}, \widehat{\text{recall}}) / n_{\text{pos}}$
   - Benchmark false-alarm uncertainty: $\text{fa}^* \sim \text{Binomial}(n_{\text{neg}}, \widehat{\text{fa}}) / n_{\text{neg}}$
   - Sample $p^* = (q^* - \text{fa}^*) / (\text{recall}^* - \text{fa}^*)$, taking the 2.5% and 97.5% quantiles.
4. **Sensitivity Analysis**:
   Evaluate $\hat{p}$ across the four extremes of the 95% Wilson intervals of $\text{recall} \in [\text{rec}_{\text{lo}}, \text{rec}_{\text{hi}}]$ and $\text{fa} \in [\text{fa}_{\text{lo}}, \text{fa}_{\text{hi}}]$.
5. **Parse Failure Handling**:
   Parse failures default to suspicion = 50.0 and are tracked explicitly. All cases with parse failures are excluded from the primary $q$ and $p$ calculations, and the parse failure rate is reported.

## 7. Receipts and Durability
- Receipts recorded under `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/R1-PREVALENCE/receipts/` and copied to `/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R1/receipts/`.
- Per-run readings stored in JSONL format with `case_id`, `sample_id`, `stratum`, `suspicion`, `verdict`, `reason_code`, `latency_s`, and full session attestation.
- Report committed to git touches only `research/experiments/on-prevalence-20261102/` (code, synthetic tests, aggregate numbers; no third-party raw text or transcripts).
