# Ops Eval Runner — content-identity CAS final closure

## Authority and workspace

- Continue as sole writer in `/Users/petermakhnatch/Developer/eval-lab/.worktrees/eval-runner-cas-harbor-settlement`, branch `fix/run-cas-harbor-settlement`.
- Current reviewed head: `f406bc06bf713caf39820daa7099244b3743704f`.
- Read exact report: `/tmp/system-architect-run-cas-harbor-rereview-f406bc06.md`.
- Preserve every closed producer-digest, captured-archive restoration, typed failure, terminal-state, explicit `SettledRun`, and configured-Harbor-path guarantee. Do not rebase/integrate/touch root/run a model.

## Required clean cutover

The authenticated return contract must be immutable content identity, not mutable filesystem pathname identity.

### CAS identity

- Remove mutable `manifest_path` and `blob_path` from the authenticated `EvidenceArchive` contract, or make them explicitly non-authoritative diagnostics outside that contract. Prefer deletion and migrate every caller/test.
- The authority returned by produce/reopen must consist only of independently bound values: record kind/id plus expected producer `record_digest`, `content_digest`, `archive_digest`, CAS URI/reference, file count, and uncompressed bytes.
- `reopen_evidence_archive` may use store-root/kind/id to locate bytes, but it must capture and authenticate exact record/archive bytes against the supplied independent digest(s), restore those captured bytes, and return only the content identity. A later consumer must reopen with the settled expected record digest; it must never trust a remembered path.
- Once the exact anchored bytes have been captured and verified, later pathname replacement must be irrelevant to the returned identity. Remove the futile sequential “last reread means immutable path” claim.
- Add adversarial hooks that replace record/blob entries after their last captured-byte reads. Assert that no mutable path is returned/used as authority and that a subsequent reopen using the settled digest refuses the replacement.

### Raw job settlement boundary

- Remove optional live-tree equality as a return-time claim. Freeze the completed raw job out of the mutable producer namespace before hashing/archiving—prefer an atomic same-filesystem move into an executor-owned settlement area—then settle from that frozen source.
- The run completion contract is the reopened CAS content identity, not continued equality with a mutable job directory. A diagnostic/restored job directory may exist, but it is not authoritative and must not be consumed by downstream projection.
- If a frozen source path is retained internally, do not expose it as authenticated identity. Mutation of the old producer path after the atomic freeze must be irrelevant.
- `SettledRun` and queue/ingestion callers must consume the CAS record/reference plus settled record digest. Remove any downstream authority read from `SettledRun.job_dir` or mutable raw-job paths; this is the user-required clean cutover.
- Add a probe that mutates the former producer path after freeze/final source capture. CAS identity and downstream handoff must remain the frozen verified content, while subsequent tampered locator reads fail against the expected digest.

### Harbor launch

The configured executable race is independently closed. Preserve the executor-owned staged artifact behavior and test. Arbitrary same-user mutation of executor-owned staging is a documented threat-model hotspot, not the present configured-path blocker; do not regress it.

## Shared Data contract

Engineer Data will consume a locator plus independently supplied expected record digest, never a raw path as authority. Keep the public loader simple and mandatory-digest. Page Data the final approved signature/head after this repair; do not copy projection code here.

## Verification and delivery

Use LSP references before changing/removing exported fields and migrate every caller. Add exact post-final-snapshot mutation tests and downstream CAS-only handoff coverage. Run focused runner/queue/evidence-store tests, touched Ruff, format check, and `git diff --check`. Commit complete approval-ready repair and page exact old/new heads, paths, test counts, clean status, and no-model/no-integration confirmation. Do not stop at a scope summary.
