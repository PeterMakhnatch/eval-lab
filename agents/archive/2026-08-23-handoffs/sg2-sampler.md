Status: done
Last: merged as PR #110 (`e8eae70`)
Next: none
Blockers: none

# SG-2 Spec-Sampler Handoff

## Implementation Summary

1. **Axes as Data Files (`authoring/templates/*.yaml`)**:
   - **`category.yaml`**: 14 domain templates derived directly from the CRAFT facet vocabulary and scanned 551-task local corpus (74 TB3 tasks + 477 library tasks). Records typical verifiers, languages, container topologies, corpus exemplars, and topic seeds.
   - **`scenario.yaml`**: 10 instruction styles spanning register (terse to conversational to formal) and length (minimal to comprehensive): `minimal`, `incident-emergency`, `bug-report`, `feature-specification`, `refactoring-migration`, `investigation-audit`, `dialogue-transcript`, `structured-pipeline`, `adversarial-obfuscated`, and `documentation-driven`.
   - **`difficulty.yaml`**: 4 difficulty levels (`introductory`, `intermediate`, `advanced`, `expert`) with explicit complexity bounds and concrete anti-pattern lists capturing what makes a task *bad* rather than hard.

2. **Coverage-First Sampler (`sample_spec_batch` in `src/evallab/authoring.py`)**:
   - **Primary — CRAFT Gap Queries**: Queries facet combinations with zero registered coverage (`verifier_type × env_multi_container × pinned_deps`) from `derived/parquet/craft/craft.parquet`. All available craft gaps are emitted first.
   - **Secondary — Random Axis Product**: Samples uniformly from the Cartesian product of `category × scenario × difficulty` using a deterministic PRNG seed to fill remaining slots after gaps are exhausted.
   - **Multi-Phase Novel-Spec Mode**: Generates new `(category, scenario)` pairs from topic seeds and style constraints via `design_novel_spec` using an injectable designer or deterministic offline stub (`default_novel_designer`).

3. **Ledger Deduplication and Lineage**:
   - Every candidate spec is checked against existing entries in `derived/parquet/qualification/ledger.parquet` and quarantined proposals (`library/tasks/_proposed/`). Deduplication is keyed strictly on axis coordinates `(category, scenario, difficulty, target_facets)`.
   - Axis coordinates and provenance are recorded on `Proposal`, serialized in `proposal.json`, and mirrored in `ProposalSpec` / `ProposalAxes` models in `src/evallab/schemas.py`.

4. **Corpus Acceptance Split**:
   - Evaluated against the real scanned local corpus (`derived/parquet/craft/craft.parquet`):
     - Total specs emitted: 20
     - Zero duplicates against qualification ledger; zero duplicates among themselves.
     - Craft-gap query specs: 13 (65.0%)
     - Random axis product specs: 7 (35.0%)
     - Gap ratio: $13/20 = 65.0\% \ge 33.3\%$ ($\ge 1/3$).

5. **Untouched Core Executor**:
   - `src/evallab/queue.py`, `src/evallab/runner.py`, `task_workbench.py`, and `registry.py` are byte-identical to `origin/main`.
