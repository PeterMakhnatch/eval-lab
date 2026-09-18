# Repo Custodian brief: close remaining B1–B4 authority gaps at `75792d39`

## Assignment

Repair every blocking finding in the exact Architect report below, commit the code/tests and generated documentation, and return a clean exact head for rereview.

- Worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch: `analyst/synth-data-promotion-hardening`
- Starting HEAD: `75792d39306c0f25dc5d464fe8b4dc5313652fd7`
- Starting tree: `e8ea6a91108ed0d1554d12bb29c36573fc201e3f`
- Architect BLOCK report: `/tmp/system-architect-final-data-authority-rereview-75792d39.md`
- Previous repair brief: `research/inbox/repo-custodian-repair-final-data-authority-b1-b5-86a9e0bb.md`

Read the full report before editing. Work only in the integration worktree. Do not touch the dirty root checkout. Preserve the accepted B5, B6, exact tamper regression, approved CAS/projection semantics, and historical evidence.

## B1 — make the actual model-facing tree private and immutable across adapter construction

The deterministic RequestStore snapshot is still discoverable and writable, and `adapter_factory` currently receives snapshot-backed records before the last digest check. Close this structurally:

- Adapter construction must receive frozen metadata only—not `JobRecord`/`TrialRecord` objects exposing a writable snapshot path. Cleanly migrate every adapter-factory caller/test; do not leave a compatibility path that still exposes the source path.
- Construct the adapter first. Only after the factory returns, verify the stored frozen snapshot's exact digest/no-follow identity.
- Materialize/copy the verified frozen content into a fresh private execution directory whose unpredictable path is never exposed to adapter construction. Make files/directories non-writable, reject every symlink, and bind full tree content plus device/inode/dirent identity.
- The actual model loading/rendering/citation/sidecar path must be only this private execution tree. Recheck its digest and identity at the last pre-invocation boundary and after analysis/finalization as appropriate. Clean it safely after use.
- Keep the final live-original identity check after adapter construction: mutation of the original path still quarantines with zero calls.
- Commit the exact new adversaries:
  1. `adapter_factory` mutates the stored RequestStore snapshot by changing `result.json`/adding `exception.txt` -> quarantine, zero calls, no sidecar/authority;
  2. snapshot symlink/relabel substitution -> zero calls;
  3. original-path adapter mutation -> zero calls;
  4. unchanged source/snapshot -> analysis uses only the private execution tree and remains green.
- Do not regress to a recheck-only mutable-source design. The private verified execution tree is required even after the zero-call checks.

## B2 — authenticate inside the atomic publication boundary

The current final locator check occurs before fsync/native-call hooks, so bytes can change afterward.

- Fsync the complete staging tree first.
- Convert the private staging tree to a non-writable, no-follow publication form; bind device/inode identities for every entry.
- Move the complete final locator reauthentication into the authenticated atomic-publication primitive itself, after all validators/fsync/hookable work and immediately before the raw native no-replace syscall. The primitive must accept the expected locator/content identity and refuse if final bytes or identities differ.
- Keep the raw syscall implementation below this authenticated boundary so an injected wrapper that mutates staging and then calls the real authenticated primitive is caught by the real primitive's own final check.
- No Python validator, fsync traversal, callback, or other hook may run between the internal final reauthentication and raw syscall.
- Commit the exact native-boundary adversary from the report: mutate staged `result.json` in a wrapper at the publication call boundary, then call the real authenticated primitive; require no destination, typed tamper failure, and zero staging residue.
- Preserve lexical no-follow destination/root checks, Darwin/Linux signatures and architecture handling, typed Linux conflicts, mid-failure cleanup, and one-winner concurrent publication.

## B3 — authenticate every ancestor and the canonical durable job identity

- Walk every lexical component from the trusted `repo_root` through `research/evidence/runs`, job, trial, and required evidence files with parent-dir-fd plus `O_NOFOLLOW` (or an equivalently strict no-follow implementation). Reject any ancestor/intermediate symlink before a model call.
- Safely load/authenticate canonical job-level evidence after no-follow validation. Require canonical durable job UUID to equal the independently CAS-materialized job UUID. Do not compare the CAS archive `record_id` to job name/UUID.
- Require canonical job-level embedded provenance/spec identity to be present and exact. Missing provenance fails closed.
- Bind job-level required evidence files and directory identities/digests across analysis/finalization, not only trial descendants. A job-root result/provenance mutation or replacement must mint no authority.
- Replace retained generic `RuntimeError` post-call drift paths with typed `TrialAdmissibilityError` reasons. Content, inode, and dirent drift all fail before authority minting.
- Commit exact adversaries:
  1. `<repo>/research` intermediate symlink -> zero calls;
  2. canonical durable job-level `result.json.id` differs from CAS-loaded UUID while trial remains exact -> zero authority;
  3. missing canonical provenance -> zero calls/authority;
  4. post-call canonical content drift -> typed `TrialAdmissibilityError`, zero authority;
  5. byte-identical replacement and nested symlink controls remain green.

## B4 — no inferred terminal event and no optional attempt identity

- `select_terminal_job_locator` must require explicit `expected_event`, exact job name, exact spec ID, CAS kind `job`, and the expected attempt/request identities available to the caller.
- If `expected_attempt` is supplied, an event with missing `attempt_number` must not match. Exact mismatch and duplicate exact matches fail closed.
- `CampaignOrchestrator._queue_cas_locator` must require `expected_event` with no default and contain no state inference, candidate scan, or `events[-1]` branch.
- `_materialize_queue_job` and every other production caller must pass the exact expected transition explicitly and pass the campaign attempt number/index. If distinct campaign failure transitions are legitimate, each caller binds that one exact transition; never pass an allowed set.
- Tests and runtime must use the same production selector.
- Commit missing-attempt, wrong-attempt, omitted-event-at-callsite/type-level, duplicate exact, and production-caller controls. Canonical/bootstrap remains exactly `dispatch_completed`.

## Preservation and verification

- Preserve accepted B5 real raw-deletion CAS ingest, B6 invalid-sidecar separation, and the exact final-state `source-digest-drift` cause chain.
- Do not modify approved settlement/attach/reconciliation/parquet authority, historical generator/source/evidence, `data_backfill.py`, or `historical_git_snapshot.py`.
- Do not weaken evaluator or settlement semantics. Do not execute model/control calls.
- Run focused adversaries first, then exact CAS, projection/attach, canonical-authority, quality/worker, historical, state-events, governance, Ruff lint/format, repomap/docindex, and `ty` if available.
- Regenerate repository map/docindex only after code/tests are clean. Commit code/tests, then generated docs if changed. Confirm a clean integration worktree and untouched root checkout.

Return the exact commit chain, HEAD/tree, causal result for every adversary, exact matrix counts, preservation identities, and unavailable tools honestly. Do not claim completion until every B1–B4 control is committed and green.
