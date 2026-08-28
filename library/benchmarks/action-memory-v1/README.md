# Action Memory Benchmark Family (v1)

## Overview
`action-memory-v1` is a deterministic, source-only benchmark family measuring **actionable memory and context binding** under controlled context doses and distractor conditions. Unlike passive needle-in-a-haystack or static recall benchmarks, `action-memory-v1` requires the agent to read and track dynamic entity state through explicit streamable HTTP MCP operations and execute a mutating action whose arguments bind the strictly latest entity state after state inversions.

## Core Construct
- **Construct**: Actionable Memory & Entity Value Binding under Inversion and Context Growth.
- **Key Characteristics**:
  - Exposes deterministic streamable-HTTP MCP state/context operations (`read_context_chunk`, `get_entity_state`, `mutate_action`).
  - Strict byte-accounted context doses with matched arms (clean, neutral padding, semantic distractors).
  - State inversion: Entities are defined with initial state $K \to V_1$, updated in later chunks to $K \to V_2$. The agent's final mutation action must bind $V_2$.
  - Exact deterministic verifier scoring (oracle = 1.0, NOP = 0.0, stale mutant = 0.0, wrong-target mutant = 0.0, recall-only mutant = 0.0).

## Factor Space & Cells
1. **Dose Ladder**: `0k` (clean), `4k`, `16k`, `64k` bytes of context.
2. **Intervention Arm**:
   - `clean`: minimal direct facts and state inversion.
   - `neutral_padding`: synthetic byte-accounted filler chunks (position prefix/middle/suffix metadata recorded).
   - `semantic_distractor`: distractors sharing schema/keys but distinct entity identifiers to test precision.
3. **State Inversion Depth**: Single inversion ($K \to V_1 \to V_2$) and multi-inversion ($K \to V_1 \to V_2 \to V_3$).

## Provenance and Convergent Validity
- **Provenance Note**: Conceptually informed by LOCA-Lean / LOCA-bench principles of active context manipulation and memory-grounded agent actions.
- **Zero External Runtime Dependency**: Fully self-contained, deterministic synthetic generator with zero network/LLM dependencies.

## Evidence & Output Contract
The agent container produces:
- `/app/evidence/benchmark-events.jsonl`: Monotone index of read/update/mutate events.
- `/app/evidence/final-state.json`: Verified final execution record containing target entity, bound argument value, and mutation digest.
