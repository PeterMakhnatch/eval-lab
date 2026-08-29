---
type: experiment-scaffold
topic: action-memory-sequential-retrieval-v1
author: Main
date: 2026-08-29
status: observed
collection: trajectory-analysis
---

# Action Memory sequential retrieval scaffold v1

Appended verbatim to both seed-1337 Action Memory 64k tasks for the default-timeout and timeout-multiplier-3 scaffold jobs.

## Retrieval execution protocol

After `list_context_chunks`, preserve the returned handle order exactly. Call `get_context_chunk` strictly one handle at a time, in that order. Do not batch or parallelize retrieval calls. Maintain an explicit read index and, before executing the mutation, verify that every listed handle was read exactly once with no reordering or duplication. This protocol changes only execution discipline; derive the bound value from the retrieved records as usual.

## Observed boundary

The default-timeout run produced two non-scored `AgentTimeoutError` outcomes. With `agent_timeout_multiplier=3`, the neutral task completed 257/257 reads and passed; the semantic task stopped after 232/257 reads and failed. The two scored scaffold trials consumed 14,137,819 prompt tokens total. This is an observed execution-cost intervention, not evidence that sequential retrieval generally improves Action Memory.
