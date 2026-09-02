---
status: living
audience:
  - builder
  - reviewer
---

# Domain-first operating model

Use the domain model to exclude invalid programs before adding validation, branches, or tests.

## Decision order

1. Name the domain facts and state transitions.
2. Parse external data once at the boundary.
3. Represent each valid outcome with a type that contains exactly the data that outcome needs.
4. Make the core operate only on parsed domain values.
5. Test observable behavior and domain invariants.
6. Delete compatibility code, test seams, and defensive checks made unreachable by the model.

## Parse, do not validate

A boundary parser returns a domain value or a located error. It does not return a raw dictionary plus a boolean, and callers do not repeat the same checks.

- Parse JSON files with the owning Pydantic contract.
- Parse CLI text in the argument parser, including numeric bounds.
- Preserve unparseable external records as a distinct error value when the product must report them.
- Do not use `model_construct()` or unchecked casts to create states that normal input cannot produce.
- Do not make downstream policy code defend against fields required by its input type.

## Make impossible states unrepresentable

Prefer a union of complete states over one record full of optional fields.

Bad:

```python
@dataclass
class Result:
    ok: bool
    value: Value | None = None
    error: str | None = None
```

Better:

```python
@dataclass(frozen=True)
class Parsed:
    value: Value

@dataclass(frozen=True)
class Unreadable:
    error: str

Result = Parsed | Unreadable
```

Apply the same rule to configuration. Values that must occur together belong in one object. `NovelSpecPlan`, for example, pairs a positive requested count with the designer that can fulfill it; there is no count-without-designer state.

## Boundaries and seams

Inject a collaborator only when it is a real runtime boundary or has multiple production implementations. Do not add a fake default, compatibility fallback, re-export facade, or callable constructor option solely for a unit test.

Prefer, in order:

1. a pure function over parsed domain values;
2. a real boundary exercised with temporary filesystem state;
3. a narrow protocol for an actual external service;
4. a test-local implementation of that protocol.

A fake implementation belongs in tests. Production must never fabricate a successful model run, recovery, payment, or verifier result.

## Tests must justify their presence

Keep a test when it protects an observable contract, safety boundary, invariant, transition, precedence rule, or real failure mode. Delete it when it:

- parses production source to assert imports, call order, strings, or symbol counts;
- mirrors a registry, enum, schema, or command surface in a second golden artifact;
- proves a re-export refers to the original object;
- reaches private fields only to prove a test seam exists;
- constructs impossible states to exercise redundant downstream validation;
- asserts that a fake production fallback returns its own canned fixture;
- pins the changing size of a corpus that is intended to grow.

Exact snapshots and counts are justified only when the exact artifact or count is itself a named, stable contract.

## Completion rule

A domain-first change is not complete at the new type. Follow it through every caller, then remove obsolete branches, aliases, migration scripts, comments, tests, exports, and generated projections. The final tree should contain one current path, not a new path beside the old one.
