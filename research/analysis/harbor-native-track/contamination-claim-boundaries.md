# Contamination and claim-boundary note (TW / FACET / external packs)

Date: 2026-09-06. Status: policy, binding on this track until superseded.

## Facts (observed)

- TW/FACET/TB2.0-oracle trials landing in `runs/` carry **`oracle.txt`, not
  `trajectory.json`**: `runs/harbor-pack-verify/.../tw_100459__DYtW6yK/agent/oracle.txt`,
  `runs/harbor-tb21-oracle-smoke/.../{break-filter-js-from-html,gpt2-codegolf,llm-inference-batching-scheduler}__/agent/oracle.txt`.
  They contribute zero ATIF steps and are excluded from every trajectory
  denominator in this track's tables.
- TerminalWorld (1,530 tasks) and FACET (6,020 packages) are **public** (Hugging
  Face). Any model under test may have been exposed to their content during
  pretraining; exposure is unknown and unknowable from our side.
- Their verifiers were validated under their authors' harness and task-version
  assumptions, not ours. Passing their verifier here measures transfer under
  our harness, not the capability the pack was built to isolate.

## Boundaries (binding)

1. Nothing from a public external pack enters a **capability number** (no rate,
   no curve, no Wilson interval presented as agent capability).
2. Nothing from these packs enters a **card Result section**. They may appear in
   a card's *Methods/Transfer-observations* section, labeled with pack name,
   revision/commit, trial count, and the sentence "public pack; model exposure
   unknown; verifier under authors' harness."
3. Oracle-only trials are reported as **pack-verification outcomes** (did the
   reference solution pass under our Docker?), never as agent outcomes.
4. External-pack trials keep their quarantine prefixes (`tw_*`, FACET job names)
   so they cannot silently merge into canary denominators. Any query over
   `runs/` or the traj store that does not exclude these prefixes is
   mis-specified for capability purposes.
5. First-use policy per external pack: oracle pass → nop fail → one cheat probe,
   before any agent trial counts as a transfer observation (admission, not
   capability).

## What remains unestablished

- Model-exposure status for any specific model/pack pair (cannot be established
  from our side; the boundary above is designed to not need it).
- Verifier-equivalence between the pack's harness and ours for any given task
  (establish per task via the oracle/nop/cheat triple, not by assumption).
