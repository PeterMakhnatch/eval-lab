# Study 04 — Claude-code vs Codex on pinned event-summary

**Hypothesis.** Holding the pinned event-summary task, attempts=3, and
docker fixed, claude-code pass@3 differs from Codex pass@3 by enough to
see in n=3. n=3 will not support a ranking; the first value of the pair is
"does claude-code complete a canary trial at all once the token exists."

**One variable.** Agent ∈ {codex, claude-code}.

**Fixed.** `task=canary/event-summary`, `attempts=3`, docker, no extra
instruction. Codex cell is Study 01's event-summary spec.

**Policy.** `canary` lists both agents. This spec is admissible.

**Inherited credential record.** The removed RUNNER worktree reported
`keychain_readable=false` for service `harbor-practice-claude-oauth` and a
quarantined queue. PROGRAM-REPAIR did not probe current credential state, so
availability is unresolved here and the historical queue outcome is not a
current dispatch claim.

**Cost.** $2.50. Combined with Study 01's three Codex canaries and Study
02's k=1 arm, admitted estimates total $10.83, under $20.

**Next spec this implies.** When the keychain item exists, tick this spec
before designing a 5-task agent comparison. If claude-code fails with an
auth exception, that is harness/credential, not a capability result.

## 2026-08-15 PROGRAM reconciliation

Codex cell is now the scored 2026-08-15 event-summary job, not the
2026-08-14 `ValueError` jobs. No claude-code trial exists in primary
`runs/`. Credential state is inherited runtime provenance and was not probed by
PROGRAM-REPAIR; it remains unresolved here.
