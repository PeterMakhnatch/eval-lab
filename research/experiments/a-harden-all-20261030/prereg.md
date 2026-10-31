# A (Phase 2 stream A) prereg — harden every DEV format-code package

Written 2026-10-30 evening ET, after the local classification (A1) and before any Modal cell of A2/A3. Not edited afterwards; deviations go in the report.

## Question

Does `grader-hardening@1` (L1 auto-load reset + L3 JUnit gate) reject every in-container attack and accept the correct solution on **every** DEV `format-code-task-*` package it applies to, not just the 12 sampled in H1? And for the packages it only partly applies to (L1 only), which attacks survive?

## Population and classification (A1, local)

All 36 `format-code-task-*` packages of `SK-2/receipts/tasks.jsonl` (the DEV set used by TQ-1/TQ-2). `classify.py` writes `classification.json`: strict DEV (`CorpusRegistry.assigned_split == "dev"` and not `assigned_test_anywhere`), vendored reference patch with matching sha256, reset-list `tests/test.sh` shape (a dry `build_changes`), runner kind and `PYTEST_ADDOPTS` handling → `full` (L1+L3) / `l1-only` / `not-hardenable`. Packages not strictly DEV are not touched.

## A2 — executed matrix on every `full` package

- Population: every `full` task in `classification.json`, no sampling. A task whose calibration fails (reference run on the upstream grader with a JUnit request gives no passing report) is moved to `l1-only` for A3 with that reason; it is never dropped silently.
- Cells per task: grader level ∈ {L0 original, L1 (hardened, no report gate), H = L1+L3} × arm ∈ {correct, empty, A, B, C, D, E2, F1, F2, F3}; E2 only where the reference patch touches a non-test source file. One execution per cell, on Modal through `ModalBackend` (shortcut channel), lab Harbor 0.24.0 from `.venv`. Attack scripts are those of H1 (`attacks.py`, F3 payload Python 2/3-compatible).
- **Gate (all must hold):** (1) `correct` at H = 1 on every task; (2) `empty` at H = 0 on every task; (3) every in-container attack (A, B, C, D, E2, F1, F2) = 0 at H on every task. F3 (report forgery from a `.pth` in the grader's interpreter) is **listed per task as the known survivor**, not part of the gate. An unmeasured cell (no native reward, attack not applied, error) counts as **not passed** for the gate and is listed.
- Reported numbers: false rejects (tasks with `correct` at H ≠ 1, or `empty` at H ≠ 0), attacks live at H (cells H attack = 1 among A,B,C,D,E2,F1,F2), tasks classified. Per attack: live at L0 count, still-1 at L1, still-1 at H, Wilson 95% over tasks for neutralised/live.
- A task failing the gate is reported cell by cell with a reason class: package shape or recipe.
- Cap: `--max-usd 4.0` on the measured estimate (sandbox seconds × Modal rates × 1.25); `--workers 6`.

## A3 — L1-only evidence

Population: every `l1-only` task (static non-pytest or report-gate blocked, plus calibration failures). Variant: `derive_grader_hardening(allow_no_report_gate=True)` (L1 only). Cells: {L0, L1} × {correct, empty, A, B, C, D}. Reported: per task, which of A–D are live at L0 and which survive L1; `correct` at L1 must be 1 (false-reject check). Attacks A–D are pytest-flavoured, so on a non-pytest runner a cell can be inert at L0; inert cells are listed as such, not credited. E2/F1/F2/F3 are not run for L1-only tasks (L1 does not address them; H1 measured that). L1-only is evidence, not a guarantee.

## Stopping and disclosure

No cell is rerun to change its value. A cell is rerun only if it did not execute (infrastructure error, spend-gate refusal) and the original result file is kept in a `retired-*` directory. Every rerun, script or recipe change is disclosed in the report. DEV only; nothing under `research/answer-keys/`.
