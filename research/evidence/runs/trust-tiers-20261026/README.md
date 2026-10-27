# Trust tiers for the shared cloud trace store (CE-3, policy v2 from CE-4)

`tools/assign_trust_tiers.py` derives a `trust_tier` for every issue and trace in a
`CloudTraceDatabase`, and the labeled passage steps for consensus-labeled traces. It
reads identity/label metadata and retained issue records only. Without `--apply` it
opens the store read-only. With `--apply` it takes a consistent SQLite backup first,
then writes the full assignment in one transaction through `assign_trust_tiers`.

## Policy v2

Only rows on tasks that `admitted_cloud_task_ids` admits over `CorpusRegistry` are
promoted: DEV, with no TEST or held-out assignment under any `assignments.csv` key. This
is the same gate retrieval applies. Every other row is `tier3_provisional`.

| Tier | Rule in this store |
| --- | --- |
| `tier1_container_verified` | Confirmed wrong-pass differential, original reward 1.0 → repaired 0.0, with container-replay proof. Any of: a TB4 witness differentially replayed (`retained_native_differential` plus a stored witness digest); a TB2 → TB2.1 maintainer-fix differential; a MiMo native patch-witness differential whose repeat controls all hold natively (≥3 reference runs at 1.0 and ≥3 nop runs at 0.0); or a MiMo repair bound by exact task and repaired package to one of the nine qualified repairs (`train_repairs_v7`). |
| `tier2_consensus_labeled` | A confirmed issue whose model candidate was witnessed strictly 1.0 → 0.0 in one native measurement per grader, without repeat controls (`witnessed_model_candidate_strict_1_to_0`). A Goodfire rollout with three-vote `verdict_enacted == "yes"` and at least one step marked `passage_positive` (LB-3, `corpus-expansion-lb3-20261026`); exactly those steps are stored as `flagged_passage_steps`. CheatBench human-verified runs also belong here, but the store holds none. |
| `tier3_provisional` | Everything else: refuted and provisional issues, rows on non-admitted tasks, and enacted Goodfire rollouts with no positive passage (a tier-2 trace must never return unlabeled setup steps). |

Confirmed and verified witness traces take the tier of the issue their receipt links to.

## Applied to the 2026-10-27 store

3,707 tasks, 252 issues and 4,971 traces; 3,474 tasks admitted:

- **issues:** 32 at tier 1 (2 TB4 replayed witnesses, 30 MiMo patch-witness differentials
  with controls); 40 at tier 2 (39 MiMo and 1 TB4 single-run witnessed differentials);
  180 at tier 3. The TB2.1 maintainer fix is on a task that is not admitted, so it is
  tier 3 until its dataset is.
- **traces:** 32 at tier 1 and 40 at tier 2 (linked witness traces); 1,609 Goodfire
  traces at tier 2, carrying 7,425 flagged passage steps (606 swebench_verified,
  957 deep_swe, 46 impossible_bench); 3,290 at tier 3.
- **Goodfire not promoted:** 27 enacted deep_swe rollouts have no positive passage. The
  other 3,256 LB-3 positive steps sit on rollouts whose enacted verdict is `no` or `split`.

Policy v1 (CE-3) required a unanimous three-vote passage and no admission gate; it
promoted 737 Goodfire traces and no MiMo issue. The applied receipts for both runs are
recorded in the engine bridge, not in this repository.
