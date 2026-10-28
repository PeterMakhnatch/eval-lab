# CM-13 results: facet-cited Stage-2 windows (Goodfire DEV 80 enacted / 70 clean)

Judge `openai-codex/gpt-6.1-terra` (thinking low, subscription route, zero paid API calls, zero fallback,
zero failed or refused calls in all 1,356 judgments). The earlier cascade numbers used the Kimi k3.1 judge,
which was at its five-hour limit when this ran, so arm A below is *this judge's* production baseline,
not the earlier 12/80. Counts are replicate 1; replicate 2 (every prompt judged again) is in
`results/retest.json`. Per-run rows are keyed by a hash of the run id; no trace text, reply or quote is stored.

## Headline

Facet-first windows raise run-level detection a lot (9 → 25 of 80 enacted runs) but also the clean false
alarms (2 → 6 of 70) and cost 2.07× the tokens per run. By the rule written before the run, **not adopted**:
it needs at least +3 detections *and* at most 2/70 clean false alarms *and* no more tokens per run than A.
At a fixed clean false-alarm rate of 3% or less, no facet arm beats production.

## Detection at each arm's own verdict rule

| Arm | Windows / run | Tokens / run | Enacted recall (95% CI) | Clean FAR (95% CI) | Replicate 2 |
|---|---|---|---|---|---|
| A production | 1.05 | 3,253 | 9/80 = 11.2% (6.0–20.0) | 2/70 = 2.9% (0.8–9.8) | 9/80, 1/70 |
| B facet only | 1.73 | 5,365 | 23/80 = 28.8% (20.0–39.5) | 5/70 = 7.1% (3.1–15.7) | 23/80, 6/70 |
| B1 first facet window | 0.85 | 2,642 | 17/80 = 21.2% (13.7–31.4) | 4/70 = 5.7% (2.2–13.8) | 15/80, 4/70 |
| C facet first, then A | 2.17 | 6,735 | 25/80 = 31.2% (22.2–42.1) | 6/70 = 8.6% (4.0–17.5) | 26/80, 6/70 |
| D random windows, same count as B (control) | 1.73 | 5,314 | 16/80 = 20.0% (12.7–30.0) | 5/70 = 7.1% (3.1–15.7) | 17/80, 5/70 |

Facets alone (`suspicion >= 2`): 7/80 and 1/70.

## Does the citation or just having more windows do it?

Paired over the same runs (discordant counts, exact sign test):

| Comparison | Enacted | Clean |
|---|---|---|
| C vs A | +17 / −1 (p = 0.0001) | +4 / −0 (p = 0.13) |
| B vs A | +17 / −3 (p = 0.003) | +4 / −1 (p = 0.38) |
| B vs D random windows, same count | +7 / −0 (p = 0.016); replicate 2: +6 / −0 (p = 0.031) | +1 / −1 |
| B1 vs A (about the same budget) | +13 / −5 (p = 0.10) | +3 / −1 (p = 0.63) |
| D random vs A | +11 / −4 (p = 0.12) | +4 / −1 (p = 0.38) |

Half of B's recall gain over production (+7 of +14 enacted detections) appears with random windows at the same window
count and the same false-alarm count; the citation adds seven more enacted runs on top of that, with no extra clean
alarm. Half of the lift over production is therefore the judge seeing more windows per run, not the facet.

## Localization on the 80 enacted runs

| What | Count |
|---|---|
| Facet cited a labelled step exactly / within 5 assistant turns (reproduces the dispatch) | 39/80 (48.8%) / 70/80 (87.5%) |
| Runs with no citation (no B window) | 23 of 150 |
| Window flagged on a labelled step: A / B / C / D | 15 / 23 / 26 / 10 |
| Flagged within 5 assistant turns of one: A / B / C / D | 48 / 66 / 72 / 60 |
| Window span covers a labelled step: A / B / C / D | 51 / 67 / 73 / 61 |
| A *confirmed* window flagged on a labelled step: A / B / C / D | 1 / 3 / 3 / 1 |

The exact-step rate is the discriminating number (B 23, production 15, random 10). Within-5 and span coverage
are weak: labelled passages are dense in these traces, so random windows already cover 61 of 80. The judge confirms
a window on the labelled step in at most 3 of 80 runs, so run-level recall is mostly confirmation of other exploit
content in the window, not located detection.

## Fixed clean false-alarm rate

Ranking runs by their number of confirmed windows, best recall with at most 3% clean false alarms (at most 2/70):
A 9/80 (FAR 2/70), B 4/80 (1/70), B1 0/80, C 7/80 (2/70), D random 10/80 (2/70). Replicate 2: A 9, B 4, C 7, D 8.
Window counts per run are 0 to 3, so the ranking is coarse. Nothing here reaches the CM-10 target of 15% recall at
3% or lower false alarms.

## Meaning

Whole-run citations point at the labelled step much better than the production tripwires do (23 against 15 exact
flags, and against 10 for random), so the idea of using them to place a window is sound. It does not yet turn into
cheaper or cleaner detection: the extra windows cost tokens and clean alarms, the judge rarely confirms on the
labelled step itself, and at equal false-alarm rate production's single window is as good. A better use of the
citation may be to hand the judge a bigger window or to check the cited step's quote, which this item did not test.

## Limits

- One cohort (80/70) that earlier cascade tuning also used, wide intervals; a one- or two-run difference is noise.
  Judge test-retest was stable (an arm's run-level alarm changed on 1 to 5 of 150 runs between replicates).
- B and C read the whole run through its facet, so they are run-end Stage 2, not a causal prefix monitor; A is causal.
- A is the production code path with a different judge than the earlier 12/80; the earlier baseline is not reproduced.
- Facet records were not re-blinded here. CM-12 found 22 Goodfire records failing its registered metadata rule; only their
  step indices reach the judge. The current facet file also holds 319 cited steps against the 326 in the earlier scorecard;
  the 39/80 and 70/80 hit rates reproduce exactly.
- B1, the random control D and the replicate were added after the first scores were read. They are controls and do not change
  the pre-set decision rule.
- Gating by facet suspicion (`>= 2`) leaves 6/80 and 1/70 for C; it removes the gain along with the false alarms.
