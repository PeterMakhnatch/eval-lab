Status: building
Last: Stub-validated the bounded loop, retry, proposal, discovery, STOP, and Fleet digest.
Next: Rebase onto credential-aware health, run the bounded live Codex pass, then open the PR.
Blockers: Credential-aware health is green in draft PR #1 but not yet on origin/main.

AUTOPILOT is integrating the headless researcher pass without changing the
standing-approvals policy or giving researcher subprocesses write/network
access. The existing headless doctor, STOP marker, executor, and digest remain
the authoritative safety boundaries. Baseline tests pass (36); scoped Ruff is
clean. Repository-wide Ruff has nine upstream failures in CURATOR/RECON-owned
files on the current origin/main and is expected to clear when PR #1 lands.
