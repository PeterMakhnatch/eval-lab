# tools/

## Purpose

Holds isolated, locked uv projects for external toolchains that Eval Lab
shells out to but must never absorb into the root dependency graph. Each
subdirectory pins its own dependencies with its own committed `uv.lock`.

## What lives here / entry points

- `modal-mimo-serve/` — the Modal client (`modal==1.5.5`) plus the HAR-90
  SGLang server app for `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`: `serve.py`
  (the deploy and weight-download targets), `smoke.py` (a stdlib latency and
  reasoning-split probe) and a README covering deploy, stop and cost. It is
  invoked only as `uv run --project tools/modal-mimo-serve --locked modal ...`;
  the lab reaches the server through the `mimo_selfhosted` proxy provider.
- `mimoagent-harbor/` — Xiaomi's pinned native controller (Python 3.12,
  OpenAI 3.x) and byte-identical `swe.yaml`, isolated from Harbor/LiteLLM's
  OpenAI 2.x dependency graph. Install with
  `uv sync --project tools/mimoagent-harbor --locked`; Harbor invokes
  `src/evallab/mimoagent_worker.py` through this interpreter with `-I`.

## Invariants or rules

- No Eval Lab code imports anything from these environments; access is by
  subprocess through `uv run --project <dir> --locked` only.
- These projects are never workspace members of the root `pyproject.toml`;
  the root `uv.lock` must not change when one is added or relocked.
- Adding a new toolchain here needs a bucket justification in
  `agents/STRUCTURE.md`.

## Tests or checks

- `uv run --project tools/modal-mimo-serve --locked python
  tools/modal-mimo-serve/smoke.py --help` is the offline check for the
  Modal toolchain. A live smoke needs a deployed server and spends GPU time.

## What not to add here

- Anything the lab imports as a library (that belongs in root
  `pyproject.toml`) or one-off scripts (those live in `scripts/`).
- Unlocked or floating dependencies; every pin here is exact.
