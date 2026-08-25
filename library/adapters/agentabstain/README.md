# AgentAbstain paired-task analysis adapter

This adapter preserves an exact 16-pair (32-variant) slice of official
AgentAbstain rows, deterministic primary verification, ATIF conversion, and
separate response judgment evidence.

## Harbor status: materialized preview_002 pair (14 pairs pending)

The selected pair `ambiguous_action_specification/preview_002` has been
materialized as two fully-compliant Harbor task packages:
- `library/tasks/agentabstain-ambiguous-action-preview-002-act`
- `library/tasks/agentabstain-ambiguous-action-preview-002-abstain`

The FastMCP runtime wrapper (`runtime.py`) exposes official tools across
`spotify` and `gmail_and_email_records` with deterministic state persistence
and calls logging. Primary verifiers evaluate irreversible tool commits and
state diffs deterministically.

Blocker evidence and per-file digests are in `raw/BLOCKER.json` and
`raw/HF_ENVIRONMENT_MANIFEST.json`. Raw bytes/reproduction evidence are under
`raw/`; normalized rows are under `data/`.
