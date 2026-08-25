# AgentAbstain paired-task analysis adapter

This adapter preserves an exact 16-pair (32-variant) slice of official
AgentAbstain rows, deterministic primary verification, ATIF conversion, and
separate response judgment evidence.

## Harbor status: blocked pending MCP wiring

The selected HF environment modules and schemas were retrieved at the pinned
CC-BY-4.0 revision and all 16 import successfully after installing FastMCP with
the official `abstention_factory` runtime. A direct transition probe reaches
the environments, but their tools are registered on FastMCP and are not Python
attributes; an MCP server/Harbor wiring layer is still required to execute
real tool calls, persist state changes, and project real ATIF. A metadata-only
Harbor package would be invalid, so no runnable Harbor claim is made.

Blocker evidence and per-file digests are in `raw/BLOCKER.json` and
`raw/HF_ENVIRONMENT_MANIFEST.json`. Raw bytes/reproduction evidence are under
`raw/`; normalized rows are under `data/`.
