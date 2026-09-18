# System Architect brief: final data-authority rereview at `75792d39`

## Assignment

Perform a fresh, read-only, adversarial rereview of the exact clean repaired head below. Rerun every causal adversary that blocked `86a9e0bb`; inspect the new implementation rather than trusting the Custodian report.

- Worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch: `analyst/synth-data-promotion-hardening`
- Exact HEAD: `75792d39306c0f25dc5d464fe8b4dc5313652fd7`
- Exact tree: `e8ea6a91108ed0d1554d12bb29c36573fc201e3f`
- Main repair: `06a2c3ea`
- B1 zero-call follow-up: `934f1089`
- Generated docs: `57770250`, `75792d39`
- Prior BLOCK report: `/tmp/system-architect-integrated-data-final-rereview-86a9e0bb.md`
- Prior review brief: `research/inbox/system-architect-integrated-data-final-rereview-86a9e0bb.md`
- Repair brief: `research/inbox/repo-custodian-repair-final-data-authority-b1-b5-86a9e0bb.md`

Write no repository files. Deliver `/tmp/system-architect-final-data-authority-rereview-75792d39.md` and page `wH:p9` with exact `APPROVE` or `BLOCK`, report path, head/tree, and concise causal evidence.

## Required adversaries

### B1

Verify `AnalysisRequest` binds a deterministic content identity for a private frozen snapshot and that all model-facing loading, rendering, citations, and sidecar logic use only that snapshot. The original trial path must never become the model source after admission.

Rerun the exact adapter-factory adversary: stage an analysis-ready WARN, mutate the original path in `adapter_factory` by adding `exception.txt` and changing result content, and require quarantine with zero model calls, no sidecar/authority, and an unchanged snapshot matching the frozen digest. Also attempt direct mutation/relabel/symlinking of the snapshot itself if its path is discoverable; a content-addressed snapshot must fail closed, not become a second mutable authority. Confirm ordinary pre-admission add/remove/replace drift and legitimate unchanged WARN behavior.

### B2

Rerun:

- broken in-root destination symlink,
- symlinked durable root,
- staged-byte mutation after all validators but before publication,
- injected mid-materialization/validation failure,
- two concurrent publishers,
- Darwin and simulated Linux `EEXIST`/`ENOTEMPTY` handling.

Verify lexical `<durable-root>/<spec.name>` identity is preserved with no `resolve()` relabel, final complete-tree bytes are reauthenticated against the exact locator immediately before native atomic no-replace publication, staging residue is cleaned, ctypes signatures/architecture mapping are correct (including Linux arm64/aarch64), and no overwrite-capable fallback exists.

### B3

Rerun:

- arbitrary in-repository publication root relabel,
- outside-root relabel,
- nested evidence-file symlink,
- symlink in every publication path component,
- pre-call content mutation,
- byte-identical whole canonical-directory replacement during analyzer execution,
- post-call content/inode drift.

Require exact lexical trusted canonical root `<repo>/research/evidence/runs`, zero calls for pre-call binding failures, typed `TrialAdmissibilityError` for identity drift, and zero minted authority on all failures. Confirm device/inode/dirent binding covers required source components across analysis/finalization. Confirm no CAS-record-id/job-name/job-UUID conflation: locator identity, event job name, and loaded UUID/provenance must be authenticated independently. Missing embedded provenance must fail closed.

### B4

Review the centralized production selector and every caller. `expected_event` must be explicit and exact—not an allowed set. Canonical/bootstrap analysis must use `dispatch_completed`; other campaign terminal states may only be selected when the caller binds that exact state transition. Require exact expected job name, spec ID, CAS kind `job`, and every available attempt/request identity; exactly one match.

Rerun wrong event, wrong job, wrong kind, missing/mismatched attempt identity, missing provenance, duplicate near match, and duplicate exact match. Tests and runtime must use the same production selector. No last/any selection.

### B5

Verify `_ingest_command` uses `args.store` directly with no environment/default fallback. Rerun the committed real archive-boundary CLI control: delete the raw source immediately after archive; settlement/projection must succeed only via CAS materialization, and the ready manifest must assert exact locator record/content identity and relevant digests.

### B6 and tamper preservation

Confirm invalid sidecars still persist without admissibility minting, explicit strict finalization fails closed, invalid-citation worker behavior is green, and the final-state tamper regression still asserts exact `TaskControlEvidenceError` caused by `TrialAdmissibilityError:trial_admissibility_invalid:source-digest-drift` rather than an incidental error.

## Preservation

Confirm approved CAS/projection source semantics and historical evidence remain unchanged, including:

- approved projection source `26509bef00ac0bc19666e3436c1bdc04c357f878` authority files,
- historical parent `09b839770e53137dd09a7131776120d462a67761` source/evidence,
- snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`,
- plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`,
- manifest SHA `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`,
- counts `170/152/130/128/2/40/18`, ready/admissible `0/0`, and 1,690 explicit-null design fields,
- descriptive-only historical status.

## Verification and verdict

Run focused adversaries, then the relevant exact CAS, projection/attach, canonical-authority, quality/worker, historical, state-events, and governance matrices, Ruff lint/format, governance, repomap/docindex, and `ty` if available. Report exact counts and unavailable tools honestly.

`APPROVE` only if every demonstrated B1–B5 authority path is causally closed, B6/tamper behavior and preservation hold, and no new competing authority path was introduced. Otherwise `BLOCK` with the exact defect and minimal repair contract.
