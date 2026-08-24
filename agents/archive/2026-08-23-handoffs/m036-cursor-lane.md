Status: done
Last: merged as PR #132 (`3fe5916`)
Next: none
Blockers: none

# M036 — Cursor agent lane

Status: complete — ready for review
Last: added a `subscription-cli-session` auth mode with a `CliSessionProbe`, four
pinned Cursor profiles (default **`cursor-grok-4.6-high`**, explicitly not `-fast`),
and registered `cursor-cli` as a credential lane. Live probe against the real CLI
returns `ok=True`.
Next: `quota.py` has no cursor accounting, so `evallab preflight` cannot size a
cursor wave yet — it lists claude-code and codex only. That is the next slice and it
is why this PR does not claim preflight support.
Blockers: none for this PR. Running a real trial needs a registered task (M038).

## Why a new auth mode was necessary

`AuthMode` had `none | subscription-auth-file | subscription-keychain`. Cursor fits
none of them, measured:

```
$ ls ~/.cursor/            # cli-config.json, agent-cli-state.json, hooks.json, …
$ python -c "json.loads(...)"  # cli-config.json keys: permissions, version, editor,
                               # display, notifications, hints, modelSlashCommands
                               # agent-cli-state.json keys: version, hasShownAgentCommandTip
$ security find-generic-password -s cursor
security: SecKeychainSearchCopyNext: The specified item could not be found

$ cursor-agent status
✓ Logged in as p.makhnatch@gmail.com
```

The credential lives in an opaque internal store. Both existing modes would have
been a lie: an `AuthFileProbe` on `cli-config.json` reports "available" whenever a
**UI config file** exists, which stays true after the session expires. The only
honest check is to ask the CLI, so the lane gets `subscription-cli-session` with
`secret_source="cli:cursor-agent status"` and a probe that reads **exit status plus a
stdout marker** — never a token. Exit-zero alone is insufficient and is tested: a CLI
can exit 0 while printing "You are not signed in."

## What landed

| Change | Detail |
|---|---|
| `AuthMode` | `+ "subscription-cli-session"`, with a validator requiring a `cli:<command>` secret source |
| `CliSessionProbe` | injectable `runner` seam (no live calls in the suite), timeout, structure-only reads |
| `default_probe_for` | dispatches cli-session profiles to the new probe |
| 4 profiles | `cursor-grok-4.6-high` (default), `cursor-grok-4.5-high`, `cursor-claude-opus-5-thinking-high`, `cursor-gemini-3.7-flash-high` |
| `credentials.py` | `CURSOR_SESSION` constant, `"cursor-cli"` in `AGENT_CREDENTIAL_REQUIREMENTS`, probe, `available_credentials()` |
| `DEFAULT_PROFILE_FOR_ADAPTER` | new explicit map — see the bug below |

Live verification, run in this worktree:

```
cursor profiles: ['cursor-grok-4.6-high', 'cursor-grok-4.5-high',
                  'cursor-claude-opus-5-thinking-high', 'cursor-gemini-3.7-flash-high']
probe type: CliSessionProbe ('cursor-agent', 'status')
LIVE probe: ProbeResult(ok=True, expires_at=None, reason=None)

available: ['codex_auth', 'cursor_session']
requirements: {'claude-code': 'claude_oauth', 'codex': 'codex_auth', 'cursor-cli': 'cursor_session'}
default models: {'codex': 'gpt-5.6-terra', 'claude-code': 'anthropic/claude-fable-5',
                 'cursor-cli': 'cursor-grok-4.6-high'}
```

## A bug I introduced and then fixed — worth reading

`DEFAULT_AGENT_MODELS` was a comprehension over the profile registry keyed by
adapter. With one profile per adapter that was fine; with **four** cursor profiles
sharing `cursor-cli`, last-write-wins picked whichever came last in the tuple — and
it silently chose `gemini-3.7-flash-high` as the cursor default instead of Peter's
stated `cursor-grok-4.6-high`. Replaced with an explicit
`DEFAULT_PROFILE_FOR_ADAPTER` map and pinned by a test that also asserts the default
does not end in `-fast`.

## Mutation evidence

```
MUT 1 — flip the default profile/pin to the -fast variant
FAILED tests/test_profiles.py::test_cursor_default_profile_pins_grok_4_6_high
FAILED tests/test_profiles.py::test_cli_session_probe_reports_ok_on_logged_in_marker

MUT 2 — make the probe trust exit status only (drop the stdout marker check)
FAILED tests/test_profiles.py::test_cli_session_probe_fails_when_marker_absent_despite_exit_zero
  - assert True is False

restored -> 30 passed
```

## Observations, not fixed here

- `evallab doctor` reports `FAIL catalog-parquet catalog=80 projected=6 missing=74`.
  That is a projection gap, not something this lane touched — it appeared after the
  free battery runs landed 8 new job dirs. Flagging for INGEST (M029), whose entire
  purpose is completeness as an invariant.
- `gemini-cli` remains dead for this account (`IneligibleTierError`), which is why the
  Gemini route here goes **through Cursor** rather than through `gemini-cli`.

## Policy edit Peter must apply

`policy/` is human-only in this repo, so this PR does not touch it. Cursor cannot
dispatch as a standing-approved agent until this lands in
`policy/standing-approvals.yaml` (currently line 35: `agents: [oracle, nop]`):

```yaml
-    agents: [oracle, nop]
+    agents: [oracle, nop, cursor-cli]
```

Check the surrounding rule's ceiling fields while you are there — `daily_cost_ceiling_usd: 20`
and `per_job_cost_ceiling_usd: 3` are at lines 29-30, and a subscription lane with no
metered per-call cost may want its own rule rather than inheriting the codex ceilings.
Without this edit, cursor runs still work but require per-spec GATE authorisation.
