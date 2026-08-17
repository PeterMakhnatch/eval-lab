---
status: living
audience:
  - runner
  - operator
---

# Agent profiles: subscription-only identity and qualification

M003 (Platform). Code: `src/evallab/profiles.py`; compat layer:
`src/evallab/credentials.py`; preflight hooks: `runner.profile_for_request` /
`runner.preflight_request`. Queue/CLI wiring is deliberately absent — it is a
later integrator change after M002.

## The contract

An **AgentProfile** is the immutable identity of one runnable configuration:

| Field | Meaning |
|---|---|
| `adapter`, `adapter_version` | Harbor adapter that executes |
| `model` | exact pin; a run may inherit it or match it, never override it |
| `auth_mode` | `none` (controls) / `subscription-auth-file` / `subscription-keychain` |
| `secret_source` | *identifier* only — `keychain:<service>` or `file:<pattern>` |
| `required_files`, `capabilities` | preconditions and features |
| `resources`, `limits` | cpus/memory; timeout/attempt/concurrency ceilings |
| `verified_facts` | dated, independently observed evidence — or empty |
| `digest` | sha256 of canonical (sorted-key) JSON; deterministic |

Validators enforce coherence (controls take no model/credential; billable
profiles need pin + secret source) and reject anything API-key-shaped in any
field. `subscription_environment` remains an allowlist; `scrub_environment`
additionally drops key-shaped names even if allowlisted by mistake.

## Qualification ladder

`declared → installed → credential-ready → smoke-passed → canary-qualified`

Each state is earned separately. The current registry, honestly:

| Profile | Evidence | Ceiling today |
|---|---|---|
| `oracle`, `nop` | pass/fail controls observed across the canary suite | canary-qualified |
| `codex-gpt-5.6-terra` | 2026-08-06 harbor-practice run recorded this exact model | smoke-passed (canary set pending) |
| `claude-code-fable-5` | keychain integration observed; **model string never executed here** | credential-ready at best; never smoke-passed |
| `gemini-cli-declared`, `grok-cli-declared` | none | declared; probe always refuses (`not independently proven`) |

## Fail-closed preflight

`preflight(profile, probe)` returns a `PreflightDecision`:

- controls proceed with no credential;
- a billable profile with no wired probe is **blocked** (fail closed);
- a failing/stale probe blocks with reason and optional expiry.

A blocked preflight stops *before* any trial exists. It cannot be recorded as
reward 0 — the decision type has no reward field, and nothing downstream may
translate it into one. Auth failures are operational events, not capability
evidence.

## Probes

`ProbeResult` carries `ok / expires_at / reason` — three typed fields, no
payload that could hold a token. `KeychainProbe` checks existence via exit
status (never `-w`); `AuthFileProbe` reads the file only to extract an expiry
timestamp and compares it against an injected clock. All seams (home,
security runner, clock, environment, runner) are constructor-injected; tests
run with zero host state (`tests/test_profiles.py`, 22 tests).

## What replacing "hard-coded assumptions" means here

`DEFAULT_AGENT_MODELS` is now derived from the profile registry instead of a
literal dict; agent→credential requirements stay exported for the queue but
the truth lives in profiles. Adding a provider = adding a profile with
evidence, not editing constants scattered across modules.
