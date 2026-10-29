# SEAL-GUARD: sealed material out of default searches (2026-10-29)

Question: can a broad `rg`/`fd`/OMP `grep` over `research/` stop returning sealed
material (answer keys, TEST/held-out split files and rows derived from them), without
breaking any consumer?

What ran (no model calls, no sealed contents opened): `git ls-files` inventory by
pattern; a repo-wide search for consumers; `rg`/`fd`/OMP `grep`/`glob` counts before and
after; the governance check on the live tree and on throwaway Git repos.

## Numbers (measured, `results.json`)

- The root `.ignore` already existed (three archive patterns, #425, registered in
  `agents/STRUCTURE.md`). It was extended, not created.
- Sealed tracked files: 794 under `research/answer-keys/`, 2 `test.jsonl` under
  `research/datasets/`, 3 `qa-trace-sft-v0` result files that hold per-row TEST ids
  and labels (`baseline.py`, `harness_v2.py`, `harness_v3.py` write
  `predictions: [{id, gold, pred}]` for the test split), 2 `seeker-sft-v1` files
  built from `reported_test_split` rows (`quarantined.jsonl`, `source_manifests.json`).
  Total 801, all matched by `.ignore` after the change.
- `rg --files research`: 52165 -> 51365 (answer-keys files 793 -> 0). `rg -l expected_verdict research`:
  86 -> 27 files (answer-keys 59 -> 0). `rg --no-ignore` still returns 86.
- Consumers: no script, Makefile, hook, CI workflow, test or source module runs
  `rg`/`fd`/`git grep` or an ignore-aware walker over these paths. Every reader
  (guard.py, key builders, `CorpusRegistry`, tests) uses `open`/`Path.read_text`/
  `glob`, which ignore `.ignore`. The 142 tests that read the live keys and datasets
  pass (governance, splits guard, key builders, issue bank, corpus queue, seeker SFT).
- Governance: `evallab.governance check` now fails when a tracked path matching
  `SEALED_PATH_RULES` is not matched by `.ignore` (Git ignore semantics via
  `git ls-files -c -i --exclude-from=.ignore`). Fixture tests cover pass, a new
  uncovered `test.jsonl`, and a missing `.ignore`.

## Meaning and limits

- This hides sealed files from broad default searches only. It is not access control:
  direct reads, `git`, and `rg --no-ignore` reach them. Naming a sealed directory as
  the search root (`rg x research/answer-keys`) still searches it; so does an explicit file path.
- Calibration answer keys under `research/calibration/*/answer-keys/` are synthetic
  rubric fixtures, not split-sealed, and were left out. Experiment folders such as
  `research/experiments/*heldout*` were not classified as sealed and were left out.
- `.ignore` hides `research/answer-keys/README.md` and the key builders from
  default searches as well; read them by path.
- Untracked sealed material (e.g. `sealed-sources/`) is covered by the directory
  pattern but is not checked by governance, which sees tracked files only.
