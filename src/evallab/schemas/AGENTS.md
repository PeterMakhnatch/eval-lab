# Schemas & Contracts Subsystem (src/evallab/schemas/)

## Responsibilities
Defines core typed models, immutable DTOs, trial specs, and validation schemas across Eval Lab.

## Core Invariants
1. Backward Compatibility: Modifying schemas must preserve existing field serialization or provide default fallbacks.
2. Strict Type Safety: Pydantic models must enforce field boundaries and reject undefined extra parameters where fail-closed contracts apply.
3. Decoupled Imports: Domain models should not import runtime execution engines or database connection handlers.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_contracts.py tests/test_authoring_properties.py`
