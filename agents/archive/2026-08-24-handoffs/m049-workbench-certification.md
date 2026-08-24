Status: done
Last: merged packet-bound executable task certification in PR #151 (`fbc21d9`)
Next: none; lease spent
Blockers: none

# M049 (C) — Portable workbench certification

## Contract

- **Outcome:** bind portable workbench certification to exact task bytes and prove fair-alt, nop-repeat, and please-hack evidence cases.
- **Lane / owner:** Tasks / Tasks lane owner.
- **Exclusive lease:** workbench, registry/schema/CLI binding, focused tests and fixtures, durable `research/registration/candidates/**`, and M049 board/handoff surfaces.
- **Status:** merged via PR #151; lease spent.
- **Acceptance:** a certificate is a SHA-bound evidence packet with separate correctness, soundness, completeness, solvability, difficulty, and realism axes. The local uppercase fixture defines oracle ×3, nop ×2, three invalid probes, a byte-distinct fair alternative, and a retained please-hack replay. Registry binding re-reads packet and candidate bytes and rejects tamper, task replay, circular identities, and missing replay evidence. Legacy records explicitly say `legacy_missing` and retain their portable oracle/nop interpretation.
- **Next executable step:** none.

## Source evidence and dependencies

M007/#49 supplied the workbench and M038/#133 supplied registry promotion. PR #147 merged the portable registry base at `0ad6446`; PR #151 then merged executable certificate binding before M052/#155 consumed it. M047 remains independent.
