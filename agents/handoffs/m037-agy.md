# M037 — Antigravity (AGY) agent lane

Status: complete — ready for review
Last: wired the Antigravity agent lane (`antigravity-cli`) with `subscription-cli-session` auth mode, five pinned profiles (default **`antigravity-gemini-3.7-flash-high`**), and registered `antigravity-cli` in `evallab.credentials`. Live probe returns `ProbeResult(ok=True)`.
Next: quota accounting and standing approval policy update.
Blockers: none for this PR. Running a real trial needs a registered task and Peter's standing approval or gate authorization.

## Harbor Adapter Findings & Evidence

Harbor 0.21.0 provides two Antigravity agent adapters:
1. `antigravity-cli` (`harbor.agents.installed.antigravity_cli.AntigravityCli`):
   - Installs `agy` via `curl -fsSL https://antigravity.google/cli/install.sh | bash`
   - Executes `$HOME/.local/bin/agy --dangerously-skip-permissions --model <model> --prompt=<instruction>`
   - Does NOT drive `agentapi` or ACP directly. (`~/.gemini/antigravity-cli/bin/agentapi` is simply a wrapper script for `agy agentapi "$@"`).
   - In containerized environments, supports headless OAuth token injection via `AGY_AUTH_JSON_PATH` or `AGY_FORCE_AUTH_JSON=1` (seeded into `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`).
2. `antigravity-sdk` (`harbor.agents.installed.antigravity_sdk.AntigravitySDK`):
   - Requires `GEMINI_API_KEY` (an API key, forbidden by lab subscription-only policy).

Therefore, `antigravity-cli` is the only supported subscription agent transport for Antigravity in this lab.

## Verified Ground Truth (Observed 2026-08-19)

- `~/.local/bin/agy --version` -> `1.1.15`
- `~/.local/bin/agy -p "Reply with exactly: AGY_LANE_OK"` -> `AGY_LANE_OK` (exit 0)
- `~/.local/bin/agy models` -> listed 14 available models including `gemini-3.7-flash-high`, `gemini-3.7-flash-medium`, `gemini-3.7-flash-low`, `gemini-3.1-pro-high`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`
- `~/.local/bin/agy --model gemini-3.7-flash-high -p "..."` -> executed headlessly with exit 0.

## What landed

| File | Change |
|---|---|
| `src/evallab/profiles.py` | Registered 5 Antigravity profiles: `antigravity-gemini-3.7-flash-high` (default), `antigravity-gemini-3.7-flash-medium`, `antigravity-gemini-3.7-flash-low`, `antigravity-gemini-3.1-pro-high`, `antigravity-claude-sonnet-4-6`. Updated `default_probe_for` to handle `expect="gemini"` for `agy`. |
| `src/evallab/credentials.py` | Added `ANTIGRAVITY_SESSION = "antigravity_session"`, added `"antigravity-cli"` to `AGENT_CREDENTIAL_REQUIREMENTS`, implemented `probe_antigravity_session()` and `probe_antigravity_session_result()`, wired into `available_credentials()`, and added `"antigravity-cli": "antigravity-gemini-3.7-flash-high"` to `DEFAULT_PROFILE_FOR_ADAPTER`. |
| `tests/test_profiles.py` | Added unit tests for default profile pin, model pinning, `CliSessionProbe` on "gemini" marker, failure on non-zero exit, failure when marker absent on exit zero, and `default_probe_for` resolution. |
| `docs/research/antigravity-lane.md` | Research documentation on adapter mechanics, authentication, verified commands, and profile configuration. |

### Live Verification Output

```python
Probe: ProbeResult(ok=True, expires_at=None, reason=None)
Available: frozenset({'cursor_session', 'antigravity_session', 'codex_auth'})
Requirements: {'claude-code': 'claude_oauth', 'codex': 'codex_auth', 'cursor-cli': 'cursor_session', 'antigravity-cli': 'antigravity_session'}
Default models: {'codex': 'gpt-5.6-terra', 'claude-code': 'anthropic/claude-fable-5', 'cursor-cli': 'cursor-grok-4.6-high', 'antigravity-cli': 'gemini-3.7-flash-high'}
```

## Mutation Evidence

```
MUTATION 1: CliSessionProbe trusts exit status only (ignores stdout marker)
FAILED tests/test_profiles.py::test_cli_session_probe_fails_when_marker_absent_despite_exit_zero
FAILED tests/test_profiles.py::test_antigravity_cli_session_probe_fails_when_marker_absent_despite_exit_zero
  - assert True is False

MUTATION 2: Model pin mismatch (change default pin to gemini-3.7-flash-medium)
FAILED tests/test_profiles.py::test_antigravity_default_profile_pins_gemini_3_7_flash_high
  - AssertionError: assert 'gemini-3.7-flash-medium' == 'gemini-3.7-flash-high'

Restored: 36 passed in 0.13s
```

## Policy edit Peter must apply

`policy/` is human-only in this repo, so this PR does not touch it. To allow `antigravity-cli` in standing approvals:

```yaml
-    agents: [oracle, nop]
+    agents: [oracle, nop, antigravity-cli]
```
