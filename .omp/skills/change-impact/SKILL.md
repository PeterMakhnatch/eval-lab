---
name: change-impact
description: Analyze what a code, schema, CLI, SQL, task, verifier, or serialized-data change could break outside its diff, and prove the safety-critical invariant. Use before exported-symbol changes, refactors, migrations, or reviews of deceptively small changes.
---

# Change impact

Find dependents that a text search or the visible diff can miss. Finish with executable evidence, not a plausible impact narrative.

## Establish the changed contract

1. Read the complete diff or target construct.
2. State what behavior, type, data shape, persistence format, or operator contract changes.
3. Identify the one or two invariants on which the change's safety depends.

## Follow the impact paths

- Use LSP references before changing an exported Python symbol. Follow definitions, implementations, re-exports, and callers.
- Search non-symbol contracts explicitly: CLI registration, Pydantic models, JSON fields, Harbor job layout, SQL schema and views, configuration keys, task/verifier boundaries, docs, and external tool consumers.
- Check the pinned dependency or upstream source when safety depends on library behavior.
- For surprising guards or apparently redundant code, inspect `git blame`, file history, the introducing commit, and linked pull request. Label inferred intent as inference.
- Treat an empty search as evidence only for the searched surface; do not generalize it to dynamic or external consumers.

## Prove the safety invariant

Use the cheapest direct level that answers the risk:

1. Cite the exact implementation or contract.
2. Trace why the bad path cannot reach the changed code.
3. Run a focused test or script against the real implementation.
4. Exercise the actual CLI, TUI, service, or data path.

For material risk, reach level 3 or 4. If that is unavailable, mark the invariant unproven rather than rounding up confidence. Do not substitute compilation, agent self-report, or an unrelated broad suite.

## Report

Return:

- changed contract;
- affected callers and non-code consumers;
- safety-critical invariant and proof level;
- confirmed risks with concrete failure paths;
- cleared risks and the evidence that cleared them;
- cheapest pre-merge reproduction that would fail if the conclusion is wrong.

Keep the investigation scoped to the requested change. Do not add compatibility layers in place of migrating known callers.

## Provenance

Adapted for OMP and eval-lab from Lauren Tan's MIT-licensed Pstack `blast-radius` skill: `https://github.com/cursor/plugins/blob/main/pstack/skills/blast-radius/SKILL.md`.
