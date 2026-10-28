# TS-9 ASN1String conversion candidate

- Task: `mimo-v2.6-rl/format-code-task-001338`; admitted DEV case `D0116`.
- QA key: `mimo-weak-tests-v2.1:format-code-task-001338:block`.
- Validation mode: `static-only`; status: `candidate`; native evidence: empty.
- **static only; no native oracle or retained-shortcut trial executed**.
- Base revision: `837dc7cad1072c93e05ed5eb3bcc4a2c38a07f7e`.

## Immutable identity

- Original package: `derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/format-code-task-001338` in the primary checkout (read-only).
- Parent package digest: `sha256:bb254ac8363ead39d979510874add4a7233cfb144dbb6f409143d6f6bf4adc2a`.
- Candidate package digest: `sha256:e6540599550b87294a39a2f32ea18d9b7dc99b3b87c5fc5985f9db19b0bdc62c`.
- Parent Harbor digest: `sha256:c47a97c894e1b0d63ad403490b2e6e183c2861505a53b0e9f47cecabee24b280`.
- Candidate Harbor digest: `sha256:77a33c21619619b9869578ea364e756a6541b20df0ecadb00a5524d643c24970`.
- Generated record: `library/task-variants/mimo-v2.6-rl__format-code-task-001338/e6540599550b.json`.
- Transform: `verifier-repair@ts9-1`, generated through `evallab.task_variants.derive_task`; original parent unchanged; materialization confined to this worktree's `derived/task-store/variants`.
- Original source receipt SHA256: `aa7664a4ef3b3a07c4a570f78a9cb7f96e1fd188a550a07d4c3979808c552254` (`inputs/dev/D0116/source.txt` in the 2026-10-27 `oq6-tq-gate-rerun` results).
- QA source-record SHA256: `44abb781d0ebf8218853da904861129cce653e0655c763aaf408de643f78c056`.
- QA keys SHA256: `f7d4156093b77fecdae050f9ea1ffee68df856147957259be717db5f93bd00e8`.
- Assignment SHA256: `7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`; supplied parent cross-key DEV admission receipt, not independently rerun here.
- Native changed file: `tests/test.patch` only; SHA256 `191b0e29771eb9b9aafbc00775e1bcdf634e4ff88ba6cbf4542eda52afb15789` → `6d6eb60c37db61efef932a6e332b57b9d66aa85ae424a6b47cd60e3ceefdd0cb`.

## Authored semantic coverage

The sole patch change adds 12 Python lines inside the existing selected constructor tests in `python/ct/crypto/asn1/types_test.py`, plus the required unified-diff hunk-length adjustment:

| Existing selected test | Intended conversion rejection | Valid conversion control |
|---|---|---|
| `test_printable_string_constructor_rejects_invalid_characters` | Four existing invalid values wrapped with unrestricted `types.ASN1String` must raise `error.ASN1Error`. | Convert the existing valid repertoire sample and compare its string representation with direct valid construction. |
| `test_ia5_string_constructor_rejects_non_ascii` | Three existing non-ASCII values wrapped with unrestricted `types.ASN1String` must raise `error.ASN1Error`. | Convert `hello@example.com` and compare with direct valid construction. |
| `test_visible_string_constructor_rejects_control_del_and_non_ascii` | Five existing control/DEL/non-ASCII values wrapped with unrestricted `types.ASN1String` must raise `error.ASN1Error`. | Convert the existing space/punctuation/alphanumeric valid sample and compare with direct valid construction. |

Unrestricted source construction is evaluated before the `assertRaises` call, so an error creating the source cannot satisfy the target conversion assertion. These are behavioral tests; no production helper, validation mechanism, or implementation layout is prescribed.

## Existing runner contract retained

`tests/test_command.sh` invokes `bash /testbed/mimo_test_command.sh base && bash /testbed/mimo_test_command.sh new`. In this exact parent, `mimo_test_command.sh` is a plain-text hunk in `tests/test.patch`, not an encoded `mimo_build_env.tar.gz.b64` member. Its existing explicit new-mode pytest `-k` expression already names all three extended tests. No command wiring or encoded generated build tar was changed.

The existing direct invalid-construction assertions, strict invalid-decoding tests, non-strict legacy-decoding cases, unrestricted-string cases, base selection, and new selection are retained. No new test method depends on discovery or a changed selector.

## Limits and handoff

Public API corroboration only: the pinned package does not include the production Python source. Static public upstream [`types.py`](https://raw.githubusercontent.com/google/certificate-transparency/master/python/ct/crypto/asn1/types.py) exposes the `ASN1String` constructor/conversion API and `__str__` behavior; this is not a native-base or oracle qualification. Archived copy: `/Users/petermakhnatch/Developer/research-context/sources/links/raw.githubusercontent.com/2026-10-28-https-raw-githubusercontent-com-google-certificate-transpare.md`, archive content SHA256 `b69b69d5a7b4e836f48f66a95c65ee14a525679ba21dc6cdcbf95b08f742a17c`. No solution directory, solution patch, or oracle patch was read.

Source-boundary disclosure: the first scope-file jq query selected the entire DEV cases array and displayed unrelated scope metadata; it was corrected to select `format-code-task-001338` alone. No unassigned native package source or held-out/TEST package was opened.

Only source inspection, metadata reading, and immutable record generation were performed. No tests, builds, linters, formatters, patch-application checks, native oracle/shortcut controls, Docker/cloud execution, model calls, task/artifact uploads, checker use, or promotion were performed. Rejection of the historical wrong-patch witness and acceptance of a legitimate native repair are intended, not measured. The parent owns the integrated verification batch and subsequent PR/CI/merge; this candidate makes no native-qualification claim.
