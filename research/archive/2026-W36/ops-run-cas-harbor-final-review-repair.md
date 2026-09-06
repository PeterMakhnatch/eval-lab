# Ops Eval Runner: repair final run CAS/Harbor review blockers

## Exact target

- Continue as sole writer in `/Users/petermakhnatch/Developer/eval-lab/.worktrees/eval-runner-cas-harbor-settlement` on `fix/run-cas-harbor-settlement`.
- Current head: `f19081c2db29b12f818d68a356671cd4f200f79a`; base remains `93d2e7c184ce607ea57c86f732bd493bfba2489d`.
- Review report: `/tmp/system-architect-run-cas-harbor-final-review-f19081c2.md`.
- Preserve the clean explicit `SettledRun` cutover and all passing behavior. Do not touch the primary checkout. Do not launch a model run.

## Blocking repairs

### R1. Authenticate the complete canonical reopened record

Move the canonical Evidence CAS manifest parse/schema/reconstruction authority into `src/evallab/evidence_store.py`; do not duplicate an Ops-only record schema in `runner.py`. One shared loader must:

- parse record bytes as an object, require the complete canonical manifest schema, validate field types and values (including `schema_version`, canonical `blob_path`, `file_count`, `uncompressed_bytes`, `archived_at`, IDs/digests/URI/source), and refuse missing/extra/malformed fields;
- derive/validate the canonical manifest path `records/<kind>/<record_id>.json` and blob path from the trusted store root/content digest, not trust path strings blindly;
- reconstruct the authoritative `EvidenceArchive` from reopened bytes;
- prove archive digest, restored content digest, source content digest, file count, and uncompressed byte count against reopened evidence;
- return the reopened authoritative object/bytes so `SettledRun.cas_record` and `record_digest` bind the same exact canonical record.

Tampering each of `blob_path`, `file_count`, `uncompressed_bytes`, and `schema_version` must refuse with `evidence_cas_unsettled`; retain all existing core-field/archive/source tamper refusals. Include missing/extra/wrong-type/noncanonical path cases.

### R2. Typed settlement failure and terminal executor state

At the parse boundary, valid non-object JSON (for example `[]`), invalid UTF-8, invalid JSON, and manifest schema failures must normalize to `ExecutionFailure(reason="evidence_cas_unsettled")`. Every exception escaping the post-Harbor settlement phase must write terminal executor status `failed` before propagation. Expected storage/parse/schema failures stay typed; unexpected programmer exceptions retain their original type/cause and are not disguised, but still leave terminal `failed` state. Add end-to-end tests for both a non-object record and an unexpected injected settlement exception; neither may leave `running`.

### R3. Bind launch to captured executable bytes

Extend the Harbor runtime identity with stable file identity as needed. At the actual launch boundary, immediately re-stat and re-digest the exact resolved executable and compare path/file identity/metadata/digest to the captured identity before invoking it. Refuse any inode, size, relevant timestamp, or digest change with a typed Harbor identity/drift failure; do not execute changed bytes. The immutable run metadata must describe the executable bytes actually launched. Add a test that replaces the resolved executable between identity capture and launch and proves the replacement is never executed and executor state is terminal failed.

Do not weaken the exact `uv.lock` version derivation or bare-semver grammar. Keep CAS mandatory before launch and settlement mandatory before successful return.

## Verification and delivery

Run focused `tests/test_runner.py tests/test_queue.py`, the three adversarial regressions above plus existing tamper/source/restore/version cases, touched Ruff/format, and diff-check. Commit one focused follow-up on the same branch. Page the parent with exact new commit, full range, changed paths, exact test counts, and any unresolved risk. Do not integrate.
