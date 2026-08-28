# Interpretation Subsystem (src/evallab/interpretation/)

## Responsibilities
Owns bounded evidence packing (`EvidencePack`), model and deterministic
evaluators (`MachineJudgment`), data quality screening
(`TrajectoryQualityReport`), platform governance gates (`AcceptanceDecision`),
and the **runtime/citation** TrajectoryIR in this package.

A second, lossless ATIF TrajectoryIR remains at
`src/evallab/trajectory_ir.py`. That is not a shim. Do not merge, re-export,
or delete either file without a Peter-approved authority gate.

Feature registry canonical path: `interpretation/feature_registry.py`.
Top-level `src/evallab/feature_registry.py` is a re-export shim.

## Core Invariants
1. Bounded Model Inputs: `EvidencePack` enforces strict token budgets, verifiable
   citation spans, and explicit omission ranges before model evaluation.
2. Separation of Judgment and Acceptance: Evaluator outputs (`MachineJudgment`) are
   distinct from Platform governance policy outcomes (`AcceptanceDecision`).
3. Stable Subpackage Locations: No module renames or file moves out of this subpackage.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_trajectory_ir.py tests/test_evidence_pack.py tests/test_trajectory_acceptance.py tests/test_trajectory_judgment.py`
