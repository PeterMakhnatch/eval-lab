# Interpretation Subsystem (src/evallab/interpretation/)

## Responsibilities
Owns the agent trajectory intermediate representation (`TrajectoryIR`), bounded
evidence packing (`EvidencePack`), model and deterministic evaluators (`MachineJudgment`),
data quality screening (`TrajectoryQualityReport`), and platform governance gates
(`AcceptanceDecision`).

## Core Invariants
1. Single Authority for Trajectory Semantics: All trajectory normalization, sequence
   alignment, and construct extraction live in `evallab.interpretation.*`.
2. Bounded Model Inputs: `EvidencePack` enforces strict token budgets, verifiable
   citation spans, and explicit omission ranges before model evaluation.
3. Separation of Judgment and Acceptance: Evaluator outputs (`MachineJudgment`) are
   distinct from Platform governance policy outcomes (`AcceptanceDecision`).
4. Stable Subpackage Locations: No module renames or file moves out of this subpackage.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_trajectory_ir.py tests/test_evidence_pack.py tests/test_trajectory_acceptance.py tests/test_trajectory_judgment.py`
