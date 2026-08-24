Status: done
Last: merged as PR #105 (`d95099f`)
Next: none
Blockers: none

## Summary of Changes

1. **`ExperimentSpec` Contract Expansion (`src/evallab/schemas.py`)**:
   - `question_ref: str | None = None`: free-form reference linking a spec to the research question it answers (e.g. from `PROGRAM.json` or study brief).
   - `elicitation: ElicitationSpec | None = None`: elicitation tuple (`preamble_hash`, `toolset`, `env_overrides`) supporting the §4 one-variable difference check via `diff_fields()`.
   - `prereg: PreregSpec | None = None`: preregistration block (`expected`, `decision_rule`) required for `purpose=comparison` specs, stored verbatim and quoted by eval cards.
   - `power: PowerSpec | None = None`: statistical power and sample size planning (`mdd`, `planned_n`).
   - Every new field is optional with a safe default (`None`), preserving backwards compatibility with all 92 existing trials, 5 promoted evidence bundles, and committed queue specs.

2. **`TaskRegistryRecord` Contract Expansion (`src/evallab/schemas.py`)**:
   - `contamination: TaskContamination | None = None`: contamination assessment record (`public_since?: date`, `in_pretrain: Literal['y', 'n', 'unknown'] = 'unknown'`, `basis: str = ''`).
   - `human_minutes: int | None = None`: expert completion time estimate in minutes (optional integer $\ge 0$).
   - `TaskRegistryRecord` remains located in `src/evallab/schemas.py` and imported by `src/evallab/registry.py`.

3. **Golden Schema Freeze (`tests/fixtures/contracts/`)**:
   - Added `ExperimentSpec.json` and `TaskRegistryRecord.json` to the byte-for-byte golden schema freeze.
   - Regenerated and verified that all existing schemas (`Suite`, `AnalysisRecord`, `ObservationRecord`, `CalibrationRecord`, `Verdict`) remain identical byte-for-byte.
   - Schema additions are purely additive (`anyOf` with `null`, default `null`/`unknown`).

4. **Contract Tests (`tests/test_contracts.py`)**:
   - `test_golden_schemas_match_live`: asserts byte-for-byte identity of live schemas against committed golden fixtures for all 7 contract models.
   - `test_golden_freeze_detects_injected_field`: verifies that injecting an unapproved field fails schema matching with an assertion error.
   - `test_roundtrip_all_models`: validates `model_dump()` -> `model_validate()` round-trip identity across all models including `ExperimentSpec`, `TaskRegistryRecord`, `ElicitationSpec`, `PreregSpec`, `PowerSpec`, and `TaskContamination`.
   - `test_experiment_spec_new_fields_roundtrip_and_prereg_verbatim`: verifies all new fields round-trip faithfully and specifically asserts byte-identical survival of `prereg.expected` and `prereg.decision_rule` text (newlines, tabs, spacing).
   - `test_spec_pre_dating_new_fields_loads_with_defaults`: verifies legacy specs from disk load without errors, with all new fields defaulting to `None`.
   - `test_task_registry_record_pre_dating_new_fields_loads_with_defaults`: verifies legacy registry records load with `contamination=None` and `human_minutes=None`.
   - `test_in_pretrain_valid_literals` and `test_in_pretrain_rejection`: validates closed set `{'y', 'n', 'unknown'}` and verifies rejection of invalid strings.
   - `test_elicitation_one_variable_difference_expressible`: validates that 1-variable differences (`preamble_hash`, `toolset`, or `env_overrides`) are strictly distinguishable from 0-, 2-, and 3-variable differences via `diff_fields()`.

5. **Documentation (`docs/contracts.md`, `docs/INDEX.md`, `docs/repo-map.md`)**:
   - Documented all new entity fields, invariant rules, and updated the golden schema regeneration snippet.
   - Regenerated documentation index and repository map.

## Verification

- `uv run pytest tests/test_contracts.py`: 32 passed
- `uv run pytest`: 1143 passed, 2 skipped, 1 xfailed
- `uv run ruff check .`: clean (all checks passed)
- `uvx ty@0.0.71 check src/`: 28 diagnostics ($\le 28$)
- `uv run python -m evallab.docindex check`: clean (passed)
- `DirectoryQueue` test loading real specs from `queue/proposed`, `queue/waiting`, `queue/done`: all loaded successfully
