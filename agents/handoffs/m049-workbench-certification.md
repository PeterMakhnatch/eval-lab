Status: review-wanted
Last: implemented packet-bound registry certification plus fair-alt, nop-repeat, invalid, and replayable please-hack fixture contracts
Next: review the exact diff and run the repository validation gates outside this no-validation task
Blockers: none

# M049 (C) — Portable workbench certification

## Contract

- **Outcome:** bind portable workbench certification to exact task bytes and prove fair-alt, nop-repeat, and please-hack evidence cases.
- **Lane / owner:** Tasks / Tasks lane owner.
- **Exclusive lease:** workbench, registry/schema/CLI binding, focused tests and fixtures, durable `research/registration/candidates/**`, and M049 board/handoff surfaces.
- **Status:** review-wanted; implementation is complete, but this handoff makes no unexecuted validation claim.
- **Acceptance:** a certificate is a SHA-bound evidence packet with separate correctness, soundness, completeness, solvability, difficulty, and realism axes. The local uppercase fixture defines oracle ×3, nop ×2, three invalid probes, a byte-distinct fair alternative, and a retained please-hack replay. Registry binding re-reads packet and candidate bytes and rejects tamper, task replay, circular identities, and missing replay evidence. Legacy records explicitly say `legacy_missing` and retain their portable oracle/nop interpretation.
- **Next executable step:** independent review and validation; record the observed result rather than inferring it from implementation.

## Source evidence and dependencies

M007/#49 supplied the workbench and M038/#133 supplied registry promotion. PR #147 merged at `0ad6446`, repairing non-portable control references and binding retained evidence to package identity. It did not claim the named workbench fixture acceptance above. M052 waits for M049; M047 does not.
