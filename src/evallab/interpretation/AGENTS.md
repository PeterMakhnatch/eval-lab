# Interpretation

## Purpose
Bounded cited evidence packs, machine judgments, acceptance records, interpretation IR, and feature registry.

## What lives here / entry points
- `evidence_pack.py`: Bounded evidence packaging and token budget enforcement.
- `trajectory_judgment.py`: Evaluator contracts and automated judgments.
- `trajectory_acceptance.py`: Platform governance and acceptance decisions.
- `trajectory_ir.py`: Runtime and citation TrajectoryIR (distinct from root `trajectory_ir.py`).
- `feature_registry.py`: Canonical feature extraction registry.

## Invariants or rules
- Inputs stay bounded and cite immutable evidence.
- A machine judgment is evidence, not automatic acceptance; preserve the explicit acceptance decision.
- Keep interpretation IR distinct from core trajectory IR under the parent contract.
- Do not add model runners or infrastructure drivers here.

## Tests or checks
The assigned verifier uses affected existing IR, evidence-pack, judgment, and acceptance tests under the repository environment.

## What not to add here
No implicit acceptance, hidden uncited context, or incidental merger of the two IRs.
