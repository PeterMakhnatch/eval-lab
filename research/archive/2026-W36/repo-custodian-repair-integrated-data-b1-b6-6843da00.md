# Repo Custodian — repair B1–B6 on exact integrated head `6843da00`

## Exact start and scope

Work only in clean canonical worktree `/private/tmp/eval-lab-staged-spine-integration`, branch `analyst/synth-data-promotion-hardening`, exact blocked head `6843da002ddebdcd2d2c450633a5f00da2554a2d` (tree `640305aa`). Do not rewrite prior commits or touch dirty root. Add focused source/test commit(s), then official generated docs only if changed.

Authoritative rereview: `/tmp/system-architect-integrated-data-repair-rereview-6843da00.md`. Repair every B1–B6 and the weak tamper test exactly. Preserve the accepted quality settlement/state-events work. No compatibility aliases, default authority, verifier weakening, backfill, legacy mutation, model/control calls, or unrelated fixes.

## B1 — freeze deterministic quality identity before any model call

Cleanly revise the durable `AnalysisRequest` contract (bump its schema version if its required identity surface changes; migrate all callers/tests, no backward shim) so staging freezes and request identity commits to:

- normalized deterministic quality report decision: check version/digest, status, analysis-ready/ingestable flags, quarantine reason and deterministic report digest (exclude nondeterministic timestamp);
- an exact presence+content identity for every filesystem input used by `evaluate_trial_quality`, including optional inputs such as `exception.txt`, result, trajectory and all job/trial files whose presence/content can affect the report.

At staging, evaluate and freeze this identity without a model call. At admission, recompute from current exact source bytes before interpreting status or calling adapter; any add/remove/replace/status drift is typed stale/tampered/quarantined with zero calls. The frozen/request identity must not allow a staged quarantined source to become admissible after deleting an optional file.

Add the Architect adversary verbatim plus add/remove/replace/quality-status/check-digest drift cases. Assert exact reason and zero calls. Preserve all existing result/trajectory/lock/task/verifier/prompt/rubric/profile gates.

## B2 — atomic, fail-clean CAS-to-durable bootstrap publication

Replace direct-to-final `_promote_control_bootstrap_job` materialization with a hardened publication primitive:

1. Validate/hold the durable parent without following symlink components.
2. Create an unpredictable sibling staging directory; materialize the exact authenticated locator only there.
3. Load, secret-scan, and independently re-inventory/hash the complete staged tree against the same locator after validation.
4. Fsync every file and directory required for crash durability.
5. Publish the whole validated tree in one OS-backed atomic **no-replace** directory operation. Support Darwin (`renameatx_np(RENAME_EXCL)`) and Linux (`renameat2(RENAME_NOREPLACE)`) or fail typed on unsupported platforms; never fall back to overwrite-capable rename/check-then-rename.
6. Fsync the parent. Always remove staging on any failure; never leave the final destination or partial authoritative bytes. Preexisting files/directories/broken symlinks and concurrent publishers refuse without overwrite.

Add failure injection during materialization, after materialization/before validation, after validation/before publish, destination already present, broken/whole/component symlink, and two concurrent publishers. Verify one complete winner at most, loser typed, no staging leak/partial final tree, retry after injected failure succeeds.

## B3 — remove arbitrary `canonical_trial_path` relabel authority

Do not accept a freely caller-selected canonical path merely because bytes compare equal. Replace that surface with a derived, typed source binding anchored to independently authenticated terminal spec/request/locator identity and the canonical bootstrap publication contract. Requirements:

- expected durable job/trial relative path derives internally from exact repo root + canonical `research/evidence/runs` namespace + authenticated job/trial identity; caller cannot override the namespace/name;
- validate exact job ID/name/trial ID/name/provenance/content binding before any model call;
- reject absent/wrong/same-named byte-identical unrelated copy and every symlink/path-component substitution;
- hold fd/inode (or equivalent no-follow identity) through source digesting, analysis and sidecar/admissibility publication, and recheck after analyzer return to close replacement races;
- stored `source_trial_path` and source digests describe that one authority. No temp absolute path drift and no path-only authority.

Preserve normal non-bootstrap `run_trial_analysis` callers and strict `TrialAdmissibilityError`; use a narrow typed binding rather than a permissive path parameter. Add zero-call pre-call cases plus in-call replacement/race and post-sidecar/pre-promotion tamper adversaries.

## B4 — exact unique terminal-event binding

In bootstrap analysis, select exactly one terminal event by:

- exact terminal event type;
- exact job name;
- exact `event.spec_id == request.provenance.spec_id`;
- exact expected attempt/request identity where present.

Require one and only one match, `kind == "job"`, all locator fields/digests valid, and after materialization verify job ID/name plus experiment/spec provenance before any model call. Never “take last”. Add zero-match, duplicate exact, duplicate name/different spec, wrong kind, wrong locator job ID/name/provenance adversaries; all zero calls.

## B5 — explicit CLI ingest CAS store

Add required `--store` to `evallab ingest`. `_ingest_command` resolves exactly that caller argument and has no env/default/getattr fallback. CLI help/golden must expose it and missing argument must fail parser before mutation. Add a true CLI-level test: explicit raw job is archived to that store; delete raw job immediately after archive; projection still succeeds only from returned locator. Wrong/missing store/locator cannot be recovered by scan/discovery.

## B6 — preserve invalid-sidecar behavior without minting authority

`run_trial_analysis` may persist and return an invalid analysis sidecar as the existing contract requires. It MUST NOT call the strict trial-admissibility finalizer for `validation_status != "valid"`; therefore it mints no admissibility authority. Actual attempts to finalize invalid interpretation remain strict and raise `TrialAdmissibilityError`. Restore `test_invalid_citations_produce_invalid_sidecar_not_crash` and add assertion that no admissibility sidecar/authority is produced and no replay ambiguity is introduced.

## Correct the weak tamper test and formatting

Change the bootstrap tamper test to mutate an admissibility-bound source (Architect proved `final-state.json` reaches the right contract while result/control remains valid). Assert the exact `TaskControlEvidenceError` chain contains `TrialAdmissibilityError:trial_admissibility_invalid:source-digest-drift`; do not accept incidental malformed-result/missing-control errors.

Run Ruff format on touched files; `src/evallab/cli.py` and `tests/test_trajectory_quality.py` must be clean.

## Preservation and final verification

- Keep accepted removal of mutable quality APIs/views and settled per-trial quality outputs.
- Keep exact evaluator semantics; no ATIF/schema-version broadening.
- Preserve `storage/settlement.py`, `parquet_io.py`, `attach.py`, `reconciliation.py`, `trajectory_data_quality.py` exact approved authority; state-events explicit `element` producer fix plus strict wrong-name/type/nullability refusals.
- Preserve `data_backfill.py`, historical generator/artifacts/digests, isolation/admission/context/memory/MemGym/staged controls.
- Add source-level tests that fail on each proven bug. Run focused B1–B6 adversaries, AnalysisWorker, bootstrap, quality, CLI/golden, CAS, projection/property/Z3, canonical authority, historical pinned dry-run/shared verifier, state-events, statics/governance/docs/type check.
- Negative-search old quality APIs/path-backed reads/default ingest-store fallback/permissive canonical path. Inspect AST/symbol/test set differences and explain every removal.
- Commit code/tests first, official docs separately only if changed, leave exact head clean.

Page `wH:p9` with commit chain, exact head/tree, per-B1–B6 causal evidence, adversary/test counts, unavailable services/tools, and preservation hashes. Do not claim completion if any adversary/matrix/format remains red.
