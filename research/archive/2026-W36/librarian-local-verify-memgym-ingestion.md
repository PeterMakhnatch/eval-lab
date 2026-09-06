# Librarian — locally verify MemGym for source-only ingestion

## Context

The shared `ContextOperationFact.step_index`/payload digest contract and generic memory-continuity producer are now integrated on canonical head `9768ad60`. The remaining `Prepare MemGym ingestion` task was blocked on that integration. Prior research calls MemGym Apache-2.0 at a commit beginning `50b404e6`, but marks it **agent-verified, not local**. Correct arXiv binding is reported as `2605.20833`; do not reuse the fabricated old ID `2512.09876`.

Read-only research. Do not edit Eval Lab, implement an adapter, register/promote/activate MemGym, run benchmark/model/control tasks, or spawn subagents.

## Required settling checks

1. Resolve the official MemGym paper/project/repository from primary sources. Record exact URLs and distinguish author-owned sources from mirrors.
2. Verify the full exact commit corresponding to prefix `50b404e6`; if no such reachable commit exists, report the mismatch rather than choosing another pin.
3. Clone/read locally at that exact detached commit in `/tmp` or another non-repository cache. Record:
   - exact commit/tree identity;
   - complete `LICENSE` digest and exact license text/classification;
   - dependency/lock/environment files and digests;
   - source/data/task/config files that define long-horizon memory behavior;
   - evaluator/verifier implementation and whether evaluation is deterministic, model-judged, or mixed.
4. Map the real source schema field-by-field:
   - native episode/step ID and total ordering;
   - condensation/compaction event representation;
   - summary, ordered forgotten-message indices, compression metadata;
   - memory write/read/use operations, tool-call IDs, payload identity;
   - task outcome and verifier evidence;
   - any missing or ambiguous field that must remain typed unavailable.
5. Trace source-native code paths/functions from task generation through execution output to scoring. Identify exact released files and callable functions, not prose claims.
6. Determine whether a no-model deterministic local fixture can exercise the ingestion adapter without pretending to validate benchmark performance. Separate:
   - source/package adoption;
   - C0 ingestion/feature extraction;
   - benchmark certification/registration;
   - measurement activation.
7. Define the smallest clean Eval Lab adapter scope reusing integrated `ContextOperationPayloadV1`, `ContextOperationFact.step_index`, and memory producer. No second fact schema or inferred order.
8. Identify concrete blockers: missing stable data, network dependency, generated-only fixtures, judge model, license/data split, unavailable native IDs/order, non-hermetic dependencies, or unverifiable outputs.

## Output

Write `/tmp/librarian-memgym-local-source-verification.md` with:

- primary source URLs and exact commit/tree;
- local path:line evidence from cloned source;
- license/dependency/data digests;
- source-to-canonical field matrix;
- deterministic fixture feasibility;
- exact proposed adapter/test/benchmark-card file scope;
- explicit GO/HOLD for **source-only C0 ingestion** and separately for certification/measurement.

Page `wH:p9` with the two verdicts and report path. Confirm no Eval Lab edit, no model/control/benchmark run, and no subagents.
