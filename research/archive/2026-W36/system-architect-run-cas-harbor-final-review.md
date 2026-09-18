# System Architect final review: generic run CAS and Harbor identity

## Exact target

- Branch: `fix/run-cas-harbor-settlement`
- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/eval-runner-cas-harbor-settlement`
- Base: `93d2e7c184ce607ea57c86f732bd493bfba2489d`
- Head: `f19081c2db29b12f818d68a356671cd4f200f79a`
- Review exact range `93d2e7c..f19081c2` read-only. Do not edit or integrate.
- Source architecture review: `/tmp/system-architect-e2e-data-architecture-review.md`.
- Implementation brief: `research/inbox/ops-run-cas-harbor-settlement.md`.

## Required authority checks

### Harbor executable/runtime gate

- Derive the declared Harbor version from the repository's actual lock authority without a duplicate constant or ambiguous package selection.
- Resolve the exact executable before staging/launch, digest that exact resolved executable, and launch that same path; reject PATH/identity drift and malformed/unavailable executable identity before run evidence is produced.
- Confirm the supported `harbor --version` grammar is in fact the observed executable grammar. The implementation accepts only one bare canonical semver; arbitrary prefix/suffix/multiple-version output must fail closed.
- Actual version must satisfy the intended lock/compatibility policy. This branch chooses exact semantic-version equality; approve only if that matches current repository authority rather than an undocumented compatibility range.
- Lab metadata must bind declared/actual version, resolved executable path, and executable digest.

### Mandatory generic-run CAS settlement

- Missing `EVALLAB_EVIDENCE_STORE_ROOT` must refuse before Harbor launch.
- A process is not complete until the sanitized job tree is archived, the canonical record and archive bytes are reopened, archive/content/URI/source identity are compared, CAS bytes are restored and re-digested, and the mutable source remains byte-identical through settlement.
- Archive write, record read/parse, archive read/digest, restore, restored-content digest, record field, URI, or concurrent source mutation must never return success. Expected storage/parser failures should surface one typed unsettled result and leave executor state failed; unexpected programmer failures must not be disguised as success.
- Secret scanning/sanitization must precede CAS archival.
- No `evidence-archive-error.txt` success path or optional CAS bypass remains.

### Explicit result and caller cutover

- `SettledRun` must be one explicit boring API: operational `job_dir`, verified canonical `cas_record`, and digest of exact reopened record bytes. It must not masquerade as a `Path` or keep an ambiguous union/legacy return.
- All production generic-run callers in the exact range must consume `settled_run.job_dir`; no missed caller may receive the dataclass where a Path is expected.
- `cas_record.manifest_path` must be the canonical existing `records/job/<record_id>.json` handoff Engineer Data can parse without depending on an Ops-only schema.
- Campaign-specific settlement may remain unchanged, but the generic runner must not be represented as catalog/projection-ready merely because CAS settled.

## Adversarial validation

Independently run focused tests and add temporary non-committing probes where coverage is narrow. In particular verify:

1. exact matching lock/runtime succeeds and the exact digested path is launched;
2. current observed 0.21.x executable against 0.22.x lock refuses before launch;
3. malformed/multiple/prefixed version output refuses;
4. missing CAS refuses before launch;
5. record content/archive/source/URI tampering, archive byte tampering, restored content mutation, and source mutation during settlement all refuse;
6. successful settlement returns the canonical record path and exact record digest;
7. queue caller behavior remains correct for success, failure, and retries.

Reported implementation evidence: `107 passed` across `tests/test_runner.py tests/test_queue.py`; touched Ruff/format and `git diff --check` clean.

Write `/tmp/system-architect-run-cas-harbor-final-review-f19081c2.md` with exact verdict `APPROVE` or `BLOCK`, source evidence, commands/results, and concrete blockers. Page the parent with the verdict. Do not modify the branch.
