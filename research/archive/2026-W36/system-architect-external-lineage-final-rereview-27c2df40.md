# System Architect final narrow external-lineage closure review

## Exact target

- Worktree/branch: `/private/tmp/eval-lab-external-lineage-b1-b4-repair`, `fix/external-lineage-b1-b4-repair`
- Base: `721afd5245c92b5b7761a64dc704a9a258ee6279`
- Prior blocked head: `817b7068fd444711c05d6d7de21257e8d6782d2c`
- New head: `27c2df40` (resolve exact full hash in worktree)
- Prior report: `/tmp/system-architect-external-lineage-narrow-rereview-817b7068.md`
- Read-only. Do not edit or integrate.

Review the exact follow-up and confirm the one residual is mechanically closed before any mutation: an already-registered historical `m049-v1` record may only perform same-actor read/reopen/audit verification and return exact stored bytes unchanged. Different actor, replacement certification, parsed CLI mutation, or any other attempted update must refuse and preserve the exact registry bytes. Reconfirm candidate->registered requires bound v2, canonical timestamps remain exact, and B2/B3 remain closed.

Reported evidence: 263 focused tests and touched Ruff/format clean. Independently rerun the new production API/CLI/read-only test and a direct replacement-certification probe, plus focused suites if needed. Write `/tmp/system-architect-external-lineage-final-rereview-27c2df40.md` with exact `APPROVE` or `BLOCK`, then page the parent. Do not commit.
