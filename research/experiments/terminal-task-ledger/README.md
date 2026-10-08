# Terminal task ledger: guard + prefetch validation

One $0 fix for the 64 MiMo-V2.6-RL terminal tasks
(`FineEnvs/MiMo-V2.6-RL-harbor-terminal@fe1c2b665aae1ba7a09a270d979724d32269ae6a`):
`terminal-guard-extend@1` closes the proven site-packages hook-planting hole
on all 64, `env-prefetch-network@1` repairs the two missing-module graders,
and the ledger records keep/fix/discard per task.

## Files

* `build.py` — builds `ledger.csv` (one row per task) from the pinned
  snapshot, the lineage records and `validation.json`. Re-run after any
  snapshot, variant or validation change:
  `uv run python research/experiments/terminal-task-ledger/build.py`
* `derive.py` — derives the 64 guard + 2 prefetch variants (refuses to
  overwrite; re-runnable).
* `grade_local.py` — grades one task package in local Docker
  (`--network none` default) with Harbor's ordering: setup runs before tests
  are materialized (setup itself does `rm -rf /tests`), optional `--plant`
  of the skip-all hook, `--commit-to`/`--skip-setup` for the networked
  prefetch setups graded offline afterwards.
* `validation.json` — hand-recorded MEASURED results; full outputs in
  `validation-logs/`.
* `ledger.csv` — generated: 59 keep / 4 fix / 1 discard.

## Static census (all 64 graders, $0)

Commands (run from the repo root; `T` = the pinned snapshot tasks dir):

* pytest invocations: every `task_test.sh` has exactly one
  `python3 -m pytest` line, always targeting `/tests/test_outputs.py`
  (63 absolute, 2951 relative with `"$@"`). No `-p` plugin-enabling flag
  anywhere (only 0674's `-p no:cacheprovider`, which is core).
* Plugin surface in `test_outputs.py`: only core pytest features
  (`parametrize` ×3, `raises`/`approx`/`fixture`); no asyncio/trio/anyio,
  no third-party `pytest-*` imports, no `pytest_plugins` declarations.
* No `conftest.py` / `pytest.ini` / `setup.cfg` / `pyproject.toml` /
  `tox.ini` / `.coveragerc` under any `tests/` or `tests/fixtures/` dir.
* No `test_outputs.py` reads `PYTEST_PLUGINS` / `PYTHONPATH` /
  `PYTHONSTARTUP` from the ambient environment (1990 sets its own
  `PYTHONPATH` for an explicitly spawned child probe).
* Compiler use in `test_outputs.py`: only 0109 (`os.environ.get("CXX",
  "g++")` compile-and-run) and 2836 (`g++ -std=c++20 -fsyntax-only`).
  0758's `defs.cc` is a source-text assertion, not a compile.
* `anti_hack_guard.py`, `test.sh` byte-identical across 64/64;
  `task_test.sh` shares the guard stanza; `setup.sh` byte-identical 64/64.

## Image surface census (MEASURED, `--network none`, read-only probes)

All 12 locally cached images (11 pre-existing + pulled 2376, the only new
pull) share one Python base env:

* single site-packages (`/usr/local/lib/python3.12/site-packages`),
* module presence: numpy/sklearn/pandas/joblib/packaging/petl/mido/requests
  present; `stevedore`, `cryptography`, `torch` absent (12/12).
* no `g++`/`gcc`/`cc` on PATH (12/12);

Images for the other 52 tasks are not cached (no further pulls; ≤2 allowed,
1 used). Their base env is inferred uniform from the 12 measured
([INFERENCE]); every guard variant snapshots its own image at setup, so the
protection does not depend on that inference.

## Validation table (MEASURED, local Docker)

| task | control | result |
|---|---|---|
| 0109 original | plant sitecustomize+zzskip | reward **1**, 7 skipped (hole reproduced, matches investigator) |
| 0109 guard | same plant | reward **0**, 5 failed / 2 passed / 0 skipped, `sitecustomize.py` quarantined, ctrf written |
| 0109 guard | do-nothing | reward **0**, identical 5F/2P (plant had zero effect) |
| 0036/0308/0390/0628/0688/0758/1789/1990/2836 guard | do-nothing, `--network none` | reward **0** each, tests ran (4–7 per task), ctrf written, 0 skipped |
| 0260 original | do-nothing | collection error `No module named 'stevedore'` (`from stevedore import extension`) → 0 |
| 0260 prefetch+guard | setup with network, grade `--network none` | 5 collected (4F/1P) → **0** |
| 2376 original | do-nothing | collection error `No module named 'cryptography'` (`from cryptography.hazmat.backends import default_backend`) → 0 |
| 2376 prefetch+guard | setup with network, grade `--network none` | 6 collected (6F) → **0** |
| 0109 guard | `test_core_native_cpp_sampler_compiles_and_runs` alone | `FileNotFoundError: 'g++'` |
| 2836 guard | `test_core_changed_cpu_modules_compile_as_cxx20` alone | `FileNotFoundError: 'g++'` |

Oracle status: **unprovable as shipped** — no reference solutions exist in
any package (0109's grader even asserts `not (APP/"solution").exists()`),
so no control can show reward 1. "Scores 0" throughout means tests ran and
failed (ctrf counts), never a silent pass.

## Residual limits

* An agent that deletes `/var/lib/mimo/terminal-guard-snapshot.json`
  downgrades grading to quarantine-`sitecustomize`-only plus unpinned plugin
  autoload — the same downgrade class as deleting the upstream pristine
  manifest. The primary path (snapshot present) is airtight against the
  proven attack.
* Honest editable installs adding `*.pth` files are quarantined at grade
  time (imports then fail closed to 0). No grader needs them (static census).
* 0109/2836 still need a setup-time compiler prefetch (not built here);
  ledger marks them `fix`.
* 0674 is discarded (torch); ledger marks it `discard`.
* 52 uncached tasks are validated statically only; their guard variants are
  derived and lineage-bound, awaiting image pulls for dynamic confirmation.
* Training-time setup needs the network for the two prefetch installs
  (public `network_mode` in Harbor is open; a fully locked setup would fail
  closed at the pip step, not silently grade).
