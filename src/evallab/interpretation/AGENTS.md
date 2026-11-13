# Interpretation

## Purpose
Bounded cited monitor evidence, machine judgments, interpretation IR, trial posture, and feature extraction.

## What lives here / entry points
- `trial_posture.py`: Evidence-based trial disposition.
- `features.py`: Canonical feature extraction registry.

## Invariants or rules
- Inputs stay bounded and cite immutable evidence.
- A machine judgment is evidence, not automatic acceptance; preserve the explicit acceptance decision.
- Do not add model runners or infrastructure drivers here.

