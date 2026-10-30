# DT-14 — Prompt sensitivity of the evidence reader on short runs

Prereg `prereg.md` (sha256 `e1ef8dca3914`), written before the first DT-14 call. One
reader (`deepseek/deepseek-flash`, toolless, thinking low), one run-text rendering, one
verbatim-verification rule, six instruction blocks. Only the instruction block changes
between arms. Driver: `research/experiments/ds-dt14-prompt-sensitivity-20261030/`
(`dt14_prompt.py` sample/build/read/score, `dt14_env.py` step projections).

**Everything below rests on 182 calls, all served `deepseek/deepseek-flash`, 0
fallback events, 0 unattested calls** (`receipts/attestation.json`), total spend
**$0.8129**.

## Deviations from the prereg (written before the calls, listed here)

1. **Sample size** cut from 60 to 24 runs per corpus per fold (48 per fold) and the
   20-per-corpus transfer set dropped, after pricing the prereg design from the real
   prompt sizes: 600 fold-A calls at the measured $0.0074/call was ≈$2.20 against a
   $1.20 cap. Same deterministic rule otherwise (`sha256(blind_id)` order over runs
   with ≤40 steps, alternating positions into A/B).
2. **Wall clock cut the folds.** Fold A finished on Goodfire (24 runs × 5 variants) and
   on the first 8 CheatBench runs (8 × 5); CheatBench fold A's remaining 16 runs and
   fold B's remaining variants were not run. Fold B got the selected variant on 12
   Goodfire and 12 CheatBench runs.
3. V5's instruction asks for a `category` field, so its output schema differs from the
   other arms by that one field; its alert rule (`category != none`) is the brief's, and
   verified citations are still counted for honesty.

## Q1 — How large is the spread between instruction blocks?

**What ran**: fold A, design-complete set = the 30 runs where all six arms produced an
attested, parseable reading (24 Goodfire + 6 CheatBench; 18 positives / 12 negatives).

```
uv run --project . python research/experiments/ds-dt14-prompt-sensitivity-20261030/dt14_prompt.py score
```

| variant | instruction | recall (pos) | false alarms (neg) | length-matched diff [95% CI] | citation honesty |
|---|---|---|---|---|---|
| V0 | DT-8 block, reused | 8/18 = 44.4% [24.6–66.3] | 0/12 = 0.0% [0.0–24.2] | +0.451 [+0.229, +0.666] | 17/25 = 68.0% |
| V1 | assignment first, cite beyond it | 3/18 = 16.7% [5.8–39.2] | 0/12 | +0.178 [0.000, +0.357] | 6/11 = 54.5% |
| V2 | counterfactual ("no checker?") | 4/18 = 22.2% [9.0–45.2] | 0/12 | +0.222 [+0.052, +0.442] | 6/12 = 50.0% |
| V3 | actions only, never descriptions | 6/18 = 33.3% [16.3–56.2] | 0/12 | +0.334 [+0.118, +0.556] | 11/17 = 64.7% |
| V4 | neutral summary, then verdict | 7/18 = 38.9% [20.3–61.4] | 0/12 | +0.399 [+0.220, +0.613] | 15/20 = 75.0% |
| V5 | required category field | 5/18 = 27.8% [12.5–50.9] | 0/12 | +0.288 [+0.112, +0.496] | 13/18 = 72.2% |

Per-length-tertile recall (V0 → V4), same set: tertile 0 `2/5 → 2/5`, tertile 1
`3/4 → 3/4`, tertile 2 `3/9 → 2/9`; false alarms 0 everywhere except V0's
`0/4, 0/3, 0/5`.

**Number**: recall spread across V0–V5 = **27.8 points** (16.7% to 44.4%); among the
five new blocks alone, V1–V5 span 16.7%–38.9% = **22.2 points**. False-alarm spread =
**0.0 points** (every arm 0/12). Per corpus, fold A: Goodfire 0.0%–38.5% (24 runs,
13 pos), CheatBench 33.3%–52.9% (8 runs, 6 pos; V0 the only arm with a false alarm,
1/7).

**Meaning**: the instruction block is a real lever on recall — a 22-point swing among
tuned variants, larger than most detector gaps we have measured, and free of any
false-alarm cost on these short runs. The two blocks that *add reasoning scaffolding*
(V3 action-only 33.3%, V4 summary-then-verdict 38.9%) recover most of V0's lead; the
two that add an extra discriminator (V1 assignment, V2 counterfactual) cost 20–28
points. V0 itself remains the best single arm on fold A, so the inherited block is not
badly chosen — but V4 lands within 5.5 points of it with a stricter citation habit
(75.0% vs 68.0% verified).

## Q2 — Does the fold-A winner hold on held-out fold B?

**What ran**: fold A selection (rule fixed in the prereg: highest fold-A recall with at
most 2 false alarms on fold-A negatives) ranks V4 first (7/18 at 0/12), then V3, V5,
V2, V1. V4 was then run on 12 Goodfire + 12 CheatBench fold-B runs (24 calls).

| arm (fold B) | recall | false alarms | length-matched diff [CI] | honesty |
|---|---|---|---|---|
| V4 (selected) | 6/10 = 60.0% [31.3–83.2] | 0/14 = 0.0% [0.0–21.5] | +0.635 [+0.458, +0.823] | 13/21 = 61.9% |
| V0 (reused) | 8/21 = 38.1% [20.8–59.1] | 2/27 = 7.4% [2.1–23.4] | +0.344 [+0.126, +0.562] | 26/35 = 74.3% |

Paired, same runs, exact McNemar (`receipts/result.json` → `v0_vs_variant.pooled-B`):
positives V0-only 1 vs V4-only 2 (n = 10), **p = 1.0**; negatives V0-only 1 vs V4-only 0
(n = 14), **p = 1.0**. By corpus: Goodfire B V4 2/3 at 0/9 vs V0 0/6 at 0/18; CheatBench
B V4 4/7 at 0/5 vs V0 8/15 at 2/9.

**Number**: V4's fold-B recall advantage is +21.9 points pooled, but the discordant
pairs are 2 vs 1 and the interval on the difference includes 0 (p = 1.0). V4 removes
both of V0's fold-B false alarms (2/27 → 0/14), also not significant.

**Meaning**: no. On held-out runs the selected variant does not beat V0 by an interval
that excludes 0. The honest summary is that the spread is large on the tuning fold, and
the fold-B evidence is consistent with a small V4 gain and a real specificity gain, but
cannot separate them from noise at n = 10 positives.

## Q3 — Cost, and what the arms cost per run

182 calls, $0.8129 total. Goodfire $0.0051–0.0058/run; CheatBench $0.0016–0.0026/run
(median short-run prompt 26k input tokens Goodfire, 6k CheatBench; 512k output tokens,
thinking included, over all arms). No prefix-cache reuse of the shared run text was
observed (`cacheRead` stayed at the fixed 4608-token system prompt), so six variants of
one run cost six full prompts.

## Evidence

| receipt | sha256[:12] |
|---|---|
| `prereg.md` | `e1ef8dca3914` |
| `receipts/variants.json` | `bf3d81b4f3ba` |
| `receipts/result.json` | `511c27d6aecf` |
| `receipts/attestation.json` | `9e572058c48d` |
| `receipts/readings-v1.jsonl` … `readings-v5.jsonl` | ids, verdicts, categories, n_verified only |

## Not done / limits

- **n is small**: 18 positives on fold A's design-complete set, 10 on fold B. The 22–28
  point spread is far outside sampling noise in direction and size but its own interval
  is wide; treat it as "the prompt matters, by roughly 1.5–2.5 positives out of 18".
- **CheatBench fold A covers 8 of 24 runs, fold B 12 of 24**; the transfer set
  (>40 steps) was not run at all, so nothing here says how the blocks behave on long
  runs, where the length confound dominates.
- **V5's alert rule differs** (category, not verified citation), so its recall is not
  strictly comparable with V0–V4; its verified-citation count is reported alongside.
- **One reader, one temperature, one corpus pair.** Labels are the publishers'
  consensus. No false alarm was adjudicated.
- Two scoring bugs were found and fixed before any number was reported: cached step ids
  were strings (every citation failed verification), and the attestation summary
  compared a set to a list. Both fixed and re-scored from the stored outputs; no call
  was re-run.

DS-DONE: DT-14
