# Trust tiers for the shared cloud trace store (CE-3)

`tools/assign_trust_tiers.py` derives a `trust_tier` for every issue and trace in a
`CloudTraceDatabase`, and the labeled passage steps for consensus-labeled traces. It
reads identity/label metadata and retained issue records only. Without `--apply` it
opens the store read-only. With `--apply` it takes a consistent SQLite backup first,
then writes the full assignment in one transaction through `assign_trust_tiers`.

## Policy

| Tier | Rule in this store |
| --- | --- |
| `tier1_container_verified` | Confirmed wrong-pass differential, original reward 1.0 → repaired 0.0, with container-replay proof: a TB4 witness differentially replayed (`retained_native_differential` plus a stored witness digest); a TB2 → TB2.1 maintainer-fix differential; or a MiMo issue bound by exact task and repaired package to one of the nine historically qualified repairs (`train_repairs_v7`). Witness traces count when their confirmed/verified receipt links to a tier-1 issue. |
| `tier2_consensus_labeled` | Goodfire rollout with three-vote `verdict_enacted == "yes"` and at least one passage that all three judges verified positive (`consensus` positive, 3 verified positive votes, 0 negative, unanimous segment). Exactly those passage steps are stored as `flagged_passage_steps`. CheatBench human-verified runs also belong here, but the store holds none. |
| `tier3_provisional` | Everything else, including refuted and provisional issues. |

## Dry run on the 2026-10-26 store (read-only backup copy)

3,707 tasks, 252 issues and 4,971 traces:

- **issues:** 3 at tier 1 (two TB4 witnesses, one TB2.1 maintainer fix); 249 at tier 3.
- **traces:** 3 at tier 1 (the linked witness traces); 737 Goodfire traces at tier 2, carrying 4,148 flagged passage steps; 4,231 at tier 3.
- **Goodfire not promoted:** 1,030 enacted-yes traces have no unanimous passage, so they stay at tier 3.
- **MiMo:** none of the 81 confirmed, witnessed strict 1 → 0 MiMo differentials binds to a qualified repair package, so all 81 stay at tier 3. They come back only with `include_provisional=True`, or if the MiMo rule is widened.

The applied receipt is recorded with the CE-3 entry in the engine bridge, not in this
repository.
