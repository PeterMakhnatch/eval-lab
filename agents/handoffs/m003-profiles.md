Status: review-wanted
Last: Profiles + fail-closed preflight implemented; 22 injected tests green; premerge green (ty 28 <= 28)
Next: PR "M003: add subscription agent profiles" — stop at review; merge owner is the integrator, never the author
Blockers: none

# M003 handoff — subscription agent profiles

**Executing agent/model (recorded per mission order): Claude Code
(interactive session), model claude-opus-5[1m] (Opus 5, 1M context).**

Lease (exact): `src/evallab/profiles.py` (new), `src/evallab/credentials.py`,
`src/evallab/runner.py` (additive tail only), `tests/test_profiles.py` (new),
`docs/agent-profiles.md` (new), this file. No CLI/status/dashboard/queue/
policy/ACTIVE.md edits; queue wiring is the later integrator change.

## Design in one paragraph

`AgentProfile` (frozen pydantic, extra=forbid) is the immutable identity:
adapter(+version), exact model pin, subscription auth mode, secret-source
*identifier* (`keychain:<service>` / `file:<pattern>` — validators reject
anything API-key-shaped in any field), required files, capabilities,
resources, limits, dated `verified_facts`, and a sha256 digest over
canonical sorted-key JSON. Qualification is a five-state ladder
(declared/installed/credential-ready/smoke-passed/canary-qualified), each
earned separately. Probes return `ok/expires_at/reason` only, through
injected seams (home, security runner, clock, environment). `preflight`
fails closed: billable profile without a passing probe → blocked decision
with reason, before any trial exists — structurally incapable of becoming
reward zero (the decision type has no reward field; a test asserts this).

## Honest registry

- codex-gpt-5.6-terra: verified (2026-08-06 harbor-practice run).
- claude-code-fable-5: keychain probe real; model string never executed here
  → credential-ready ceiling, no verified facts.
- gemini/grok: declared profiles whose default probe always refuses
  ("not independently proven in this lab").

## Compatibility

queue.py/automation.py imports preserved exactly: `DEFAULT_AGENT_MODELS`
(now derived from the registry — same key/values), `available_credentials`,
`missing_credential_for`, `CLAUDE_OAUTH`, `CODEX_AUTH`,
`AGENT_CREDENTIAL_REQUIREMENTS`. The old `-w` (print-secret) flag in the
keychain probe is gone; existence is checked by exit status with output
discarded unread.

## Evidence

```
$ uv run pytest tests/test_profiles.py -q
......................                                                   [100%]
22 passed
$ uv run ruff check .
All checks passed!
$ bash scripts/premerge.sh
premerge green: Python 3.12; ty 28 <= 28
```

Full suite in premerge: all green (includes existing test_queue/test_runner —
compat proven by their passing, not claimed). No network, Docker, cloud,
benchmark, or live model call was made; tests use zero real credentials.

## Notes for the integrator

- `runner.preflight_request(request)` is the wiring point for the queue after
  M002 merges; `profile_for_request` refuses unknown agents and pin
  mismatches ("change profiles, not pins").
- `scrub_environment` adds a belt-and-suspenders drop of key-shaped names
  even if allowlisted; `subscription_environment` behavior is unchanged.
