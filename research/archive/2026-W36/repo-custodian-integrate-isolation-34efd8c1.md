# Repo Custodian — integrate approved isolation/admissibility authority

## Corrected complete lineage

This brief supersedes its earlier 16-file scope. The parent initially omitted the already-reviewed `ba95065b` foundation. The complete isolation authority is the exact lineage `46794c166c39cb64c7c41350f8906b5ec3badab1..34efd8c15363c8bf671afe6ce313c022c3212025`, which touches exactly 27 paths. The missing `bind_outcome_admissibility` dependency exposed the omission. Integrate the full 27-path lineage below—no fewer and no paths outside it.

## Integration target

- Canonical worktree `/private/tmp/eval-lab-staged-spine-integration`, branch `analyst/synth-data-promotion-hardening`.
- Required starting head `adc8a65a`; the current uncommitted integration must still be based there.
- Approved source worktree `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final`, exact head `34efd8c15363c8bf671afe6ce313c022c3212025`.
- Foundation head `ba95065b5ebe580b352ee370b08a2836a84f7c15`, whose parent is `46794c166c39cb64c7c41350f8906b5ec3badab1`.
- Final approval `/tmp/system-architect-isolation-admission-rereview-34efd8c1.md`; the ba950 foundation is the already-reviewed Darwin isolation/evidence authority preserved throughout all final re-reviews.
- Never edit the dirty root or run a model/calibration.

## Exact 27-path scope

1. `research/evidence/readiness/zai-opencode-glm-5.3.json`
2. `research/evidence/readiness/zai-opencode-glm-5.3.network-isolation.json`
3. `sql/views.sql`
4. `src/evallab/analysis_control.py`
5. `src/evallab/campaigns.py`
6. `src/evallab/cohort.py`
7. `src/evallab/database.py`
8. `src/evallab/evidence/atif.py`
9. `src/evallab/evidence/facts.py`
10. `src/evallab/harbor_network.py`
11. `src/evallab/interpretation/benchmark_events.py`
12. `src/evallab/interpretation/producers/__init__.py`
13. `src/evallab/network_isolation.py`
14. `src/evallab/network_isolation_runtime.py`
15. `src/evallab/outcome_authority.py`
16. `src/evallab/profiles.py`
17. `src/evallab/queue.py`
18. `src/evallab/registry.py`
19. `src/evallab/schemas/__init__.py`
20. `src/evallab/trial_admissibility.py`
21. `tests/test_analysis_control.py`
22. `tests/test_campaigns.py`
23. `tests/test_network_isolation_authority.py`
24. `tests/test_outcome_authority.py`
25. `tests/test_registry.py`
26. `tests/test_trial_admissibility.py`
27. `tests/test_trial_admission_bootstrap.py`

## Replay method

Do not merge/cherry-pick the old lineage. Use canonical `adc8a65a` as the base for every overlapping file and graft the full approved isolation semantics from `46794..34efd8c1`. Non-overlapping files may be added from the approved head. Preserve all newer canonical external-lineage, resolver, task-workbench, action-memory, schema, registry, and digest authority. Preserve every canonical test and append/adapt isolation tests; do not replace canonical `test_registry.py` or other overlapping tests with the older source version.

The two readiness evidence JSON files are approved immutable evidence bytes: copy exact source bytes and verify their embedded/file digests remain the approved values. Do not regenerate or alter them.

## Required behavior

- Darwin network isolation evidence/runtime authority and readiness binding;
- strict canonical per-trial admissibility shared by analysis/facts/outcome/benchmark/promotion;
- O_NOFOLLOW stable result snapshot binds source digest, sidecar result, completion time, and live path identity;
- exclusive/fsynced canonical trial authority;
- immutable staged control certification/approval/runtime identity;
- current causal oracle/nop isolation, unskippable direct execution gate, single live rebind;
- production staged-control lifecycle;
- all newer canonical external import and resolver authority retained.

## Verification

Run the exact 13-file 335-test approval matrix. Also run:

- `tests/test_task_workbench.py tests/test_registry.py`
- `tests/test_analysis_control.py tests/test_autonomous_research.py tests/test_digest_assertion_hardening.py`

Run touched Ruff/format and `git diff --check`. Confirm the final diff is limited to the exact 27 paths, canonical top-level definitions/tests were not dropped, evidence digests are unchanged, and the tree is clean. Commit and page exact old/new heads, conflict/resolution catalog, test counts, and no-model/no-root-edit confirmation.
