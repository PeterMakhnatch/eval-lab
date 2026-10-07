# HAR-193: $0 adaptive finite-campaign replay

`simulate.py` imports production `evallab.campaign_approval.adaptive_band`; it
never runs a model, submits a queue job, deploys anything, or spends money.
It compares fixed M=2/4 with adaptive prefixes of **the same complete sequence**.
The Bernoulli grid is p=0, .05, .25, .5, .75, .95, 1; default 10,000 synthetic
sequences per p and seed 20261007. Each seeded four-draw sequence is reused for
both horizons and all confidence settings. Synthetic p is a draw probability,
**not** a threshold defining the outcome bands.

## Run after integration

From the worktree root (parent owns execution and validation):

```sh
PYTHONPATH=src uv run --no-sync python research/experiments/har193-adaptive/simulate.py \
  --source-root /Users/petermakhnatch/Developer/eval-lab/.worktrees/har164 \
  --confidence .95 --confidence .6
```

Omit `--source-root` after that checkout retires: the same 24 source-bound
outcomes remain embedded in `results.json`. Supplying it verifies all retained
native-result, copy-gate and context-authority hashes and native values; a
missing or changed source refuses instead of silently falling back. The script
replaces the single result table, retains the fixture, and records fixture,
production-policy, script and generated-sequence SHA-256 digests. The initial
artifact is explicitly an **analytical reference, not executed simulation**;
execution changes `artifact_kind` to `executed_offline_simulation`.

## Definitions and provenance

The [HAR-168 card](https://linear.app/petermakhnatch/issue/HAR-168/live-harbor-trials-for-mimoagent-incremental-atif-stream-on-daytona)
closes on 24 selected native cells, not 48: 23 verifier grades plus one NULL
context-exhausted non-pass (scientific-close comment, October 6, 18:33 ET).
Its earlier disposition (17:18 ET) explicitly keeps 002139-a2 in the denominator
without inventing a zero or replacement. The preserved authority artifact is
`runs/har168/attempt2-remaining4-disposition.json`, SHA-256 `b1adb779d34c…`;
source-bound SDK evidence proves 65,686 > 65,536 tokens. Original 002391-a1
503 infrastructure is excluded; authentic `002391-a1-infra-r1` is retained.
The fixture stores full native paths, trial UUIDs and hashes, all raw/gated/null
fields, and the two copy-gate source hashes. Copied raw passes 000792-a1 and
001985-a1 remain visible but are gated non-passes. Total gated passes: 13/24.

[Production's finite-band contract](../../../src/evallab/campaign_approval.py)
is **always = all M gated passes; never = zero; sometimes = mixed**. Applying
that definition to HAR-168's complete two-attempt sequences gives 4 always,
5 sometimes, 3 never. These are finite observed bands, not claims that latent
p is exactly 0 or 1; no previous capability label or prior outcome is reused.

## Savings and error

With Beta(1,1), a homogeneous k-outcome prefix has predictive confidence
`product((k+1+j)/(k+2+j), j=0..M-k-1)` that every remaining outcome matches.
A mixed prefix fixes `sometimes` with confidence 1; the full horizon fixes any
band with confidence 1. Stop only when confidence reaches the target.

At .95, **M=2 saves nothing**, including the HAR-168 replay and HAR-188's x2
campaigns; those campaigns gain admission/ordering, not fewer attempts. M=4
saves two draws when the first two mix, or one when the first two agree and the
third flips. Analytical expected saves are
`2 * 2p(1-p) + 1 * p(1-p) = 5p(1-p)` (maximum 1.25 at p=.5), with zero
classification mismatch because mixed-prefix stops are conclusive.

The result table reports attempts used/saved, early stops, classification
mismatch against the complete finite-M reference, unclassified/truncated counts,
and mean/max **posterior predictive** error `1-confidence` separately from
**empirical** mismatch. At explicit .6, M=2 can stop after one draw with
confidence 2/3: model error 1/3 (<=.4), but fixed-p empirical error can be .5 at
p=.5. The posterior bound is model-dependent, not a uniform frequentist promise.
All simulated/reference sequences are complete; intentionally skipped draws
are early stops, not missing-data truncation.

Campaign configuration uses top-level `attempts_per_task` as the sole maximum;
`adaptive_sampling` contains only `target_confidence` and `seed`. Production
fills existing capacity/budget-clamped waves with next uncertain draws,
followups before unstarted tasks, not serial one-task-at-a-time execution. At
most the active wave is partially informative when the budget stops admission.
This replay measures sampling decisions, not parallel latency or dollar savings.
