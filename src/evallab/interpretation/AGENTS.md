# Interpretation

## Purpose
Bounded cited monitor evidence, machine judgments, interpretation IR, trial posture, and feature extraction.

## What lives here / entry points
- `monitor_evidence.py`: Bounded evidence packaging and citation validation.
- `monitor_contracts.py`, `monitor_agent.py`: Evaluator contracts and automated judgments.
- `trial_posture.py`: Evidence-based trial disposition.
- `features.py`: Canonical feature extraction registry.

## Invariants or rules
- Inputs stay bounded and cite immutable evidence.
- A machine judgment is evidence, not automatic acceptance; preserve the explicit acceptance decision.
- Do not add model runners or infrastructure drivers here.

