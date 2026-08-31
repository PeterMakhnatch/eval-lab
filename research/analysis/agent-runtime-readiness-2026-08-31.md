---
status: observed
reviewed: 2026-08-31
audience:
  - operator
  - platform
---

# Agent runtime readiness: what “slot in any model” requires

## Result

Gemini 3.7 Flash High is runnable. The failed RSI attempt was an invocation/configuration failure, not a model failure.

The failed job used Harbor's `antigravity-cli` adapter without either credential-staging switch required by that adapter:

- `AGY_FORCE_AUTH_JSON=1`, or
- `AGY_AUTH_JSON_PATH=<token file>`.

The local Antigravity OAuth token existed, but the run did not copy it into the Docker agent environment. The CLI opened an interactive OAuth flow inside a headless container and timed out after 60 seconds. No model tokens were consumed and no agent work occurred. The verifier subsequently scored the unchanged baseline; that score is not evidence about Gemini.

A corrected smoke was run with the existing OAuth token staged into the container:

| Field | Observed value |
|---|---|
| Adapter | `evallab.harbor_antigravity:AntigravityCliCapture` |
| Model | `google/gemini-3.7-flash-high` |
| Task | `local-lab/event-summary` |
| Result | reward `1.0`, no exception |
| Runtime | 47 seconds |
| Trajectory | `trajectory.json`, 16 steps, 7 tool calls after ingestion |
| Raw job | `runs/agent-readiness/smoke-evallab-antigravity-gemini37` |

The same model also passed through Harbor's built-in Antigravity adapter with reward `1.0`, but the repo-owned adapter is the correct Eval Lab lane because it preserves structured stream events and writes an ATIF trajectory.

## Why “configured” did not mean “runnable”

A model name is only one component of a runnable lane:

$$
\text{Runnable lane} = \text{adapter} + \text{model pin} + \text{credential transport} + \text{environment} + \text{trajectory capture} + \text{verified smoke}.
$$

The failed RSI run had a valid adapter and model pin, but lacked credential transport. The host `agy` session was valid; the isolated Docker container did not inherit it automatically.

This distinction also applies to every other provider. A host CLI saying “logged in” proves host-session readiness, not Harbor-container readiness.

## Current readiness matrix

Observed on 2026-08-31. “Host ready” and “Harbor ready” are deliberately separate.

| Lane | Host credential probe | Harbor/container status | Current verdict |
|---|---|---|---|
| `oracle`, `nop` | No credential required | Repeated controls exist | **Ready / canary-qualified** |
| Codex | `~/.codex/auth.json` present | Successful RSI and prior Harbor runs | **Ready** |
| Antigravity / Gemini 3.7 Flash High | `agy models` succeeds | Corrected authenticated smoke passed with ATIF capture | **Ready** |
| Other listed Antigravity models | Same active subscription session | Shared credential transport; not every model pin has a current smoke | **Credential-ready; smoke each pin before campaigns** |
| Cursor CLI models | `cursor-agent status` succeeds | Harbor's installed `cursor-cli` adapter requires `CURSOR_API_KEY`; the opaque host subscription session is not transported into Docker | **Host-ready, Harbor-blocked** |
| Claude Code | Expected keychain item absent | Model pin is unproven in this lab | **Blocked** |
| DeepSeek mini-swe-agent | `DEEPSEEK_API_KEY` / `MSWEA_API_KEY` absent | Only install-level evidence exists | **Blocked** |
| Standalone `gemini-cli` | Declared profile only | No proven credential/run path | **Blocked; use Antigravity or Cursor instead** |
| Standalone `grok-cli` | Declared profile only | No proven credential/run path | **Blocked; Cursor Grok is the configured route** |

## The slot-any-model runtime contract

A profile may be selected for paid evaluation only after all gates pass:

1. **Declared** — adapter and exact model pin exist.
2. **Installed** — adapter and CLI install in the target environment.
3. **Host credential-ready** — the provider session/key/file is valid without exposing its value.
4. **Credential transport-ready** — the credential is explicitly seeded or mounted into the isolated runner.
5. **Model-compatible** — the adapter lists or accepts the exact model pin and reasoning configuration.
6. **Environment-compatible** — OS, Docker, network policy, mounts and task resources are supported.
7. **Trajectory-complete** — structured model/tool events are captured, not only final text.
8. **Smoke-passed** — one cheap deterministic task finishes with a valid verifier outcome.
9. **Canary-qualified** — repeated controls demonstrate stable behavior before a campaign.

A failed gate is an infrastructure refusal. It must stop before a scored trial and must never be translated to reward `0`.

## Current UX gaps

- `evallab run --allow-billable` still refuses non-control agents because direct paid execution is restricted to the standing-policy queue. The flag acknowledges spend but does not bypass governance.
- Raw `harbor run` is an escape hatch and does not automatically apply Eval Lab's credential environment, custom adapter routing or capture contract.
- The profile registry records host-session evidence but does not yet expose container credential-transport readiness as a first-class state.
- Cursor profiles currently overstate benchmark-run readiness: the host subscription works, while the Harbor adapter expects an API key.

## Recommended operator surface

Add one provider-neutral surface rather than more provider-specific commands:

```text
evallab agents list
evallab agents doctor PROFILE
evallab agents smoke PROFILE --task canary/event-summary
evallab agents qualify PROFILE --repeats 3
```

`agents list` should display every gate above and one blocking reason. `agents smoke` should be the only low-cost paid direct path; full campaigns should continue through the standing-policy queue. This gives “slot any ready model” ergonomics without discarding provenance, spend controls or fail-closed credential handling.
