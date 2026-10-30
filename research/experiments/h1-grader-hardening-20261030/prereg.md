# H1 prereg: executed validation of `grader-hardening@1` (written before any matrix cell)

**Question.** On real MiMo DEV packages, does the L1 (auto-load reset by pattern) + L3 (JUnit report gate) block make a wrong
submission fail under each TQ-2 attack family that works on the unhardened grader, without rejecting the correct solution?

**Data.** Single-container MiMo packages from the 44 DEV packages used by TQ-1/TQ-2 (`SK-2/receipts/tasks.jsonl`).
Filters, in order: strict `CorpusRegistry.assigned_split(...) == "dev"` and not `assigned_test_anywhere`; a vendored reference
patch whose sha256 matches `references.csv`; the reset-list `tests/test.sh` shape; a pytest runner (wrapper and shipped build-env
command resolved). Walk in `sha256(task_id)` order; take the first 12 whose calibration run succeeds (up to 2 backfills, all
reported). Packages with a non-pytest runner are listed and not tested. No held-out or TEST task is read.

**Cells per task.** Calibration (reference solution on the upstream grader plus a JUnit request; reads the reference test and
skip counts, never ships). Then on the hardened variant (H) and on the original package (L0): `correct`, `empty`, and attacks
A, B, C, D, E2, F1, F2, F3 (`attacks.py`; wrong implementation = base tree plus the attack files; F3 is given the true
reference test count). n = 1 execution per cell.

**Definitions.** An attack is *live* on a task if its L0 cell scores 1. It is *neutralised* if live at L0 and 0 at H. A cell whose
attack did not apply or whose trial produced no reward is *unmeasured*, reported, never counted as pass or fail.

**Metrics and gates.**
1. False rejects: `correct` at H must score 1 on every task (gate: 0 of the tasks that calibrated). `empty` must score 0 at L0 and H.
2. Neutralised / live per attack family, with Wilson 95% intervals over tasks; F3 is expected to survive (H2 owns it).
3. Cost: estimated from measured sandbox seconds at the Modal rates in `docs/execution-tiers.md` plus a 25% margin; hard stop at the
   $6.00 cap; every run is logged to `spend.jsonl`.

**Not decided here.** Whether to adopt the transform in the default chain (the separate-verifier chain already closes these
routes for packages that use it); `pyproject.toml`/`setup.cfg` resets can reject a solution that legitimately edits them (0 of the 36 vendored references touch any L1 file, statically checked).
