# PR #186 Architect exact-head integration review

- Reviewed head: `e5e1dd9b4a99e3d2a4a7fd47a9a425943e48f270`
- CI at reviewed head: green
- Independent review: Grok, `AgentDataExactHeadReview`
- Verdict: **BLOCK — changes required, then new exact-head review**

## P0 blockers

1. **Completed-campaign authority bypass.** `build_trajectory_ir()` resolves mutable/stale worktree `runs/` paths and has no campaign-inventory/CAS input. PR #185/Track E establish CAS as authority for the completed Gemini TB3 campaign. IR and pack source identities omit the CAS URI/content archive digest.
   - Required: `build IR` accepts an exact trial input record or CAS URI/store root, restores/reads that archive, and binds result/lock/ATIF/task/verifier digests. A path-only developer mode must be explicitly non-production.
2. **EvidencePack budget is observational, not enforced.** `build_evidence_pack()` always returns a pack after computing `is_bounded`; mandatory windows can exceed `budget_tokens`.
   - Required: whole-window selection; no byte truncation; mandatory overflow returns typed `tiered_pack_required` or `abstain_required` and cannot reach the model.
3. **Citation authority is not exact.** Window `reopening_citation.source_path` is populated from `source_sha256`; current `CitationTarget` uses step/call indexes rather than preserved ATIF `step_id`, `tool_call_id`, `source_call_id`; no canonical citation ID/source CAS binding exists.
   - Required: canonical CitationHandle with source document digest + CAS URI + typed locator + content/redaction digest. Every selected item and omitted range must reopen exact bytes.
4. **Quality coverage is not composed.** IR checks `trial_dir/quality.json`, while merged Quality Ledger authority is derived Parquet. Current campaign has four `warn` trials with repeated `ATIF_UNPAIRED_TOOL_CALL`; IR/pack must preserve exact unlinked counts and abstain for claims requiring call-observation linkage.
   - Required: consume `load_quality_report_for_trial`; no missing report assumed ready; quality fail/quarantine cannot produce a model-call pack.

## P1 blockers

1. Hard-coded expected-negative programs have no pinned `TrajectorySemanticsProfile` ID/version/digest. Missing profile must produce `unknown`, not `expected_negative` or `error`.
2. Redaction policy has no digest in IR/pack identity. Redaction changes must mint a new pack digest; raw source digest remains separate.
3. Generic `IROpportunityWindow` and episode names infer recovery/opportunity from adjacency. Rename deterministic windows as screening candidates or require benchmark opportunity-contract citations. `ev.is_error -> recovery` is not a recovery episode.
4. Alignment confound gate checks only task name/digest. It must bind task, environment, verifier, prompt/toolset, scaffold, and factor configuration and declare the one allowed delta. Remove `allow_cross_task` from v1.
5. Alignment drops multiple calls per step, lacks unmatched-range records, and calls the first mismatch `k*` even when it later reconverges. Report local divergence/reconvergence separately; `k*` is the first meaningful non-reconvergent divergence under the frozen contract.
6. Alignment/citation IDs omit IR digests, algorithm/config version, and exact event IDs.
7. PR #186 duplicates the Ops-owned inventory files in PR #187. Merge PR #187 first, rebase PR #186, and remove those files from the PR #186 diff.
8. PR-body smokes cite stale `.worktrees/tbench3-screen` trials, not the completed PR #185 CAS cohort. Re-run the five-trial smoke from the merged inventory/CAS after rebase. DeepPlanning/AgentAbstain/LOCA claims count only if source CAS/raw evidence remains reopenable; otherwise report unavailable/abstained.

## Required focused acceptance

- Identical CAS trial/config produces byte-identical IR/pack digests.
- Wrong CAS, result, lock, ATIF, task, verifier, content, or redaction digest fails closed.
- Four current TB3 warning trials retain unpaired-link coverage and cannot support linkage-dependent acceptance.
- Mandatory-window budget overflow never yields a model-callable pack.
- Missing semantics profile makes exit semantics unknown.
- Omitted reversing evidence forces reopen/new pack or abstention.
- Confounded pairs refuse; insertions/deletions and multiple calls survive alignment; reconvergent local mismatch is not `k*`.
- No PR #187 inventory paths remain in the rebased PR #186 diff.
- Re-smoke from the five exact CAS URIs in the merged analysis inventory.

## Merge order

1. Merge green/reviewed PR #187.
2. Rebase Agent Data branch onto new main; remove duplicate inventory diff.
3. Apply P0/P1 corrections and focused tests.
4. Page Architect with new exact head and CI.
5. New Grok/Architect exact-head review; prior review is invalid after changes.

## Follow-up exact-head review: `efdc43f`

The prior CAS/budget/profile/redaction/quality/opportunity/alignment blockers passed. Merge remains blocked on three cross-boundary defects:

1. **P0 CAS hydration extracts the archive, not the cited member.** `hydrate_citation()` treats `load_archive()` output as text and bypasses source-path/member extraction. Production pack citations can contain archive bytes or a limitation string instead of the named step/tool/observation.
   - Restore/open the CAS archive into a jailed temporary trial root, validate the archive/source digest, resolve `source_path`, then run the same typed locator extraction/redaction/content-hash path as live evidence.
2. **P1 CLI discards the CAS-resolved trial root.** `_traj_pack_command` independently derives `trial_dir` from CLI text after `build_trajectory_ir()` may restore a different CAS root.
   - Return/use one resolved evidence context from IR production, or pass the same restored root explicitly to pack hydration. Add an exact PR #187 inventory/CAS CLI smoke.
3. **P1 SQL projections do not match model fields.** Added views require top-level opportunity/reconvergence/count columns not serialized by the dataclasses/builders.
   - Define projection rows from the actual model/nested fields or add explicit matching top-level fields. Focused round-trip test must write one IR/pack/alignment and assert every SQL view column.

After correction: rebase if main moves, green CI, and a new exact-head review. Prior reviews do not authorize merge.
