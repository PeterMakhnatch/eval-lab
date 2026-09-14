# Interpretation Subsystem (src/evallab/interpretation/)

## Purpose
Bounded evidence packing (`EvidencePack`), evaluators (`MachineJudgment`),
data quality screening, and platform governance gates (`AcceptanceDecision`).

## What lives here / entry points
- `evidence_pack.py`: Bounded evidence packaging and token budget enforcement.
- `trajectory_judgment.py`: Evaluator contracts and automated judgments.
- `trajectory_acceptance.py`: Platform governance and acceptance decisions.
- `trajectory_ir.py`: Runtime and citation TrajectoryIR (distinct from root `trajectory_ir.py`).
- `feature_registry.py`: Canonical feature extraction registry.

## Invariants or rules
1. Bounded Model Inputs: `EvidencePack` enforces strict token budgets and citation spans.
2. Separation of Judgment and Acceptance: Evaluator outputs (`MachineJudgment`) are
   strictly distinct from platform governance outcomes (`AcceptanceDecision`).
3. Stable Locations: Do not merge or delete `trajectory_ir.py` across packages.

## Tests or checks
- Targeted unit tests: `pytest tests/test_trajectory_ir.py tests/test_evidence_pack.py tests/test_trajectory_acceptance_contract.py tests/test_trajectory_judgment_contract.py`

## What not to add here
Do not place raw physical storage drivers or direct execution queue runners here.
