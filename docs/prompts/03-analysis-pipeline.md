# Build the provenance-bearing trial analysis pipeline

## Mission

Wrap Harbor's existing `analyze` capability so the lab can request bounded,
structured analysis of completed trials while preserving source evidence,
analysis provenance, cost controls, and human-review status.

This component interprets evidence; it does not replace deterministic
extraction or comparison. Work on a named branch/worktree after the ATIF and
cohort components are complete.

## Required behavior

1. Inspect the installed Harbor `analyze` implementation and use it rather than
   recreating trial-upload or analysis-task orchestration.
2. Add a versioned rubric and structured output contract based on
   `docs/analysis-loop.md`. At minimum capture validity, primary failure
   category, summary, evidence references, alternative explanations, proposed
   discriminator, and calibrated confidence label.
3. Require every substantive claim to cite a source relative path and, for ATIF
   evidence, a step ID or tool-call ID when available.
4. Record source file/task digests plus analysis agent, adapter version, model,
   prompt/rubric/output-schema digests, timestamps, token use, and cost.
5. Write each invocation to a new immutable sidecar under
   `derived/analyses/<analysis-id>/`. Re-running with a new model or rubric
   creates a new ID; it never overwrites the prior result or source trial.
6. Add explicit human review records with `accepted`, `needs_revision`,
   `rejected`, and `superseded` dispositions plus rationale. Review must not edit
   the model's original output.
7. Add CLI dry-run/planning output that shows source trials, analysis model,
   estimated number of model calls, and destination before invocation.
8. Preserve the existing billable-run boundary. Actually invoking an analysis
   model requires an explicit flag or approval; fixture tests and default
   validation make no paid calls.
9. Index analysis metadata and review state in PostgreSQL only after writing the
   durable sidecar. Re-ingestion must reconstruct it.
10. Detect missing, invalid, or hallucinated evidence references and mark the
    analysis invalid rather than silently accepting it.

## Initial rubric questions

- Is this a valid agent attempt, task defect, environment failure, harness
  failure, or verifier defect?
- What is the earliest evidence-supported point where the attempt diverged from
  success?
- Which documented failure category best fits?
- What evidence supports that category?
- What competing explanation remains plausible?
- What one-variable experiment would distinguish them?

## Tests

All automated tests use saved synthetic analysis outputs or a fake analyzer:

- valid structured output with resolvable evidence references;
- missing file, step, and tool-call references;
- unknown failure category;
- analysis of a harness exception not mislabeled as model incapability;
- repeat invocation creates a new analysis record;
- human review leaves original analysis bytes unchanged;
- provenance and digests are complete;
- command refuses a model call without explicit billable acknowledgement;
- no source job file changes.

## Acceptance commands

```bash
uv run pytest
uv run ruff check .
uv run harbor-lab analyze plan <fixture-trial>
```

Do not make a live analysis call unless Peter separately approves the agent,
model, source trials, expected call count, and cost.

## Handoff

Document the rubric, schema, review workflow, dry-run command, and how to audit a
finding back to raw evidence. Report limitations of model-assisted analysis
plainly; model agreement is not validation.

## Implemented contract (ANALYST, 2026-08-14)

- `harbor-lab analyze plan <trial>` prints source IDs, prompt/rubric/schema
  digests, destination, one expected call, two-call maximum, and the required
  `researcher-followups` queue policy without invoking a model.
- Saved output can be validated with `analyze stub`; each invocation writes a
  fresh immutable `derived/analyses/<uuid>/analysis.json`. Reviews append under
  that invocation's `reviews/` directory and leave original bytes unchanged.
- Pydantic contracts cover the stage-5 taxonomy, validity, earliest failure,
  path/step/tool evidence, alternatives, discriminator, calibrated confidence,
  source digests, exact prompt/rubric/schema digests, token/cost provenance, and
  review disposition. Missing/hallucinated citations mark the sidecar invalid.
- Installed Harbor 0.21.0's analyzer uploads a copy for evaluation but writes
  `analysis.json` into the source trial afterward. That contradicts the lab's
  immutable-evidence rule, so the lab preserves Harbor's bounded/read-only
  analysis shape while using a separate sidecar and headless Codex adapter.
- The live adapter is callable only with a matching file already in
  `queue/running`, policy `researcher-followups`, the same source trial ID, and
  `max_model_calls=2`; it rechecks authorization per call. There is no direct
  live-analysis CLI. All acceptance tests and the catalog example use a saved
  stub response and cost $0.
