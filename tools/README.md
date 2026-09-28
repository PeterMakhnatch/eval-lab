# tools/

## Purpose

Holds isolated, locked uv projects for external toolchains that Eval Lab
shells out to but must never absorb into the root dependency graph. Each
subdirectory pins its own dependencies with its own committed `uv.lock`.

## What lives here / entry points

- `tinker-sft/` — the Tinker chat_sl toolchain for HAR-81
  (`tinker==0.30.4`, `tinker-cookbook==0.5.7`): `measure.py` (offline render
  and token statistics for exported conversations) and the
  `tinker_cookbook.recipes.chat_sl.train` entrypoint. Invoked only as
  `uv run --project tools/tinker-sft --locked ...` from
  `src/evallab/sft_tinker.py`.

## Invariants or rules

- No Eval Lab code imports anything from these environments; access is by
  subprocess through `uv run --project <dir> --locked` only.
- These projects are never workspace members of the root `pyproject.toml`;
  the root `uv.lock` must not change when one is added or relocked.
- Adding a new toolchain here needs a bucket justification in
  `agents/STRUCTURE.md`.

## Tests or checks

- `tests/test_sft_tinker.py` exercises the command construction and JSON
  contract of `tools/tinker-sft/measure.py` offline via an injected runner.
- `uv run --project tools/tinker-sft --locked python tools/tinker-sft/measure.py
  --help` is the smoke check for the isolated environment.

## What not to add here

- Anything the lab imports as a library (that belongs in root
  `pyproject.toml`) or one-off scripts (those live in `scripts/`).
- Unlocked or floating dependencies; every pin here is exact.
