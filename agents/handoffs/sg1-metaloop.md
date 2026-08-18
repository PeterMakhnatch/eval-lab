Status: PR open and verified against full test suite, type checking, repomap, and docindex.
Last: Implemented SG-1 Meta-Loop: task synthesis as a Harbor task through the existing executor. Built `library/meta/synthesize-task@1/` (Terminal-Bench format with instruction, skeleton, exemplar, templates, and 4-check completeness verifier), extended `src/evallab/authoring.py` with `propose --via-harbor` (submit-only with `purpose=craft`) and `harvest` (with strict verifier check and provenance lineage recording), and added test suite in `tests/test_authoring.py`.
Next: Review PR #SG-1; proceed to SG-2 spec sampler feeds.
Blockers: None. Zero external model calls or token spend incurred. `queue.py` and `runner.py` remain untouched.

# SG-1 Meta-Loop Handoff

## Implementation Summary

1. **Meta-Task Template Package (`library/meta/synthesize-task@1/`)**:
   - Terminal-Bench package directing an agent to author ONE task package inside `/app/output/task/`.
   - Environment includes structured skeleton directories, registered exemplar (`event-summary`), authoring guidelines and templates.
   - `tests/` contains automated completeness checker testing package structure, reference oracle solution execution, verifier tests passing on oracle and failing on empty work, and strictly testing for no answer leakage into the environment image or instructions.

2. **`propose --via-harbor` in `src/evallab/authoring.py`**:
   - Assembles meta-task with sampled/provided spec and exemplar.
   - Submits experiment spec with `purpose="craft"` to the directory queue.
   - Strictly submit-only: does not start runner or dispatch executions. Policy gate enforces standing approvals.

3. **`harvest` in `src/evallab/authoring.py`**:
   - Moves generated package from job artifacts into quarantine `library/tasks/_proposed/<proposal_id>/`.
   - Strictly verifies that the completeness checker passed in the job before admitting the proposal.
   - Records lineage inputs (`inputs: [{path, id, digest}]`, `job_id`, `injected_spec`, `exemplar`) in `proposal.json` for `evallab lineage` resolution.
   - Harvested proposal enters at `proposed` and earns its way through the unchanged 4-control battery (`author battery`).

4. **Zero Executor Modifications**:
   - `src/evallab/queue.py`, `src/evallab/runner.py`, `task_workbench.py`, and `registry.py` are byte-unchanged.
