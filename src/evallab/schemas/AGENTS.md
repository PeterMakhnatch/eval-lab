# Shared schemas

## Purpose
Dependency-light shared Pydantic contracts, currently in `__init__.py`.

## What lives here / entry points
- `__init__.py`: Primary schema definitions and model exports.
- Top-level contract companions: `execution_contracts.py`, `capability_contract.py`.

## Invariants or rules
- Validate strictly and fail closed on invalid contracts.
- Preserve actual persisted/wire contracts unless the change includes explicit versioning/migration. Do not add speculative fallback aliases or unused compatibility paths.
- No runtime DB connections, model execution, or side-effectful application imports.

## Tests or checks
The assigned verifier uses affected existing contracts and authoring-properties tests.

## What not to add here
No runtime service layer in the schema package.
