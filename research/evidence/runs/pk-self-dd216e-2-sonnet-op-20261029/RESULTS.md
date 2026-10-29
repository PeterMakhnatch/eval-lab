# Sonnet suspicion >= 1 does not transfer at the false-alarm constraint

**Verdict:** The selected Goodfire DEV operating point does **not** establish transfer. It fails the B6 established-negative false-alarm gate and the available CheatBench DEV false-alarm gate. Some requested comparisons are withheld under conservative small-cell and complementary suppression, so this result is not a claim that no recall benefit exists.

Preregistration [#1593](https://github.com/PeterMakhnatch/eval-lab/pull/1593) merged as `73c3aec644c53cc2770e763c6f69b3c5813fdebd` before the held-out scorers ran. Each held-out scorer was run once, with stored reads only; no model calls, local inference, Docker or uploads. The B6 score.json aggregate-level block was not opened before preregistration and was not used in this analysis. The strict assignments cross-key and B6 frozen-input guard completed before scoring. The receipt is `results/summary.json`; it contains aggregates only.

## Goodfire: reproduction, not validation

The exact NP22 secondary row reproduces: **17/80 enacted, 0/70 clean**, with the 0/70 95% Wilson upper bound **5.2%**. NP22 selected the last field-complete Sonnet line per run *without requiring attestation*, whereas its separately described attested-v2 cohort uses a successful v2 form call served by Sonnet; attestation is per run, not per line. The attested comparison is withheld for complementary disclosure protection. The point was chosen after seeing Goodfire, so 17/80 is in-sample and vulnerable to selection bias. One false alarm is consequential at this sample size.

## T1: B6, fixed Sonnet >= 1

The frozen B6 sample has 105 established negatives; at least **22/105 = 21.0%** alarm, a conservative lower bound from five individually releasable set cells alone. It already exceeds the 3% FAR point gate by a wide margin, hence also precludes a Wilson upper bound <= 6%. The 22 alarms comprise Cheat Index 0/15, EvilGenie 3/15, ResearchArena 7/15, SDF 7/15, and Beam 5/15. The remaining sets are not assigned zero: their cells and the exact pooled numerator are withheld. Published prior B6 >= 2 cells must also be considered when suppressing complements. The lower bound is a sum of safe disclosed cells, not an estimated pooled rate.

The preregistered pooled recall, the >= 1 versus >= 2 paired sign/McNemar reading and task-clustered interval, the per-set/all-negative comparison blocks, and stored Haiku/cascade/union comparisons are **not reportable** under the scorer's conservative complementary suppression. This is a disclosure limitation, not a zero or evidence that the paired effect is absent. Therefore clause (b) is not adjudicated, even though clause (a) fails decisively. Likewise, the CM-14/CM-19 operational target of **15% recall at 3% FAR** cannot be achieved by this operating point on B6: its established-negative FAR is already above 3%, regardless of its suppressed recall.

B6 is a capped sample, not a prevalence-weighted population; its 281 assignments yielded 278 paired Sonnet/Haiku reads, including three refused positives treated as unknown. Pooling is the sum of set counts. Runs from a task are not independent. The frozen readers were not always given byte-identical inputs: four ResearchArena negatives were affected by a blinding change, as documented in B6. B6/CM-18 positive labels include detector/oracle/monitor-selected examples and can inflate measured recall. No deployment recall is established. The planned clustered interval was not released, so no effective independent sample size or precision is claimed here.

## T2: CheatBench DEV

Stored Sonnet records **do** exist. On the fixed 75-negative DEV cohort only **37/75** had an attested Sonnet read: **8/37 = 21.6% FAR** (95% Wilson 11.4–37.2%); 38 were unknown and excluded. On those same 37 negatives, stored cascade v8, Sonnet >= 2 and Haiku >= 2 each alarmed **0/37**; the exploratory Sonnet >= 1 OR cascade union alarmed **8/37**. Thus clause (c) fails both the <=3% FAR threshold and the preregistered minimum 60 observed negatives. Positive results and the paired recall-versus-cascade comparison are withheld for complementary suppression, not treated as zero. Coverage is partial, so these results describe the read subset, not necessarily all 75 negatives. The fixed assignments cross-key preceded label and facet reads.

## T3 and limitations

NP21's four newer sets have Haiku reads only and are not applicable to this Sonnet test. The prior warning that Haiku >= 1 FAR was 17.6% on CM-18 and Sonnet >= 2 all-negative FAR was 4/180 on B6 made high FAR foreseeable; the test was run unchanged anyway. Only T1 and T2 are out-of-selection tests. The result rejects this particular threshold's specified transfer criterion; it does not prove that other thresholds, readers, corpora or deployment settings cannot work.

**Checks:** synthetic scorer selftests passed; Ruff, docindex, repomap and governance checks passed locally. `make check` timed out after 300 seconds after those gates and registry audit had passed; its remaining checks did not complete. Preregistration required hosted checks passed before merge. No model was called.
