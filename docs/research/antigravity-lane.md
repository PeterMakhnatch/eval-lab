---
status: living
audience:
  - builder
  - runner
---

# Antigravity (AGY) Agent Lane Research & Architecture

## Executive Summary

Peter's Google AI Pro access reaches the eval lab through **Antigravity** (`agy`), replacing `gemini-cli` which is deprecated for individual accounts (`IneligibleTierError`).

This document records the architecture of Harbor's `antigravity-cli` adapter, its execution model, how authentication works, the verified CLI models, and the integration into `evallab.profiles` and `evallab.credentials`.

---

## 1. Harbor Adapter Investigation

Harbor 0.21.0 provides two Antigravity agent adapters:
1. **`antigravity-cli`** (`harbor.agents.installed.antigravity_cli.AntigravityCli`):
   - **Installation:** Installs the `agy` Go CLI binary inside the environment using:
     ```bash
     curl -fsSL https://antigravity.google/cli/install.sh | bash
     ```
   - **Execution command:** In `AntigravityCli.run()`, Harbor executes:
     ```bash
     $HOME/.local/bin/agy --dangerously-skip-permissions --model <model> --prompt=<instruction> 2>&1 </dev/null | stdbuf -oL tee /logs/agent/antigravity-cli.txt
     ```
   - **Model resolution:** Harbor requires the model argument to be in `provider/model_name` format (e.g. `google/gemini-3.7-flash-high`), splitting on `/` to pass `--model gemini-3.7-flash-high` to `agy`.
   - **Trajectory capture:** Extracts session logs from `~/.agy/antigravity-cli/tmp/session-*.jsonl` and converts them to ATIF format.
   - **No ACP / agentapi requirement:** The adapter interacts directly with the `agy` CLI binary. The binary `~/.gemini/antigravity-cli/bin/agentapi` is a shell wrapper (`exec "/Users/petermakhnatch/.local/bin/agy" agentapi "$@"`) for subcommands like `new-conversation` and `send-message`, but Harbor's `antigravity-cli` adapter executes `$HOME/.local/bin/agy` directly.

2. **`antigravity-sdk`** (`harbor.agents.installed.antigravity_sdk.AntigravitySDK`):
   - Runs `/installed-agent/run_agent.py` using Google's Antigravity Software Agent SDK.
   - **Requires `GEMINI_API_KEY`**: Because API-key environment variables are forbidden in our subscriptions-only lab, `antigravity-sdk` cannot be used in this lab. `antigravity-cli` is the only supported subscription transport.

---

## 2. Authentication Architecture

### How `agy` Manages Auth
- On macOS/desktop, `agy` signs in via browser-based Google OAuth and stores credentials in the OS keyring and local application state under `~/.gemini/antigravity-cli/`.
- Once authenticated, `agy` operates headlessly without prompting for credentials.
- In containerized/headless environments (e.g. Docker), Harbor supports token injection via `AGY_AUTH_JSON_PATH` or `AGY_FORCE_AUTH_JSON=1`, copying `~/.gemini/antigravity-cli/antigravity-oauth-token` into `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`.

### Auth Mode & Probe Seam
The lane uses `auth_mode="subscription-cli-session"` with `secret_source="cli:agy models"`.
- `CliSessionProbe` executes `("agy", "models")` and checks for exit status `0` and expected stdout marker `"gemini"`.
- It never accesses, logs, or exports secret tokens.
- When unauthenticated (e.g. invalid session or unauthenticated environment), `agy models` exits `1` with:
  ```
  Fetching available models...
  Error: Please sign in to view available models. Launch the CLI without arguments to sign in.
  ```
- When authenticated, `agy models` exits `0` and outputs available model identifiers.

---

## 3. Verified Ground Truth & Commands

Verified in this lab on 2026-08-19:

### CLI Version
```bash
$ ~/.local/bin/agy --version
1.1.15
```

### Headless Execution Check
```bash
$ ~/.local/bin/agy -p "Reply with exactly: AGY_LANE_OK"
AGY_LANE_OK
```

### Model Availability (`agy models`)
```bash
$ ~/.local/bin/agy models
Fetching available models...
gemini-3.7-flash-high	Gemini 3.7 Flash (High)
gemini-3.7-flash-medium	Gemini 3.7 Flash (Medium)
gemini-3.7-flash-low	Gemini 3.7 Flash (Low)
gemini-3.6-flash-high	Gemini 3.6 Flash (High)
gemini-3.6-flash-medium	Gemini 3.6 Flash (Medium)
gemini-3.6-flash-low	Gemini 3.6 Flash (Low)
gemini-3.5-flash-high	Gemini 3.5 Flash (High)
gemini-3.5-flash-medium	Gemini 3.5 Flash (Medium)
gemini-3.5-flash-low	Gemini 3.5 Flash (Low)
gemini-3.1-pro-high	Gemini 3.1 Pro (High)
gemini-3.1-pro-low	Gemini 3.1 Pro (Low)
claude-sonnet-4-6	Claude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking	Claude Opus 4.6 (Thinking)
gpt-oss-120b-medium	GPT-OSS 120B (Medium)
```

### Headless Model Runs
```bash
$ ~/.local/bin/agy --model gemini-3.7-flash-high -p "Reply with exactly: AGY_LANE_OK"
AGY_LANE_OK

$ ~/.local/bin/agy --model gemini-3.7-flash-medium -p "Reply with exactly: AGY_MEDIUM_OK"
AGY_MEDIUM_OK

$ ~/.local/bin/agy --model gemini-3.1-pro-high -p "Reply with exactly: AGY_PRO_OK"
AGY_PRO_OK
```

*Note on `--model` formatting:* `agy` requires either a composite model name specifying effort (e.g. `gemini-3.7-flash-high`) or both `--model gemini-3.7-flash` and `--effort <level>`. The exact pinned model `gemini-3.7-flash-high` is passed directly and accepted without extra flags.

---

## 4. Eval Lab Profile & Credential Registration

### Profile Registry (`src/evallab/profiles.py`)
- **Default Profile:** `antigravity-gemini-3.7-flash-high`
  - Adapter: `antigravity-cli`
  - Model: `gemini-3.7-flash-high`
  - Auth Mode: `subscription-cli-session`
  - Secret Source: `cli:agy models`
- Additional profiles: `antigravity-gemini-3.7-flash-medium`, `antigravity-gemini-3.7-flash-low`, `antigravity-gemini-3.1-pro-high`, `antigravity-claude-sonnet-4-6`.

### Credentials Registry (`src/evallab/credentials.py`)
- Credential Constant: `ANTIGRAVITY_SESSION = "antigravity_session"`
- `AGENT_CREDENTIAL_REQUIREMENTS["antigravity-cli"] = ANTIGRAVITY_SESSION`
- `probe_antigravity_session()` and `probe_antigravity_session_result()` run `CliSessionProbe(argv=("agy", "models"), expect="gemini")`.
- `DEFAULT_PROFILE_FOR_ADAPTER["antigravity-cli"] = "antigravity-gemini-3.7-flash-high"`
- `DEFAULT_AGENT_MODELS["antigravity-cli"] = "gemini-3.7-flash-high"`
