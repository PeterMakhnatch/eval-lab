---
status: living
audience:
  - builder
  - operator
---

# DirectoryQueue property-based fuzzing

`tests/test_queue_properties.py` contains a `hypothesis.stateful.RuleBasedStateMachine` that drives `DirectoryQueue` through submit/approve/reject/dispatch/complete/fail sequences.

## The four invariants asserted on every step

1. **Conservation** — no spec is ever lost or duplicated: the multiset of spec ids across all eight state directories equals the set of ids submitted, and each id appears in exactly one state.
2. **No double dispatch** — a spec reaches `running` at most once.
3. **Legal transitions only** — every `from_state -> to_state` pair is one the real implementation permits (asserted against the explicit `ALLOWED_TRANSITIONS` map declared in the test and kept consistent with `queue.py`).
4. **Admission is respected** — a billable spec never appears in `approved/` or `running/` without an explicit `approve(...)`; free control agents (`oracle`, `nop`) may be auto-admitted by the policy gate.

## Reproducing a failing hypothesis example

When a counter-example is found, hypothesis prints the falsifying example and the seed used. Re-run with:

```
uv run pytest tests/test_queue_properties.py --hypothesis-seed=<seed> -q
```

The exact sequence of rule calls appears in the failure output.

## CI runtime bound

The test is configured with

```python
settings(max_examples=150, stateful_step_count=25, deadline=None)
```

This bound keeps the entire file under ~60 seconds in CI even on slower runners.

## Machine-local corpus data

`derived/`, `runs/`, `queue/`, and the external Harbor corpora are machine-local and absent in CI.

A test needing corpus-shaped data builds a fixture at the real layout and injects it.

A test that genuinely requires the real corpus must `skipif` with a reason naming the missing path.

A test that passes only on the author's machine is a test that does not exist.

## Catalog isolation

Tests never write to the live catalog; anything needing a real database creates and drops an isolated one.

A test session needing PostgreSQL derives an isolated database or schema from the active `DATABASE_URL`, initializes required schema objects, and drops the isolated target on teardown so live tables remain byte-for-byte untouched.
