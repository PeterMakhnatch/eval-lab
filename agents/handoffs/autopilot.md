Status: blocked-on-upstream
Last: Completed and validated live pass 01KZZCK33HJM4R8HW3V0Y25DXE; aborted the required rebase at an unowned CURATOR conflict.
Next: After PR #1 lands, rebase the AUTOPILOT-only commits onto origin/main, rerun checks, push, and open the PR.
Blockers: PR #1 is still draft; `git rebase origin/main` conflicts in agents/handoffs/curator.md while replaying 7189fe4, so protocol forbids resolving or skipping it.

AUTOPILOT is integrating the headless researcher pass without changing the
standing-approvals policy or giving researcher subprocesses write/network
access. The existing headless doctor, STOP marker, executor, and digest remain
the authoritative safety boundaries.

Acceptance evidence:

- Deterministic stubs proved the four-call validation retry, proposal sidecar,
  append-only discovery, Fleet digest, and STOP deferral with zero calls.
- The live 2026-08-15 pass emitted
  `queue/proposed/codex-01KZZCN7X9PA643W1QCKQNNNY5.json`; its manifest and all
  Pydantic outputs are under
  `queue/researchers/passes/2026-08-15/01KZZCK33HJM4R8HW3V0Y25DXE/`.
- All three live JSONL streams contain zero tool events. The completed pass was
  attributed $3; conservative attribution for all launcher troubleshooting was
  $16 against the unchanged $20 daily ceiling.
- A Claude-only healthy doctor result was stub-validated to record
  `missing_credential:codex` with zero researcher calls; catalog spend is
  refreshed before each call reservation.
- The draft discovery is `D-20260815-KTXJSHGZ` in
  `digests/DISCOVERIES.md`; a Fleet render includes it and the researcher cost.
- `uv run pytest -q` passes 41 tests and `uv run ruff check .` is clean on the
  credential-aware integration base.
