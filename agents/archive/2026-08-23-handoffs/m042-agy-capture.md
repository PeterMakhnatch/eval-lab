Status: done
Last: merged as PR #141 (`c4eac8e`)
Next: none
Blockers: none

# M042 AGY capture

## Status

Implemented and verified. No paid Harbor trial was run.

## Evidence gathered

- Installed `agy` is version `1.1.15`.
- `agy --help` exposes print-mode `--output-format stream-json`; its help says the transport emits NDJSON.
- A live no-model probe, `agy --prompt=/help --output-format stream-json`, emitted machine-readable `command_result` and terminal `result` records without invoking a model.
- Official headless documentation for the installed CLI documents `init`, `step_update`, and `result` events, including `user_input`, `agent_response`, `tool`, and `checkpoint` step types plus `tool_info` output/error payloads.
- `agy agentapi --help` exposes only `get-conversation-metadata`, `new-conversation`, and `send-message`; it does not expose a server/ACP process-event stream. The integration therefore uses the proven `stream-json` headless transport, not the client-only `agentapi` subcommands.
- Harbor's installed `antigravity_cli.py` invokes text print mode and copies a native session candidate. It was not modified.

## Changes

- `src/evallab/antigravity.py` parses structured NDJSON into ATIF-v1.6, aggregates ACTIVE text deltas, preserves ordered user/agent/tool/error observations, records raw-source and job/trial/model/agent identity in `extra`, and redacts credential-shaped fields.
- `src/evallab/harbor_antigravity.py` provides the repo-owned `AntigravityCliCapture` Harbor import path. It keeps inherited OAuth staging/scrubbing, invokes `agy --output-format stream-json`, sanitizes the stream before persistence, writes `agent/trajectory.json`, and exposes an explicit print-mode final-response-only fallback.
- `src/evallab/runner.py` routes the `antigravity-cli` lane to `evallab.harbor_antigravity:AntigravityCliCapture`, adds only the repo `src/` path to Harbor's subprocess `PYTHONPATH`, and preserves Harbor model IDs `google/gemini-3.7-flash-{low,medium,high}`.
- Tests cover structured event conversion, valid ATIF ingestion with nonzero steps/tool calls/trajectory facts, print-mode unavailability, credential redaction, exact model routing, and Harbor custom-agent loading.

## Verification

- `uv run pytest tests/test_antigravity.py tests/test_runner.py` — 40 passed.
- Targeted Ruff check — passed.
- Harbor's installed Python loaded `evallab.harbor_antigravity:AntigravityCliCapture` and reported the inherited agent name `antigravity-cli`.
- Generated ATIF validated with Harbor's installed `Trajectory` model.
- Real `agy --prompt=/help --output-format stream-json` output parsed to a stream-json ATIF record.

## Boundary

The parent must perform the one live Harbor smoke after review. This branch does not claim a live model trajectory until that smoke produces `agent/trajectory.json` and nonzero projected facts.
