# TS-9: Dynaconf environment booleans

**Candidate only — validation_mode=static-only.**

static only; no native oracle or retained-shortcut trial executed

## Exact binding

- DEV case: `D0330`, `format-code-task-000847`.
- QA key: `mimo-weak-tests-v2.1:format-code-task-000847:block`.
- Original package / direct parent: `sha256:a4f5adc9786868367f9d44a858a32936316b52dbd36ad724c9d859cd992f60d6`.
- Original Harbor digest: `sha256:22170f3086e5b009351f841e7eef0c282b4daeca93b3c392c828f4c6a68671ec`.
- Admitted source SHA256: `3f53d43c67077437d49d36c601e74508a330eb43f5d0d5728994362e5a009e44`.
- QA source-record SHA256: `2b7f84a42c42ec46f228fd574283cf7e83ac125674f05cd40e16974a5e538aee`.
- QA keys SHA256: `b2500f156cd449c78902a379d4a2249eb4ab3e84e982d6902892c30557cdf06f`.
- Assignment SHA256: `7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`.
- Transform: `verifier-repair@ts9-1`; generated with `evallab.task_variants.derive_task` from base `837dc7cad1072c93e05ed5eb3bcc4a2c38a07f7e` in an isolated native worktree/store.
- Record: `library/task-variants/mimo-v2.6-rl__format-code-task-000847/8469c93fb1bf.json`.
- Variant: `sha256:8469c93fb1bf2686bb98e5d33add4a2806bdcd0b08f40d5024fd76a630dd2b9e`.
- Variant Harbor digest: `sha256:c899fbf3523bfaa7412e6de2fe541d1d561c91c6f9881affd36182fe2e3d9af3`.
- Native changed file: `tests/test.patch` only, SHA256 `36aa977881a570d4dc41ae8c91aa1c27471255d29db39d30c31ef6ee325623fa` → `7ebfbd542aeadd06e01b336b9219b340b05d7e2d61d196eaee7639398d770d7f`.

## Authored behavior and rationale

The two existing selected tests are extended, not replaced by new command wiring. The loader test now uses fresh real settings and the same ten instruction-supported values with `key=None` and `key="VALUE"`. Initialization precedes setting the variable, so an initial bulk load cannot satisfy the single-key assertion. The settings-get test reads through `default_settings.get` before initializing a new instance, then retains the explicit `sysenv_fallback=True` read.

Each parameterized case clears/restores the process environment, uses its own temporary working directory and fresh settings, and disables settings files and dotenv. Boolean outcomes use identity assertions. The matrix includes bare `True`/`False`, lowercase `true`/`false`, quoted and `@str` versions of both words, and the existing TOML-array/table string examples. These controls are expressly required by instruction lines 9–14; no additional syntax or TOML semantics is imposed. The other existing tests, including TOML-file controls, and all runner commands remain unchanged.

This is intended to reject an implementation that fixes bulk loading while leaving explicit single-key or early/default environment reads as strings, and to reject normalization that corrupts explicit or embedded strings. Those rejection behaviors are authored expectations, not executed results.

## Evidence limits and handoff

Read the admitted instruction, original test patch, runner and setup metadata. Public `dynaconf/dynaconf` revision `455ee38569d76cd3707c73209b660cf60bd97b29` supplies supporting API evidence in `dynaconf/loaders/env_loader.py`, `dynaconf/default_settings.py`, `dynaconf/base.py` and `tests/test_env_loader.py`. In particular, explicit loading accepts a real settings object's `set`, and the default getter reads the environment independently of instance loaders. This upstream revision is not proven to be the image's base: the admitted setup resolves that base only inside the image.

No solution/oracle patch, TEST/heldout package, reserved cross-task source, or image checkout was read or executed. No source-boundary deviation occurred. Scope source/assignment hashes above are supplied admission metadata; the derivation itself computed the parent/variant and changed-file digests. Status remains `candidate`, with empty native evidence and record evidence arrays. No builds, tests, lint, formatting, patch-application/collection check, native trial, model call or cloud operation was run. Parent owns integrated static/application checks, CI and any separately authorized native qualification.
