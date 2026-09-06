# System Architect narrow re-review: external lineage final closure

## Exact target

- Branch/worktree: `fix/external-lineage-b1-b4-repair`, `/private/tmp/eval-lab-external-lineage-b1-b4-repair`
- Base: `721afd5245c92b5b7761a64dc704a9a258ee6279`
- Prior blocked head: `9aa63c31620bf0793a283f1919acc677f4e4cbbd`
- New head: `817b7068fd444711c05d6d7de21257e8d6782d2c`
- Review exact follow-up `9aa63c31..817b7068`, then confirm full-range invariants `721afd52..817b7068`.
- Prior report: `/tmp/system-architect-external-lineage-b1-b4-final-review-9aa63c31.md`.
- Read-only. Do not edit or integrate.

## Narrow closure checks

1. **Legacy v1 is read/reopen/audit only.** Every state-changing `candidate -> registered` path, including `register_task` without a newly supplied packet and parsed CLI registration, must require bound `m049-v2`. A valid stored bound `m049-v1` candidate with valid controls must remain unregistered. `verify_certification_packet` may allow v1 only for an already registered historical v1 record; it must not expose a general state-changing bypass.
2. **One canonical UTC serialization.** Transformation `created_at`, record `built_at`, and attestation `built_at` must accept only whole seconds as `YYYY-MM-DDTHH:MM:SSZ` or nonzero fractional seconds with exactly six microsecond digits. Reject `.0Z`, `.00Z`, `.000000Z`, shortened nonzero fractions, more than six digits, offsets, naive/date/lowercase/space aliases. Require parse + canonical reserialization byte equality and retain exact record/attestation string equality.
3. Reconfirm prior B2 strict candidate source authority and B3 parsed distinct two-build attestations are unchanged.
4. Reconfirm no LoCoMo activation, registry data, canary authorization, model execution, or unrelated worktree/root changes.

Reported evidence: 262 focused tests pass and touched Ruff/format are clean. Independently rerun the two focused suites, the two prior adversarial probes, timestamp alias matrix, parsed CLI legacy-candidate transition, and touched Ruff/format. Write `/tmp/system-architect-external-lineage-narrow-rereview-817b7068.md` with exact `APPROVE` or `BLOCK` and page the parent. Do not commit.
