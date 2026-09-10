# Eval Platform: close parent-review gaps before Architect review

## Exact target

Continue as sole writer in `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final` on `fix/darwin-isolation-admission-final`, currently `20fecb1dab1a08038349ea1e260748212b68eb0f` over `ba95065b`. Do not rebase yet and do not integrate. Preserve all reported B1-B5 closures, Darwin evidence/transport digests, and no external-lineage edits.

## R1. Pending-to-admitted finalization must preserve certification and identity on every public path

Current pending finalization constructs a default unbound `TaskCertificationEnvelope` when `certification_path` is omitted, then writes it over the pending record. `verify_certification_packet` accepts `legacy_missing`, so a public `promote_task` call can finalize controls while discarding the bound certification and changing `task_runtime_identity`.

- Treat the pending record's bound certification/approval/runtime revision as immutable.
- With no replacement packet, reuse and verify the existing bound certification.
- If a packet is supplied, require it to rebuild exactly the same certification envelope; refuse certification drift.
- Do not change actor/approval or other immutable revision fields during finalization except the explicit admission envelope (`control_evidence`, `state_reason`, `allowed_uses`) needed for pending -> admitted.
- Require full admitted records to carry bound certification; `legacy_missing` cannot pass this new lifecycle.
- Add API negatives for omitted packet, changed packet, changed actor/approval, and assert exact `task_runtime_identity` equality.

## R2. The repository authority path cannot be overridden

`verify_trial_admissibility` and `finalize_trial_admissibility` expose `artifact_path`, allowing a caller to make an arbitrary artifact the authority even when `repo_root` is present. Remove the override or require exact equality with `canonical_trial_admissibility_path(repo_root, trial_id)` before any read/write. Production consumers and the single writer must mechanically converge on `research/evidence/trial-admissibility/<trial-id>.json`. Add an alternate-path refusal test. Fsync the parent directory after first atomic publication.

## R3. Invalid interpretation output cannot become causal authority

`run_trial_analysis` currently writes a sidecar even when `validation_errors` is nonempty, then calls `finalize_trial_admissibility`; the builder only binds its digest, so that invalid sidecar can still produce `admissible/causal`. Only a schema-valid interpretation with `validation_status == "valid"` and exact trial/source identity may complete the six-source chain. Enforce this in the shared source verifier (not only at the writer callsite), so every consumer and promotion rejects a digest-bound but invalid interpretation. Add producer and strict-loader negatives.

## R4. Prove the real dispatch lifecycle, not a hand-authored file simulation

The current bootstrap test fabricates RunProvenance, positive isolation evidence, job/trial files, and directly calls `finalize_trial_admissibility`. It therefore does not prove B2+B3+B5 integration. In production, campaign compilation returns no `CampaignRuntimeIdentity` for nonbillable oracle/nop (`campaigns._resolve_campaign_runtime_identity`), and queue live identity rebind runs only for `spec.billable`; therefore a real pending baseline control either has no isolation evidence (cannot become causal) or can carry stored evidence without dispatch-time live parity.

Make the production control-bootstrap route explicit:

- a pending registered oracle/nop baseline attempt that will feed promotion must carry qualified, causal isolation evidence and immutable runtime identity;
- dispatch must rebind runtime/image/adapter/probe identity for that causal control even though it is nonbillable, and refuse drift before runner invocation;
- campaign/spec validation must reject causal control claims with missing bound isolation evidence;
- ordinary non-causal local controls may remain nonbillable without causal claims, but they cannot feed promotion;
- add a production-path test through campaign/spec/queue admission (runner may be mocked; no model) proving exact staged identity -> live-bound oracle/nop attempts -> canonical valid sidecars -> final admitted identity unchanged; add missing-evidence and live-drift negatives.

If existing profile/readiness contracts cannot represent a nonbillable control runtime, add the smallest explicit control-bootstrap runtime contract; do not copy billable model identity heuristically.

## Verification and delivery

Run the final admission suites plus focused campaigns/queue/registry/facts/outcome/benchmark/network tests, touched Ruff/format, and diff-check. Commit one focused follow-up, page exact new head/path list/counts and remaining integration hotspots. Do not request Architect review until all four are closed.
