Status: done
Last: merged as PR #111 (`ca84c7d`)
Next: none
Blockers: none

# SG-3 Inversion Handoff

## Implementation Summary

1. **Answer-First Task Generator (`seed_class=inversion` in `src/evallab/authoring.py`)**:
   - **Data Asset Resolution (`resolve_inversion_asset`, `find_library_data_assets`)**: Discovers and resolves real structured data files within `library/` environments and tasks (`.jsonl`, `.json`, `.csv`, `.sql`, `.txt`, `.sqlite`, `.db`).
   - **Execution Ground Truth (`execute_reference_analysis`)**: The answer key is correct by construction because it is computed by executing reference Python analysis code against real data inside an isolated sandbox. If analysis execution fails, times out, or produces invalid output, the proposal is **refused** (`AuthoringError`) — never filled in with a guessed or model-authored value.
   - **Backward Instruction Generation (`generate_inversion_analysis_code`)**: Probes the structure and types of the data asset to synthesize the reference analysis and construct `instruction.md` backwards from the verified output schema without leaking answers into the prompt.
   - **Reproducibility Verification (`reexecute_inversion_analysis`, `verify_inversion_reproducibility`)**: Re-runs the recorded analysis against the data asset and verifies that the output matches `computed_value` exactly. Any fabricated or drifted answer key is detected and rejected.

2. **Provenance, Lineage, and Pydantic Schemas (`src/evallab/schemas.py`)**:
   - Added `InversionAnalysis` and `InversionSpec` models in `src/evallab/schemas.py`.
   - Records `inversion_analysis` metadata in `inversion.json` and `proposal.json`.
   - Embeds `inputs: [{path, id, digest}]` pointing to the source data asset so `evallab lineage` traces provenance directly to the data file.

3. **Unchanged Battery and Human Gate**:
   - Inversion proposals enter at `proposed` and pass through the unchanged four-check battery (`oracle`, `nop`, `fair-oracle`, `adversarial`) and CRAFT review rubric (`craft_reviewed` score 1.0).
   - Halts at the human registration gate (`RegisterRefusal`); automation cannot register.

4. **Acceptance Verification**:
   - 3 distinct inversion proposals reach the human gate (`craft_reviewed`):
     - `events.jsonl` (JSONL event stream)
     - `data.json` (JSON record collection)
     - `my-query.sql` (SQL/text data asset)
   - Re-executing each stored reference analysis reproduces the recorded answer key exactly (`verify_inversion_reproducibility` passed on all proposals).
   - Failed reference analysis yields explicit refusal (`AuthoringError`), never a guessed key.
   - Lineage resolution verified via `read_artifact_inputs` and `resolve_lineage`.
   - Package structure, oracle solutions, test verifiers, and answer-hiding validated via completeness checker.
