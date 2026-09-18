# Repo Custodian brief: close final integrated data authority blockers at `86a9e0bb`

## Assignment

Repair every blocking finding in the exact final Architect report, commit the implementation and generated documentation on the canonical integration branch, and return an exact-head validation report.

- Worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch: `analyst/synth-data-promotion-hardening`
- Starting exact head: `86a9e0bb3f46c009bae79510ce12fb15b0b9483e`
- Architect report: `/tmp/system-architect-integrated-data-final-rereview-86a9e0bb.md`
- Prior repair brief: `research/inbox/repo-custodian-repair-integrated-data-b1-b6-6843da00.md`

Read the full Architect report before editing. Work only in the stated integration worktree. Do not touch the dirty root checkout. Preserve B6, the exact tamper regression, approved CAS/projection source semantics, and all historical evidence.

## B1 — remove the post-admission quality check/use gap

`AnalysisWorker` must never render or call a model from a mutable original trial path after validating a frozen identity.

- Bind the request to a content-addressed immutable evidence snapshot used for the actual analysis read. Prefer the existing CAS `EvidenceLocator`/materialization path; if a private frozen snapshot is the repository-consistent mechanism, it must have a deterministic content identity stored in `AnalysisRequest`, be inaccessible as a caller-selectable relabel, and be the only model-facing source.
- Do not “fix” this with one more digest check followed by reopening the mutable original path; that leaves the same race.
- Adapter construction and concurrent mutation of the original trial after admission must not affect the snapshot used for rendering/analysis.
- Add a committed adversary that stages a WARN/analysis-ready request, mutates the original in `adapter_factory` by adding `exception.txt`, and proves zero model calls or proves the model used only the exact frozen snapshot without minting authority from changed source. The original mutable path must never become the paid-call evidence source.
- Preserve evaluator semantics, including legitimate analysis-ready WARN behavior, and preserve exact pre-admission add/remove/replace drift controls.

## B2 — exact lexical, authenticated, no-replace publication

Repair `_promote_control_bootstrap_job` and its OS primitive:

- Keep the exact lexical destination `<durable_root>/<spec.name>`; do not resolve the destination dirent into an alias target.
- Reject symlinks and broken symlinks in the publication root/destination chain using no-follow checks. A symlinked durable root or requested job name must fail closed without publishing anywhere.
- Re-inventory and reauthenticate the complete final staged tree against the exact `settled_run.cas_locator` after all validators and immediately before atomic publication. Do not publish a tree whose bytes changed after validation.
- Set explicit ctypes `argtypes`/`restype`; map Darwin/Linux architectures explicitly, including Linux arm64/aarch64; narrow exception handling so `EEXIST`/`ENOTEMPTY` remains the typed `control_bootstrap_job_conflict` and other OS errors are not swallowed as `platform_unsupported`.
- There must be no ordinary overwrite-capable rename fallback.
- Commit negative controls for broken in-root job-name symlink, symlinked durable root, mutation after validators but before publish, Linux conflict errno, mid-materialization cleanup, and two concurrent publishers. Preserve one winner/no overwrite/zero staging residue.

## B3 — non-relabelable canonical publication and path identity continuity

Repair `CanonicalPublicationBinding`/`run_trial_analysis`:

- Remove arbitrary `publication_root` selection. Use a narrow trusted constructor derived from the repository/campaign context and exact terminal request/event; canonical publication root must be exactly the lexical no-follow `<repo>/research/evidence/runs`. Do not accept any other in-repository relative path.
- Keep exact derived trial path `<canonical-root>/<binding.job_name>/<trial.name>` with no scan/glob/name fallback and no CAS-record-id/job-UUID conflation.
- Reject symlinks in every directory and file component before any model call, including nested evidence files such as `result.json`.
- Require exact loaded job UUID and required embedded provenance/spec identity; missing provenance fails closed.
- Bind device/inode/dirent identity (or an equivalent fd-anchored authenticated tree) for the canonical directory and required evidence files across analyzer execution and finalization. A byte-identical directory/file replacement during the analyzer call must raise typed `TrialAdmissibilityError` before authority minting.
- Post-call drift/path-identity failures must be typed; do not raise generic `RuntimeError`.
- Commit the exact adversaries from the report: internal-repo unrelated root relabel, nested-file symlink with zero calls, byte-identical whole-directory replacement during analyzer with no authority, plus existing outside-root/unrelated-copy controls.

## B4 — one production terminal-event selector

Centralize a single production selector used by both runtime and bootstrap tests.

- Require exactly one event matching `dispatch_completed`, expected job name, exact spec ID, CAS kind `job`, and every available attempt/request identity.
- Wrong event type, wrong job, wrong kind, missing/mismatched attempt identity, duplicate exact match, and ambiguous near-match must fail closed rather than pick last/any.
- Materialize only from that exact `EvidenceLocator`, then independently require exact loaded job UUID and required embedded provenance/spec identity. Missing provenance is invalid.
- Remove duplicated/weaker test-only selection logic.
- Commit production-method adversaries for wrong event/job/kind, missing provenance, duplicate near match, and duplicate exact match.

## B5 — remove implicit ingest store authority

- `_ingest_command` must use `args.store` directly. Delete `EVALLAB_EVIDENCE_STORE_ROOT` and `derived/run-cas` fallback branches for ingest.
- Preserve required parser `--store`.
- Strengthen the committed CLI regression to exercise the real archive boundary: delete raw evidence immediately after successful archive, then prove projection/settlement succeeds from CAS materialization and assert the ready manifest's locator record/content identity and relevant digests—not only mocked plumbing or output existence.

## Preservation and non-goals

- Do not weaken `evaluate_trial_quality` or settlement validation.
- Do not modify approved `storage/settlement.py`, `storage/reconciliation.py`, `storage/attach.py`, `evidence/parquet_io.py`, historical generator/source, `storage/data_backfill.py`, `historical_git_snapshot.py`, or checked historical evidence except for a genuinely required generated repo-map/docindex update.
- Do not execute any model/control calls.
- B6 invalid-sidecar separation and the exact final-state `source-digest-drift` cause-chain regression must remain green.
- No compatibility shims, fallback authority, inferred locator, raw-source fallback, mutable Parquet island, or path scan.

## Verification and delivery

Run focused adversaries first, then the exact CAS, projection/attach, canonical-authority, quality/worker, historical, state-events, and governance matrices. Run Ruff lint/format, governance, repomap, docindex, and `ty` if available. Regenerate repo-map/docindex only after the code/tests are clean. Commit code/tests, then generated docs if changed. Confirm a clean worktree.

Return:

1. exact commit chain and HEAD/tree,
2. causal evidence for each B1–B5 adversary,
3. exact matrix counts,
4. preservation comparisons and pinned historical identities,
5. any unavailable tool honestly.

Do not claim completion until every B1–B5 control is committed and green.
