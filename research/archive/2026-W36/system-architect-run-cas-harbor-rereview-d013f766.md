# System Architect re-review: generic run CAS and Harbor identity

## Exact target

- Worktree/branch: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/eval-runner-cas-harbor-settlement`, `fix/run-cas-harbor-settlement`
- Base: `93d2e7c184ce607ea57c86f732bd493bfba2489d`
- Prior blocked head: `f19081c2db29b12f818d68a356671cd4f200f79a`
- New exact head: `d013f766c812bd7e3d62760a1e1ec3a52cb0b26b`
- Follow-up chain: `d558eb0d` → `cccd56c2` → `333ab1d7` → `7f633f68` → `d013f766`
- Prior report: `/tmp/system-architect-run-cas-harbor-final-review-f19081c2.md`
- Read-only. Do not edit, integrate, or launch a model.

## Required closure checks

1. One shared `evidence_store` canonical record authority validates the complete reopened object and exact producer bytes, schema/types, canonical record/blob/source paths and timestamp, archive digest, restored digest/count/bytes, and optional live source equality. Returned `EvidenceArchive` and `record_digest` bind those exact reopened bytes. Record-path-only reopen works without the mutable source tree for Engineer Data.
2. Every individual record field and byte-level tamper refuses: `schema_version` including bool, blob/source paths, counts/bytes, content/archive/URI/IDs/kind/time, missing/extra/wrong types, whitespace/key order/duplicate-style encoding, invalid UTF-8, non-object JSON, restored/archive/source mutation.
3. Expected read/parse/schema failures surface typed `evidence_cas_unsettled`; every settlement exception leaves terminal executor `failed`. Unexpected exceptions preserve original type/message while also leaving `failed`.
4. Harbor exact lock/version/path/digest/file identity is rechecked at the launch boundary; replacement/drift is refused before changed bytes run and state is terminal failed.
5. Explicit `SettledRun` cutover remains complete; no Path shim, optional CAS success, campaign-specific readiness conflation, or caller regression.

Reported evidence is 120 focused runner/queue tests plus touched Ruff/format and diff-check. Independently rerun focused suites and adversarial probes from the prior report, including record-path-only reopen and executable replacement. Write `/tmp/system-architect-run-cas-harbor-rereview-d013f766.md` with exact `APPROVE` or `BLOCK`, then page the parent. Do not commit.
