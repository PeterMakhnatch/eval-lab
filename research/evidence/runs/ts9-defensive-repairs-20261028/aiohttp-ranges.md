# TS-9: aiohttp normal-response range advertisement

- Task: `mimo-v2.6-rl/format-code-task-000322` (DEV case `D0171`).
- Repaired key only: `mimo-weak-tests-v2.1:format-code-task-000322:block`.
- Base commit: `837dc7cad1072c93e05ed5eb3bcc4a2c38a07f7e`.
- Original package: `derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/format-code-task-000322`.
- Original package digest: `sha256:f8e3a0380faead850e005eb613b68d1018c1c8287c1e5ea6b2fda4b70cb950dc`.
- Original QA source SHA256: `2360fb369aa798259f3887eb44a9052db74b08c8fca127bff194a786a28b407e`.
- Original key source-record SHA256: `106b1c810b433482bf8a3b58e4cec76decc35ac3c6c4d5c59743e69175066bd2`.
- DEV assignments SHA256: `7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f` (parent-supplied cross-key assignment receipt).
- Candidate digest: `sha256:4be0f9507c0ecf1826ff2c88b0682db8d7fdcc0494f53c4d86e032a9292f1a5e`.
- Record: `library/task-variants/mimo-v2.6-rl__format-code-task-000322/4be0f9507c0e.json`.
- Transform: `verifier-repair@ts9-1`; status `candidate`; evidence `[]`; `validation_mode=static-only`.

## Authored behavior

Instruction lines 11–13 explicitly require range support to be advertised on normal file responses, including HEAD probes. The candidate changes only `tests/test.patch`, adding `test_static_file_accept_ranges_without_range` to the existing `tests/test_web_sendfile_functional.py` patch. It uses the existing `aiohttp_client` and `sender` functional fixtures and existing `data.unknown_mime_type` file.

The new case makes actual no-Range GET and HEAD requests. Both must return 200, `Accept-Ranges: bytes`, the full file's `Content-Length`, and no `Content-Range`. GET must return the exact fixture bytes; HEAD must return an empty body. These are response/content assertions, not source inspection, forwarding checks, or mocks. A normal response omitting the header is intended to fail this case; this rejection is not an executed result.

All pre-existing range and conditional-date tests are unchanged. The two date-boundary annotation keys (`line-3a97627cde` and `line-562cae1181`) are explicitly excluded from this repair. Instructions, task metadata, setup, verifier shell, embedded build archive, and runner commands are unchanged. Inspection of the embedded command showed that the existing runner selects the entire functional module, so no command wiring was added.

Public API inspection confirmed `TestClient.get`/`head` in [aiohttp v3.0.0 test_utils.py](https://raw.githubusercontent.com/aio-libs/aiohttp/v3.0.0/aiohttp/test_utils.py) (local copy: `/Users/petermakhnatch/Developer/research-context/sources/links/raw.githubusercontent.com/2026-10-28-https-raw-githubusercontent-com-aio-libs-aiohttp-v3-0-0-aioh.md`) and automatic HEAD registration by `add_get(..., allow_head=True)` in [web_urldispatcher.py](https://raw.githubusercontent.com/aio-libs/aiohttp/v3.0.0/aiohttp/web_urldispatcher.py) (local copy: `/Users/petermakhnatch/Developer/research-context/sources/links/raw.githubusercontent.com/2026-10-28-untitled.md`). This confirms public API availability, not exact-image runtime compatibility.

## Evidence boundary

static only; no native oracle or retained-shortcut trial executed

The patch and immutable record were authored/generated locally through `evallab.task_variants.derive_task` with the exact original package as direct parent and a worktree-owned variants store. No build, test, linter, formatter, patch-application check, Docker/cloud execution, model call, or reward measurement was run. Patch application, fixture compatibility, and the intended regression behavior remain untested here. This candidate is not native-qualified or promoted; the parent owns integrated verification and PR delivery.

Source boundary: requirements came from the assigned instruction/test preimage and public upstream APIs. No `solution/`, `solution.patch`, oracle patch, heldout package, or other task source was read.
