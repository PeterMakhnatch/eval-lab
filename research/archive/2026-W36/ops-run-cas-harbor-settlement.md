# Ops Eval Runner: mandatory run CAS settlement and Harbor compatibility

## Base and isolation

- Create a fresh isolated worktree and branch `fix/run-cas-harbor-settlement` from exact commit `93d2e7c184ce607ea57c86f732bd493bfba2489d` (`feat/next-buildout-report`).
- Never switch, reset, clean, merge, or edit the dirty primary checkout.
- One writer owns this worktree.
- Commit the finished focused change and report branch, full commit, worktree, changed files, focused commands/results, and remaining limitations.

## Authority contract

- Evidence CAS is immutable durable truth for a completed generic run.
- PostgreSQL is not raw-artifact authority; Parquet and DuckDB are out of this lane.
- A generic run must not report successful completion or become ingestible until its job directory is archived to configured CAS, the archive is reopened, and its record/archive/content digests and source identity are verified.
- Missing CAS configuration, archive failure, reopen failure, or digest mismatch is a typed unsettled/quarantined failure. Do not swallow it into `evidence-archive-error.txt` while returning success.
- Resolve the actual Harbor executable before launching. Derive the declared supported version/compatibility policy from the repository's existing lock/config authority rather than duplicating a hand-written constant. Refuse unsupported executable/lock drift before producing run evidence.
- Bind the actual executable semantic version and executable-path/file identity digest into the immutable run/execution manifest. Preserve exact existing execution identity inputs.

## Scope

Primary production ownership is the generic execution boundary in `runner.py` plus the narrow existing evidence-store/version helpers needed by that boundary and focused tests. Do not edit PostgreSQL settlement schemas, `evidence/atif.py`, `storage/attach.py`, projection roots, interpretation projections, or historical data.

## Required behavior

1. Add one strict Harbor compatibility resolver/gate used by the real generic run path before process launch.
2. Reject the observed class of `PATH harbor 0.21` versus declared lock `0.22` drift; also reject malformed/unavailable identity. Preserve an explicit supported compatibility mechanism if the repository already has one; do not equate arbitrary string forms.
3. Make CAS archive + reopen + digest verification mandatory for a newly completed generic run. Reuse the stronger campaign settlement pattern rather than creating a second evidence protocol.
4. Return/bind the verified immutable CAS record as the settled evidence reference. A mutable Harbor job directory may remain an operational workspace, never the durable completion authority.
5. Preserve failure evidence, but surface a typed non-success state/exception that callers cannot confuse with completion.
6. Migrate every generic-run caller and focused contract test in this lane; no aliases, bypass flags, silent fallback, or legacy success path.

## Focused acceptance

- A matching declared/actual Harbor identity reaches execution and records executable version plus identity digest.
- A semantic-version mismatch refuses before Harbor launch and before run evidence is emitted.
- Missing/unparseable Harbor identity refuses closed.
- Missing CAS root/configuration refuses settlement.
- Archive write, reopen, record digest, archive digest, or content digest failure never returns a successful run.
- A successful generic run returns/binds the verified CAS record, and a focused reload proves the record points to the same content.
- Existing campaign CAS semantics remain unchanged.
- Run only focused tests covering changed behavior, then touched-file Ruff/format checks. Skip project-wide suites.

## Review source

Architect review: `/tmp/system-architect-e2e-data-architecture-review.md`, especially lines 60-66, 98-116, 195-217, and acceptance criteria 1-2 and 10.
