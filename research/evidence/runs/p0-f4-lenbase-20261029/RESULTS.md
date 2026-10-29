# Length baseline scorecard: results

Reproduction only; no new empirical claim. Stored findings, no model calls.

- NP30 149-run Goodfire population, cascade v8: length AUROC 0.776, detector AUROC 0.555 [0.517, 0.594] (stratified run bootstrap), length-only rule 28/80 = 35.0% at FAR 1/69, the cascade's own FAR. NP30 published 0.776, 0.555 and 28/80 at 1/69.
- `trace score` before and after: JSON identical once the added `length_baseline` field is removed, for goodfire (8/8 gate checks pass before and after), cheatbench (4/4) and hackbench (1/1). HackBench has no negatives and renders no block.
- On the 150-run Goodfire cohort the block is on 29 detector units; on CheatBench 17. Length AUROC there is 0.770 (Goodfire) and 0.298 (CheatBench: longer runs are less often hacks, so the length rule is not a useful baseline in that direction).
