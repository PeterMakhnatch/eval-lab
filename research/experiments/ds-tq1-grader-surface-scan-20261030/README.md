# ds-tq1-grader-surface-scan-20261030

Static scanner for grader-weakness surfaces in DEV task packages (TQ-1).

- `scan_grader_surface.py` — stdlib-only scanner. Adds no state: reads one package
  directory, prints `{task_id, features, class_flags}`; every flag carries a rule name,
  evidence file and line. Never executes package code and never opens `solution/`.
- `test_scan_grader_surface.py` — rule tests on three hand-written toy packages
  (same-container hook surface, separate-verifier artifact surface, command-wrapper tamper).
- `evaluate.py` — reruns the frozen scanner over all 44 tasks and scores the held-out half.
- `results/` — `scan-all.json`, `scan-heldout-once.json`, `scan-design.json`,
  `heldout-eval.json`, `split.json`, `recorded-defects.json`, `ambiguous-calls.jsonl` (empty:
  no model calls were made).

Frozen-run scanner sha256 `33f27c2d79f1ca2eff558b1299273f488f598badad1f383dd95a879923a1ba46`; the
published file is `0bbec47d390d78b6475db5e413d4e63fd63dc39db3222275ead2f12dfc951067` (two lint-only
renames, outputs byte-identical).
Preregistration, full numbers and limits: the TQ-1 report under
`research-context/bridge/2026-10-29-ds-squad/raw/TQ-1/`.

Run:

```bash
python3 scan_grader_surface.py <package-dir>
python3 test_scan_grader_surface.py
python3 evaluate.py
```
