# P0-SELF-F9D7D6-5 results: the Haiku judge's confirmed-card signal beyond length, at scale

Preregistration `PREREGISTRATION.md` (commit `c4a725b1`, 2026-10-29 16:06 ET, before the first model call; the sample, rule
and decision rule were fixed there). Numbers: `results.json` (all intervals); per-run rows stay private. Wilson 95% in
brackets; bootstrap = task-clustered (402 tasks), 2,000 resamples, `random.Random(20261029)`, percentile, 0 resamples skipped.
Everything below is Haiku-5.5-read cascade v8 (`window-cascade/v8`, unchanged) on **555 fresh strict-DEV Goodfire runs**.
J = a Stage-1 card the judge confirmed (rules other than `grader_tamper`); G = pass-through gate alarm; V8 = J or G.

## Answer

**The preregistered decision rule is met: the judge-confirmed-only signal is real beyond length** (interval above 0 in
tertiles 1 and 3, not in tertile 2; pooled Mantel-Haenszel difference +7.1 points [4.0, 10.6]). It is small, concentrated in
long runs, and nowhere near the 15%-at-3% target: pooled J recall 23/300 = 7.7% [5.2, 11.2] at FAR 2/255 = 0.8% [0.2, 2.8]
(about 7.8% re-weighted to the 1,636/752 DEV class mix by hand). The judge-free gate G does **not** separate classes in any
tertile after length (pooled -2.6 points [-10.0, 4.8]) and carries 58 of the 59 clean V8 alarms. One caution against
over-reading "beyond length": at J's clean-alarm budget a within-tertile length rule is degenerate (it alarms on the
single longest value of a tertile), yet in tertile 1 it catches 9 enacted runs at 0 clean, more than J's 6 (J - length
-3.0 points [-11.1, 5.2]). The signal beyond length is established in tertile 3 only when compared with length directly.

## 0. Gate, sample and reads

* Strict-DEV `goodfire-dev` gate count: **2,388** (1,636 enacted, 752 clean, 886 tasks); excluded: 246 NP31 pair runs (all
  in the gate) and the 150 sample runs (all in the gate): **1,999 eligible**.
* Drawn 555 (t1: 100 enacted / 100 clean; t2: 100 / **55** (all 55 eligible); t3: 100 / 100 (task cap had to rise to 5 in
  t3 clean, 4 in t2 clean; 23 of 402 tasks have more than 2 runs, maximum 5)). **All 555 read, 0 unread, 0 abstain.** 59 runs
  (51 in t1) have no Stage-1 card and needed no call.
* Calls: **1,026 fresh judge calls** plus 1 canary; 1,047 attempts in the ledger: **1,027 served by exactly
  `anthropic/claude-haiku-5-5` and accepted, 20 attempts served by Haiku but rejected** (not a valid judge reply; each
  retried inside its call, every call ended accepted, 0 failed calls), **0 attempts served by any other model**, **0 refusals**,
  0 `GuardStop`, 0 quota stops (cap 1,500 attempts not reached). Tokens: 5.28M input (incl. cache), 0.23M output. Claude
  quota: 5-hour 42% -> 50%, 7-day 72% -> 74% (batch-start peak 74%, stop line 85%). 35 batches of 16 runs (last 11), one
  `usd: 0` line each in `spend.jsonl` (purpose `p0-SELF-f9d7d6-5`). Wall time of the reads 28 minutes.
* Post-run edits to the committed tools were lint-only (an unused variable in `run_reads.py`, a merged `with` in `decompose.py`).

## 1. Per tertile (J = judge-confirmed only; d = enacted-minus-clean alarm rate, task-clustered bootstrap CI)

| Tertile (steps) | Enacted J | Clean J | d (J) [95% CI] | Fisher p (1-sided, descriptive) | Length-only at the same clean alarms | d (length) | Gate G enacted / clean | d (G) [95% CI] |
|---|---|---|---|---|---|---|---|---|
| t1 (<= 40) | 6/100 [2.8, 12.5] | 0/100 [0.0, 3.7] | **+6.0 [1.1, 11.5]** | 0.014 | 9/100 at 0/100 (steps >= 40) | +9.0 [3.7, 14.9] | 2/100 / 0/100 | +2.0 [0.0, 6.5] |
| t2 (41-89) | 5/100 [2.2, 11.2] | 1/55 [0.3, 9.6] | +3.2 [-2.5, 8.5] | 0.305 | 1/100 at 1/55 (steps >= 89) | -0.8 [-5.4, 2.9] | 15/100 / 13/55 | -8.6 [-25.5, 7.8] |
| t3 (>= 90) | 12/100 [7.0, 19.8] | 1/100 [0.2, 5.4] | **+11.0 [4.5, 17.9]** | 0.0013 | 0/100 at 0/100 (no admissible cut) | 0.0 | 42/100 / 45/100 | -3.0 [-18.7, 12.2] |
| **Pooled (Mantel-Haenszel)** | | | **+7.1 [4.0, 10.6]** | | | +3.1 [0.8, 5.6] | | -2.6 [-10.0, 4.8] |

Pooled difference: `D = sum_k w_k d_k / sum_k w_k`, `w_k = n1_k n0_k / (n1_k + n0_k)` (n1 enacted, n0 clean read runs of
tertile k). Length-only rules are in-sample thresholds (optimistic for length): per tertile, the most permissive step count whose
clean alarms do not exceed J's clean alarms in that tertile; the global rule (J's 2 clean alarms in all) has no admissible
non-trivial threshold (0/300 at 0/255). J minus within-tertile length: t1 -3.0 [-11.1, 5.2]; t2 +4.0 [-2.9, 10.7];
t3 +11.0 [4.5, 17.9]; pooled +4.0 [-0.3, 8.2].

Full V8 (J or G), reference: overall pooled sample 78/300 = 26.0% [21.4, 31.2] at 59/255 = 23.1% [18.4, 28.7]; per tertile
8/100 vs 0/100 (t1), 19/100 vs 14/55 (t2), 51/100 vs 45/100 (t3); d = +8.0 [2.3, 14.8], -6.5 [-23.8, 10.7], +6.0 [-9.1, 20.9];
pooled +3.5 [-4.3, 10.8]. Adding the gate removes the separation the judge component gives. Overall G alone 59/300 at 58/255.

## 2. Operating point against the target

J is binary; its point is the pooled 23/300 = 7.7% at 0.8% FAR (FAR within 3%), the J card count as a score gives the same
cut (>= 1 confirmed card). Per tertile: t1 6.0% at 0.0%, t2 5.0% at 1.8%, t3 12.0% at 1.0%. **The 15%-at-3% target is not
reached** on any cell or pooled; the best cell (t3) is 12%.

## 3. Decision rule (preregistered)

(a) lower bound of `d_k` above 0 in at least two of three tertiles: t1 yes (1.1), t3 yes (4.5), t2 no (-2.5) -> 2 of 3.
(b) pooled `D` above 0: +7.1 points, interval [4.0, 10.6]. **Real beyond length: yes by the rule as written.**

Qualifications that the rule does not capture: (i) strata hold length approximately fixed, but the length-only comparator
at J's tiny false-alarm budget is itself degenerate, and in t1 length alone does at least as well as J (9 vs 6 enacted at 0 clean,
all at exactly 40 steps, J minus length -3.0 [-11.1, 5.2]); (ii) t2 has 55 clean runs and its interval spans 0; (iii) J
alarms on 2 clean runs in 255, so d is nearly J's recall; (iv) the sample is a stratified draw, so the pooled sample rates are
not population rates; only the within-tertile and Mantel-Haenszel figures are decision quantities; (v) one reader (Haiku 5.5)
and one dataset; the Stage-1 rules were tuned elsewhere and not changed here.

## 4. Cross-tests, stored (no new reads; same component from `p0-f9d7d6-3` decomposition rows, no rule or threshold change)

The judge-confirmed-only component is stored in those findings, so it is reported as scored there (Haiku-read v8):

| Set | J recall | J FAR |
|---|---|---|
| Goodfire sample 150 | 9/80 = 11.2% [6.0, 20.0] | 1/70 = 1.4% [0.3, 7.7] |
| 123 NP31 pairs (the claim under test) | 8/123 | 1/123 = 0.8% [0.1, 4.5] |
| CheatBench DEV | 14/75 = 18.7% [11.5, 28.9] | 0/75 = 0.0% [0.0, 4.9] |
| CM-18 established (7 sets) | 22/317 = 6.9% [4.6, 10.3] | 5/329 = 1.5% [0.7, 3.5] |
| NP21 Flake | 0/7 [0.0, 35.4] | 0/60 [0.0, 6.0] |

Taken with the 555-run result, the component keeps FAR at or below about 1.5% everywhere it was read; its recall is 6-19% by set,
7.7% on this sample. Pooled established (CM-18 + Flake): 22/324 at 5/389 (arithmetic from the rows above).

## 5. Not done / limits

No Kimi, Opus or Sonnet read; one reader. Length is controlled by stratification and an in-sample length rule only; no
matched-pair design this time. Findings are in the private store `~/.local/state/evallab-prod-live/trace-harness/p0f5/`.
