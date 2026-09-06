# Schemas & Contracts Subsystem (src/evallab/schemas/)

## Purpose
Core typed models, immutable DTOs, trial specs, and validation schemas for the
eval-lab platform.

## What lives here / entry points
- `__init__.py`: Primary schema definitions and model exports.
- Top-level contract companions: `execution_contracts.py`, `capability_contract.py`.

## Invariants or rules
1. Backward Compatibility: Modifying schemas must preserve existing field serialization or provide default fallbacks.
2. Strict Type Safety: Pydantic models must enforce field boundaries and reject undefined extra parameters where fail-closed contracts apply.
3. Decoupled Imports: Domain models should not import runtime execution engines or database connection handlers.

## Tests or checks
- Targeted unit tests: `pytest tests/test_contracts.py tests/test_authoring_properties.py`

## What not to add here
Do not import runtime execution engines, heavy CLI tools, or database connection handlers here.
