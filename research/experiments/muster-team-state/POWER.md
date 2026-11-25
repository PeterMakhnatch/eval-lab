# Power: paired instance analysis

OBSERVED — `uv run --no-sync python research/experiments/muster-team-state/power.py`
prints the following table in 22.61 s wall time on the final implementation.

INFERRED — This is sensitivity under a Bernoulli model of full passes, not measured model power.
Each run has 10 independent instances. Each attempt is an independent Bernoulli draw conditional on its instance.
The guard always passes. The probe has mean pass probability p.
For heterogeneity on, each instance has probability p plus or minus `min(p, 1-p)/2`, with equal probability.
Attempts within an instance share that probability. Arms and attempts otherwise draw independently.
The paired difference uses the mean of k attempts on each instance. The code calls the same `paired` function
as `muster contrast`, with sorted difference vectors for caching and a fixed bootstrap seed.
The sorted vectors are sufficient for the bootstrap distribution and exact sign test.
The seeded 10,000-draw approximation can differ with a different ordering of the instances.
The result is conditional on the rates and heterogeneity chosen here; focal scores can be fractional.
The table does not estimate real-run variance, loss rates, coverage or power.

OBSERVED — 2,000 simulated runs/cell; 10 instances; seed 261125; guard pass rate 1.0.

| Probe rate | Heterogeneity | Attempts | Bootstrap excludes 0 | Sign p <= 0.05 |
|---:|---|---:|---:|---:|
| 1.0 | off | 1 | 0.000 | 0.000 |
| 1.0 | off | 3 | 0.000 | 0.000 |
| 1.0 | off | 5 | 0.000 | 0.000 |
| 1.0 | on | 1 | 0.000 | 0.000 |
| 1.0 | on | 3 | 0.000 | 0.000 |
| 1.0 | on | 5 | 0.000 | 0.000 |
| 0.9 | off | 1 | 0.017 | 0.000 |
| 0.9 | off | 3 | 0.284 | 0.033 |
| 0.9 | off | 5 | 0.630 | 0.180 |
| 0.9 | on | 1 | 0.012 | 0.000 |
| 0.9 | on | 3 | 0.237 | 0.025 |
| 0.9 | on | 5 | 0.572 | 0.145 |
| 0.7 | off | 1 | 0.334 | 0.044 |
| 0.7 | off | 3 | 0.975 | 0.771 |
| 0.7 | off | 5 | 1.000 | 0.984 |
| 0.7 | on | 1 | 0.335 | 0.040 |
| 0.7 | on | 3 | 0.952 | 0.672 |
| 0.7 | on | 5 | 0.996 | 0.924 |
| 0.5 | off | 1 | 0.824 | 0.369 |
| 0.5 | off | 3 | 1.000 | 0.998 |
| 0.5 | off | 5 | 1.000 | 1.000 |
| 0.5 | on | 1 | 0.841 | 0.378 |
| 0.5 | on | 3 | 0.998 | 0.952 |
| 0.5 | on | 5 | 1.000 | 0.995 |
| 0.3 | off | 1 | 0.990 | 0.852 |
| 0.3 | off | 3 | 1.000 | 1.000 |
| 0.3 | off | 5 | 1.000 | 1.000 |
| 0.3 | on | 1 | 0.990 | 0.837 |
| 0.3 | on | 3 | 1.000 | 1.000 |
| 0.3 | on | 5 | 1.000 | 1.000 |

INFERRED — Use 1 attempt per instance for the pilot only; even a 50-point gap has low sign-test power.
A separate cohort with 3 attempts can resolve large gaps, but a 30-point gap still has limited sign-test power.
A null result does not rule out a small gap; even 5 attempts give low sign-test power for 10 points.
