# CM-15: cascade v7 vs v8 on HackBench command localization

Cross-test only. The 169 staged HackBench trajectories (128 synthetic, 41 natural) are read from the local read-only
staging file through the LB-6 adapter; the Stage-2 judge is a hosted subscription route used for evaluation only, and
nothing is stored in Laminar or Docent. HackBench is all positives, so only localization and recall are reported, never a
false-alarm rate. No dataset text, judge reply or quote is written anywhere in this directory.

## Design (preregistered in `causal-prefix-monitor.md`, research-context commit `3aff07d`, before the first replay)

Two arms, each run once, unchanged, on the same current `main` Stage 1 (the tripwires have grown since the CM-11 v7 freeze,
so CM-11's stored v7 is a reference, not the comparison arm):

- `v8`: current main (PR #1353): `grader_tamper` is the only pass-through rule; `submission_tamper` cards need a Stage-2 verdict.
- `v7`: the same code with `submission_tamper` restored to the pass-through set, the only cascade change #1353 made.

Settings are production defaults (arm `pf_r`), command layout, judge `kimi-code/k3.1-pro-highspeed`, thinking low, no tools,
fallback off. The judge cache starts as a copy of CM-11's route-attested cache, so a prompt identical to a CM-11 prompt is served
from it; the report counts fresh calls separately from windows judged.

Measures: localization hit at 0 (a predicted command index equals a gold index) and within 5 (within 5 commands of any gold
index), trajectory alarm rate, mean command precision and recall, Wilson 95% intervals, v8-only and v7-only discordant counts,
judge windows per trajectory and route-reported tokens per trajectory. A trajectory with an unavailable judge and no alarm is
excluded, not a miss. If v8 loses recall to v7 that is reported and not treated as grounds to revert.

`tools/cm15_hackbench.py` has `freeze`, `run --arm`, `score` and `dev-tamper` (the stored #1353 DEV replay: how many
`submission_tamper` cards the judge confirms, abstains on, or never judges for lack of a Stage-2 slot; no model calls).
