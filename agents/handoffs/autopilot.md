Status: ready-for-rebase
Last: Completed live analyst/synthesizer/proposer pass 01KZZCK33HJM4R8HW3V0Y25DXE with three no-tool calls and a proposed-only spec.
Next: Rebase onto origin/main after credential-aware health lands, rerun checks, push, and open the AUTOPILOT PR.
Blockers: Credential-aware health is green in draft PR #1 but not yet on origin/main.

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
- The draft discovery is `D-20260815-KTXJSHGZ` in
  `digests/DISCOVERIES.md`; a Fleet render includes it and the researcher cost.
- `uv run pytest -q` passes 41 tests and `uv run ruff check .` is clean on the
  credential-aware integration base.
