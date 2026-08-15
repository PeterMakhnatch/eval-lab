# Draft N2 — event-summary model pin terra vs sol

**Status:** designed (unsubmitted).
**PROGRAM id:** `EXP-N2-event-summary-sol-vs-terra`

**One variable.** Codex `model` string: `gpt-5.6-sol` vs the already
measured `gpt-5.6-terra` control.

**Fixed elicitation.** `task=canary/event-summary`, `agent=codex`, k=3,
docker, no extra instruction. Control =
`runs/canary-event-summary-codex-20260815` (3/3 reward 1.0,
`model=gpt-5.6-terra`).

**n / k.** n_tasks=1, k=3. **not distinguishable / not comparable** as
a multi-task ranking. Paired-by-task A/B is possible only after the sol
arm exists, with named elicitation tuples (agent_version, model_pin, k).

**What would change the decision.**

- sol 3/3 scored, no exception → both observed cells are 3/3 on this one
  task; that does not establish equivalence or interchangeability at n=3.
  Do not keep proposing “avoid ValueError” specs.
- sol invalid exception → treat as harness/model-string, not capability.
- sol scored 0/3 → an observed one-task contrast that can motivate
  replication; still not a ranking or a general model claim.

**Dependencies.** Policy: `canary` + explicit model. Must **not** use
`registered/event-summary` (that namespace is not a standing canary
member and trips `researcher-followups` requires). Auth: Codex.

**Relation to** `queue/proposed/codex-01M023RP03KGSHB4WZ29WE9DGR.json`:
that spec’s hypothesis (scored result vs model-less ValueError) is
already answered by the terra 2026-08-15 canary. This draft is the
remaining *one-variable* question if Peter still wants sol vs terra.
This draft is not submitted; the proposed spec is not approved/rejected.

**Evidence provenance.** The terra control is reviewed primary evidence
in `baselines/codex-canary-20260815.md`. The proposed queue object is
runtime-only and is not a retained reference in `PROGRAM.json`; the sol
arm is design-only and has no result.
