# TS-9 cartesian-api: static candidate

- Task: `mimo-v2.6-rl/format-code-task-000232` (eligible DEV case `D0399`).
- QA keys: `mimo-weak-tests-v2.1:format-code-task-000232:block` and `mimo-weak-tests-v2.1:format-code-task-000232:line-57a0b56cc6`.
- Base: `837dc7cad1072c93e05ed5eb3bcc4a2c38a07f7e`; branch: `repair/ts9-cartesian-api-20261028`.
- Transform: `verifier-repair@ts9-1`; `validation_mode=static-only`; status remains `candidate`, native evidence remains empty.
- Record: `library/task-variants/mimo-v2.6-rl__format-code-task-000232/fb00505ebac8.json`.
- Original package: `sha256:627d6082cbe17636ef46359720c693ceb22983935209f0d3928ef00998c64fda` (the derivation's parent digest matches the assigned QA pin).
- Candidate package: `sha256:fb00505ebac8b48c500a70d42ddf177bd9bc776877344273dbc7791f340f0eb4`.
- Original source SHA256 from eligible metadata: `0e3ebcbe9a9f2f4858b7a9c2d92da37eb1276f42dcf2ca5efea79e57ba8649f8`; source text was not reread.
- Assignment SHA256 supplied by parent: `7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`.
- Scorer-key SHA256 from eligible metadata: `cadb8a02bd2dadd26e0c93afb7ed9e8a9c14ed16cb82a9b3562bb5617f1a18c1`.

## Authored behavior and semantic rationale

Only package file `tests/test.patch` changes (before SHA256 `001a14324d1710198cd68e0b83307fe6792bcfbeea14aabdae67e37ce56745f6`, after `1b4c756e15821647171b77e2eebb59333922ffc594e98c3ed10f0a3f2ff15b7e`). The native target is `quantecon/tests/test_gridtools.py`: add `import quantecon as qe` and extend its existing `TestCartesianNearestIndex` class.

The instruction demonstrates the top-level `qe` API and requires single/batched queries with both Cartesian orders. The added test calls public `qe.cartesian_nearest_index` for every query singly and together as a batch. Five tiny grid shapes cover a singleton axis in the leading, middle and trailing positions, an all-singleton 3-D grid, and a one-dimensional singleton grid. Each is parameterized over `C` and `F`; queries include interior and out-of-range coordinates. Expected indices use independent `np.argmin` brute force over at most six Cartesian points. No new timing, implementation-fingerprint or source-text assertion is introduced.

Existing tests and command inclusion are retained. The unchanged embedded `.build_env/test_command.sh` selects the whole `quantecon/tests/test_gridtools.py` file with `python -m pytest -xvs quantecon/tests/test_gridtools.py`; neither the embedded payload nor either command wrapper changes.

## Limits and source boundary

**static only; no native oracle or retained-shortcut trial executed**. No builds, tests, linters, formatters, Docker, cloud/sandbox execution, model calls or shortcut execution were run. `derive_task` generated the candidate and its lineage; patch applicability, runtime compatibility, correct-solution acceptance and wrong-solution rejection remain untested. These behavioral cases do not prove the requested asymptotic complexity.

Requirements came from the assigned instruction and test preimage. Inspection also covered this package's task/runner metadata, its containing snapshot's provenance, scoped assignment metadata and baseline derivation helpers. No `solution/`, `solution.patch`, oracle patch, TEST/heldout or reserved cross-task source was read; no source-boundary deviation occurred. The exact original parent and existing variants remain unchanged. Parent owns integrated verification, PR and merge; this is not native qualification or promotion.
