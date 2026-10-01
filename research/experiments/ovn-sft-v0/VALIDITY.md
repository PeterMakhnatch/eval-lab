# HAR-133 validity verdict: valid with caveats

**No detectable difference for either method at this size.** The five-trajectory,
ten-step SFT tested the process, not the method. Both blind G6 raters judged
the tuned arm's single counted pass copied; the deterministic counter could
not bind the unpack/read evidence. Neither the canonical primary analysis nor
the separately labelled judgment sensitivity establishes improvement.

## Result

The closed cohort contains 60 original attempts: 55 counted outcomes and five
infra-missing cells, with **no replacements**. Each primary comparison uses
its own 16/20 complete pairs and the frozen two-test Holm family.

| Intervention minus stock | Complete-pair passes | Difference | Exact / Holm p | Sharp planned-20 bounds |
|---|---|---:|---:|---:|
| LoRA-SFT | 1/16 versus 2/16 | -6.25 pp | 1 / 1 | [-20, 0] pp |
| GEPA | 3/16 versus 2/16 | +6.25 pp | 1 / 1 | [-10, +10] pp |

Each test has only three discordant pairs. Neither rejects; **non-rejection is
not equivalence**. None of the 32 joint missing-outcome completions rejects
either. Separately excluding the pass judged copied gives tuned/stock N=15,
0 gains versus 2 losses, exact p=0.5 and Holm p=1. That judgment does not
rewrite the canonical primary counts (Research-Harbor's final 17:48Z ruling).

## Evidence supporting the comparison

PREREG preceded G2. The executed v2 held-out cohort passed the source-backed
split, repository/module and instruction-similarity checks before G2/eval
outcomes. All five training trajectories and 145 per-turn rows were checked
against teacher captures, trims and assistant-target masks. The adapter,
GEPA addendum and two-test family were fixed before held-out evaluation.

All 60 published counts/raw rewards and native result hashes match the frozen
input snapshot. Captures bind model routing, sampling and GEPA-only addendum;
native intervals preserve per-task arm order. Missing outcomes stay missing,
and accounting includes every attempt.

## Material caveats

- **Chronology:** dry-run sample selection at 04:08Z preceded the first eval
  commit at 04:09:33Z. Those samples were outside both cohorts and that adapter
  was deleted, but the literal timing requirement was not met.
- **Execution:** three cold sessions replaced one. Realized trial concurrency
  varied (13, 7, 20, 20), despite common configured parallelism. Repairs changed
  runtime revisions; treatment assets stayed fixed. Shared incidents and
  arm/position-concentrated missingness constrain causal attribution. The
  sandbox-loss cause remains unknown.
- **Counting and review:** later retained full output supports the upstream
  download/extraction/read concern; the frozen consumer's input/format limits
  still leave it unknown. This is not proof of an independent solution.
  Canonical primary and copied-pass sensitivity remain distinct. G6 raters are
  agents, not human ground truth; protected #676 withdraws invalid edit-timing,
  never-edited and post-edit-token claims. No invalid metric strengthens the result.
- **Scope:** one stochastic attempt per cell and a nonrandom light-image cohort
  provide little power/generalization. GPU execution and adapter residency
  remain producer-receipt evidence, not an independent retrain/download.

G5 accounting is about **$12.40 of $16**. The coordinator's after-04 ledger is
**$24.18 Modal + Daytona**; adding the disclosed $2.31 direct-model allowance
gives **about $26.49 of $30**. Billing lag and incomplete direct-call receipts
prevent an unconditional final-cost bound. HAR-133 launched no new experiments
or provider calls.

Evidence and reproduction: [RESULTS.md](RESULTS.md),
[input CSV](har133_outcomes.csv), [analysis](har133_analysis.py),
[numbers](har133_analysis.json), [secondary tables](har133_secondary.json),
and [hash-bound sources](har133_sources.json).
