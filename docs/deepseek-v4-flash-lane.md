---
status: living
audience:
  - operator
  - runner
---

# DeepSeek V4 Flash Harbor lane

This lane runs the registered `syn-funcdag-easy` task through a narrow
credential-transport subclass of Harbor's generic `mini-swe-agent` adapter.
Installation, execution, trajectory conversion, and model routing remain Harbor's
mini-swe-agent + LiteLLM path with the exact selector
`deepseek/deepseek-v4-flash`.

## Credential contract

The admitted host names are `DEEPSEEK_API_KEY` and its compatibility alias
`MSWEA_API_KEY`. At least one must be non-empty. When only the alias is present,
the runner copies it to the canonical name in memory. The values are forwarded
only when the selected agent is the repo-owned DeepSeek adapter; unrelated Harbor
runs receive neither variable.

Docker Compose reads the canonical name from the operator process and mounts it
as `/run/secrets/evallab_deepseek_api_key`. The adapter loads that file only
inside the agent shell. Harbor configuration, Harbor and Docker child argv, and
per-exec environment mappings contain no key value. Any log-safe environment
rendering replaces admitted values with `<redacted>`.

Live execution also requires the presence-only operator attestation
`EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED`. Keep credentials in a local secret
manager or untracked shell environment. Never paste, print, commit, or pass a
value through Harbor's `--agent-env` option.

Every path removes inherited `DEEPSEEK_BASE_URL`, `DEEPSEEK_API_BASE`,
`OPENAI_BASE_URL`, and `OPENAI_API_BASE`. The no-model install and plan paths
also remove both credential names. Live execution canonicalizes the alias and
removes it before starting Harbor.

## Exact operator command

Run this command from the same Terminal tab where the fresh key variables are
already loaded. Existing OMP or agent processes do not inherit later shell
environment changes and must not be used as a credential probe.

```bash
EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED=1 scripts/deepseek-v4-flash-lane operator-run
```

The command performs these steps in order:

1. Reports only `set`/`unset` for both admitted names and the attestation, plus
   Harbor, Docker, Docker Compose, and daemon reachability.
2. Runs Harbor's `--install-only` path with both model credentials removed.
   This builds the task environment and installs mini-swe-agent + LiteLLM but
   creates zero model trials.
3. Resolves `syn-funcdag-easy` through `library/registry`, requires state
   `registered` with bound workbench certification, and runs exactly one trial.

The live task is staged outside the source package. On hosts that cannot enforce
the canonical verifier network policy, Eval Lab records its existing
host-network adaptation. The credential-bearing agent phase adds an exact
`api.deepseek.com` allowlist. The committed registered package is unchanged.

The trial is fixed at one task, one attempt, one concurrent trial, one concurrent
agent, zero retries, an 8,192-token response ceiling, and a $2.50 mini-swe-agent
cost limit. After install and live execution, the wrapper scans the corresponding
run artifacts for either loaded credential value and fails if one appears. It
never prints a matched value.

For inspection without a model call:

```bash
scripts/deepseek-v4-flash-lane plan-funcdag-easy
```

Results remain under ignored `runs/deepseek-v4-flash/` for review and ingestion
through the normal evidence workflow.
