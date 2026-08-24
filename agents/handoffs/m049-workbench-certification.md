Status: building
Last: PR #147 merged the portable identity-bound registry prerequisite as `0ad6446`
Next: demonstrate fair-alt, nop-repeat, please-hack, and exact-byte swap refusal on a fresh branch
Blockers: none

# M049 (C) — Portable workbench certification

## Contract

- **Outcome:** bind portable workbench certification to exact task bytes and prove fair-alt, nop-repeat, and please-hack evidence cases.
- **Lane / owner:** Tasks / Tasks lane owner.
- **Exclusive lease:** `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`, `tests/fixtures/task_workbench/**`, and `research/registration/**`; the registry surfaces merged in #147 are read-only unless a new lease is registered.
- **Status:** active; PR #147 merged the durable registry prerequisite, but did not claim the three named workbench cases.
- **Acceptance:** a clean checkout certifies only evidence bound to exact task id/version/package/verifier digests. Named fair-alt succeeds, nop-repeat proves repeatability without promotion, and please-hack is refused with a recorded reason. Swapping task bytes or evidence fails closed.
- **Next executable step:** demonstrate all three named fixtures and exact-byte swap refusal on a fresh branch; record any unmet case rather than claiming it.

## Source evidence and dependencies

M007/#49 supplied the workbench and M038/#133 supplied registry promotion. PR #147 merged at `0ad6446`, repairing non-portable control references and binding retained evidence to package identity. It did not claim the named workbench fixture acceptance above. M052 waits for M049; M047 does not.
