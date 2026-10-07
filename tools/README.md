# tools/

## Purpose

Holds isolated, locked uv projects for external toolchains that Eval Lab
shells out to but must never absorb into the root dependency graph. Each
subdirectory pins its own dependencies with its own committed `uv.lock`.

## What lives here / entry points

- `tinker-sft/` — the Tinker chat_sl toolchain for HAR-81
  (`tinker==0.30.4`, `tinker-cookbook==0.5.7`): `measure.py` (offline render
  and token statistics for exported conversations) and the
  `tinker_cookbook.recipes.chat_sl.train` entrypoint. Invoked only as `uv run
  --project tools/tinker-sft --locked ...` from `src/evallab/sft_tinker.py`.
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
- `modal-mimo-sft/` — the Modal TRL LoRA SFT toolchain for HAR-81
  (`modal==1.5.5`, `transformers==5.12.1`, CPU-only lock; GPU deps pinned
  inside the Modal image): `sft.py` (`dry-run` offline render/mask/cost
  check, gated `train`/`merge` on one A100-80GB) plus a fixture export.
- `agentenv-bench/` — model-free MiMo task/grader controls and an AgentEnv port.

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
- `uv run --project tools/modal-mimo-serve --locked python
  tools/modal-mimo-serve/smoke.py --help` is the offline check for the
  Modal toolchain. A live smoke needs a deployed server and spends GPU time.
- `uv run --project tools/modal-mimo-sft --locked python
  tools/modal-mimo-sft/sft.py dry-run --data
  tools/modal-mimo-sft/fixtures/tiny-export` is the offline mask/cost check;
  `train`/`merge` refuse without `--confirm-spend` and start Modal GPUs.

## What not to add here

- Anything the lab imports as a library (that belongs in root
  `pyproject.toml`) or one-off scripts (those live in `scripts/`).
- Unlocked or floating dependencies; every pin here is exact.
