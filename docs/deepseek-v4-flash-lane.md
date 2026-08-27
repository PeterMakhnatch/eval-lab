---
status: living
audience:
  - operator
  - runner
---

# DeepSeek V4 Flash Harbor lane

This lane runs the agentic `transaction-reconciliation` canary through Harbor's generic
`mini-swe-agent` adapter and LiteLLM with the exact model selector
`deepseek/deepseek-v4-flash`. It does not add API credentials to Eval Lab's
subscriptions-only profile registry or `subscription_environment()` allowlist.

The lane is declaration- and installation-ready only. Neither preparation nor the
checks below establish model availability or capability. Do not run the canary until
the previously exposed credential has been revoked and a fresh local credential is
present.

## Local environment names

The only credential name used by this lane is `DEEPSEEK_API_KEY`. Harbor resolves it
from the local process environment and forwards it to mini-swe-agent/LiteLLM without
putting its value in the Harbor argv.

Live execution also requires the local operator-attestation name
`EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED`. Its presence certifies that the exposed key
was revoked and the current local key is fresh. Keep both values in a local secret
manager or untracked shell environment. Never paste, print, commit, or pass either
value with Harbor's `--agent-env` option.

`MSWEA_API_KEY` is deliberately unsupported by this lane. The no-model paths remove
both provider and adapter key names before starting Harbor. Every path removes
`DEEPSEEK_BASE_URL`, `DEEPSEEK_API_BASE`, `OPENAI_BASE_URL`, and `OPENAI_API_BASE`;
live execution also removes `MSWEA_API_KEY`. The fresh provider key therefore follows
Harbor's canonical DeepSeek route instead of an inherited alias or endpoint override.

## Safe preparation

Run the presence-only credential and container probe:

```bash
scripts/deepseek-v4-flash-lane probe
```

It reports only `set`/`unset` and CLI/daemon reachability. It does not inspect a key,
contact DeepSeek, build a container, or claim that any Compose service is healthy.
The command exits nonzero until Harbor, Docker, the Docker daemon, and both required
environment names are present.

Run the install-only compatibility smoke:

```bash
scripts/deepseek-v4-flash-lane install-smoke
```

This invokes Harbor 0.21's `--install-only` path for the canary task. Harbor builds the
task environment and installs mini-swe-agent with its LiteLLM dependency, then exits
before `agent.run()` and verification. The wrapper explicitly removes model credential
names from the Harbor process.

Inspect the resolved canary configuration without running an agent or verifier:

```bash
scripts/deepseek-v4-flash-lane plan-canary
```

The printed plan must show one task, one attempt, one concurrent trial, one concurrent
agent, zero retries, the `mini-swe-agent` adapter, and the exact model selector. This
path also removes model credential names before invoking Harbor.

## One-trial canary

After rotating the exposed key and providing both required local environment names,
run:

```bash
scripts/deepseek-v4-flash-lane canary
```

The wrapper fails closed if either name is absent. The command runs only
`library/tasks/transaction-reconciliation`, an agentic capability task that requires
inspecting and repairing a seeded SQLite ledger. It fixes the experiment at one task,
one attempt, concurrency one, zero retries, an 8,192-token response ceiling, and a
$2.50 mini-swe-agent cost limit. Only `api.deepseek.com` is added to Harbor's agent
phase host allowlist. Results remain under ignored `runs/deepseek-v4-flash/` for review
and ingestion through the normal evidence workflow.
