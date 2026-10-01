# HAR-115: repairs, census completion and defect origins

Part of HAR-115, following HAR-113 ([`../har113-variants/README.md`](../har113-variants/README.md)).
The scripts reuse HAR-113's toolbox in `../har113-variants/` (repair kinds,
`build`, `write_probe`, `write_specs`, `runner.py --prefix har115-`,
`census_update.py`); this directory holds the HAR-115 inputs and results.

| Part | Result |
|---|---|
| 1. Repairs of the 51 tasks HAR-113's waves found | 44 repaired: broken before, `sound` after, `validated` (35 keep-build-outputs, 5 login-path, 2 pin, 1 keep-files, 1 new login-pythonpath). 1 more was a Daytona conflict (re-nop sound), 1 a census false positive (001860). 5 remain (below) |
| 2. Census nops of the last 198 unknowns | 198 of 198 nopped: 165 sound, 32 broken_environment, 1 grader_suspect. The census has no `unknown` left. Of the 33 new finds, 21 whose error line names a known kind got one repair nop each: 15 `validated`, 6 `rejected`; the other 12 were not attempted (fix-or-discard rule below) |
| 3. Classifier false positives | 000211, 001981, 002207 now `sound`, plus 001860 of the same shape; 002307's post-repair nop too. No other label moved across 979 re-labelled nops |
| 4. Defect origins | [`ORIGINS.md`](ORIGINS.md) |
| 5. Python task ledger | [`../python-task-ledger/`](../python-task-ledger/): all 1,180 tasks as usable / review / discarded / unchecked, the package to run, and 30 proposed for HAR-120 |
| Spend | $1.9807 Daytona over 274 trials (cap $2.25); no model calls |

Census (`../har108-python-census/task_health.parquet`, [`SUMMARY.md`](../har108-python-census/SUMMARY.md)):
sound 1040, broken_environment 135, grader_suspect 5, unknown 0 (was 870 / 107 / 5 / 198).
The census labels original tasks. There are **110 unique validated repair
variants**: HAR-113's 51 plus HAR-115's **59** (44 initial + 15 round-four),
with no task overlap. Validation here means nop-sound, not demonstrated
solvability or automatic admission: the audited ledger at `a90a7291` places
92 repairs in usable, 17 in review and 1 in discarded, before the overnight
HAR-127 review-queue triage.

HAR-132's read-only replay at that revision reproduced all 1,180 census
labels and ledger statuses. `census_update.jobs()` now resolves HAR-115 through the same
retained-worktree/archive rule as earlier cohorts, rather than silently
looking in an empty analysis checkout's `runs/`. The October 1 retained
snapshot yields 1,437 nop jobs, including all 265 HAR-115 jobs (199 nop,
66 repair-nop). Reproducing discovery does not invoke `main()` or rewrite
the shared catalog.

## 1. Repairs

Each repair changes only `environment/setup/setup.sh` (re-embedded in the
healthcheck payload), derived from the task's leak variant when it has one, so
PyPI stays blocked. Kinds are HAR-113's (`../har113-variants/repair_variants.py`)
plus two extensions:

- `login-path-pyenv` (`env-login-path@2`) takes extra tools to wrap: 000968's
  test command calls `py.test`, which the login shell resolves to pyenv 2.7.18.
- `login-pythonpath` (`env-login-pythonpath@1`, new): the image's `.bashrc`
  exports `PYTHONPATH=/testbed`; setup writes the login shell's entries to a
  `.pth` file for the non-login python (001176).

Matching: 24 tasks were matched to a kind from the error line alone; the other
27 were read from their HAR-113 nop logs first (free), which matched 17 more.
Six took an oracle diagnosis (`specs.py` `DIAGNOSE`, `har115-diag-*`; three
second rounds, `har115-diag2-*`, for 000393, 001176 and 001618) and four were
left to the classifier (part 3). Verdicts wait for the classifier
fix (`outcomes.py --verdicts`) because a verdict is final: 002307's repair
nop first read `broken_environment` only because of the false positive fixed
in part 3. Before that flag existed, two runs of `outcomes.py` wrote that
rejection into the uncommitted record; it was reset to `candidate` by hand
both times, never committed, and the flag now keeps verdicts behind the fix.

| task | cause | repair | nop before | nop after | variant | status |
|---|---|---|---|---|---|---|
| 000158 | package metadata (PackageNotFoundError) | keep-build-outputs | broken_environment `har113-nop-000158` | sound `har115-rnop-000158-648af3093326` | `648af3093326` | validated |
| 000159 | flake8_simplify package metadata | keep-build-outputs | broken_environment `har113-nop-000159` | sound `har115-rnop-000159-2ad471b8c15d` | `2ad471b8c15d` | validated |
| 000160 | flake8_simplify package metadata | keep-build-outputs | broken_environment `har113-nop-000160` | sound `har115-rnop-000160-6e67105d2c97` | `6e67105d2c97` | validated |
| 000161 | flake8_simplify package metadata | keep-build-outputs | broken_environment `har113-nop-000161` | sound `har115-rnop-000161-eee3b18c925e` | `eee3b18c925e` | validated |
| 000190 | pydp._pydp extension | keep-build-outputs | broken_environment `har113-nop-000190` | sound `har115-rnop-000190-dd4c842c8aee` | `dd4c842c8aee` | validated |
| 000354 | package metadata (DistributionNotFound) | keep-build-outputs | broken_environment `har113-nop-000354` | sound `har115-rnop-000354-2e6e4ec12b96` | `2e6e4ec12b96` | validated |
| 000968 | the test command runs py.test; the login shell's pyenv 2.7.18 runs this Python 2 code (urlparse), the non-login py.test is Python 3's | login-path-pyenv | broken_environment `har113-nop-000968` | sound `har115-rnop-000968-818dcd350ca0` | `818dcd350ca0` | validated |
| 001024 | the login shell's pyenv 3.7.17 has pytest and cgi.escape; the grader's non-login pytest is Python 3.11's, where cgi.escape is gone | login-path-pyenv | broken_environment `har113-nop-001024` | sound `har115-rnop-001024-edfd45ce7e93` | `edfd45ce7e93` | validated |
| 001058 | oslo_config missing for the non-login python | login-path-pyenv | broken_environment `har113-nop-001058` | sound `har115-rnop-001058-4793b3d87a6c` | `4793b3d87a6c` | validated |
| 001068 | django missing for the non-login python | login-path-pyenv | broken_environment `har113-nop-001068` | sound `har115-rnop-001068-095c628afbf9` | `095c628afbf9` | validated |
| 001176 | the image's .bashrc exports PYTHONPATH=/testbed; the grader's non-login python cannot import bzt from /testbed | login-pythonpath | broken_environment `har113-nop-001176` | sound `har115-rnop-001176-52862dc0cdbb` | `52862dc0cdbb` | validated |
| 001406 | pytest: command not found | login-path-pyenv | broken_environment `har113-nop-001406` | sound `har115-rnop-001406-db655f2e25fb` | `db655f2e25fb` | validated |
| 001607 | cloup/_version.py | keep-build-outputs | broken_environment `har113-nop-001607` | sound `har115-rnop-001607-2028fb031bca` | `2028fb031bca` | validated |
| 001618 | /usr/local python 3.9 has pip 20.2.4's dist-info over a later pip's files (python -m pip fails on import) and the base commit's pip-tools 4.5.0 predates pip 20.1; reinstall pip 20.0.2 cleanly | pin | broken_environment `har113-nop-001618` | sound `har115-rnop-001618-6711d55bc4e5` | `6711d55bc4e5` | validated |
| 002017 | napari/_version.py | keep-build-outputs | broken_environment `har113-nop-002017` | sound `har115-rnop-002017-d58b321d3fe5` | `d58b321d3fe5` | validated |
| 002055 | the conftest needs the git-ignored built frontend/dist/static | keep-files | broken_environment `har113-nop-002055` | sound `har115-rnop-002055-fcf17343b2dd` | `fcf17343b2dd` | validated |
| 002191 | the base commit's werkzeug imports OpenSSL.tsafe, which /testbed/.venv's pyOpenSSL 26.2.0 no longer has; install the last pyOpenSSL with tsafe | pin | broken_environment `har113-nop-002191` | sound `har115-rnop-002191-016fcb1b54e2` | `016fcb1b54e2` | validated |
| 002196 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002196` | sound `har115-rnop-002196-940f6cdda133` | `940f6cdda133` | validated |
| 002197 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002197` | sound `har115-rnop-002197-5f52378d3ad0` | `5f52378d3ad0` | validated |
| 002199 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002199` | sound `har115-rnop-002199-a7a0de5e6746` | `a7a0de5e6746` | validated |
| 002202 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002202` | sound `har115-rnop-002202-c8cff14e78a4` | `c8cff14e78a4` | validated |
| 002206 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002206` | sound `har115-rnop-002206-dec48c46c29c` | `dec48c46c29c` | validated |
| 002208 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002208` | sound `har115-rnop-002208-b3b73a84cf09` | `b3b73a84cf09` | validated |
| 002211 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002211` | sound `har115-rnop-002211-977f6394b276` | `977f6394b276` | validated |
| 002212 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002212` | sound `har115-rnop-002212-73a5e4e83325` | `73a5e4e83325` | validated |
| 002214 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002214` | sound `har115-rnop-002214-6440947cd849` | `6440947cd849` | validated |
| 002215 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002215` | sound `har115-rnop-002215-ec291f0ca0d9` | `ec291f0ca0d9` | validated |
| 002216 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002216` | sound `har115-rnop-002216-a84d99c3541b` | `a84d99c3541b` | validated |
| 002220 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002220` | sound `har115-rnop-002220-227a9dd7bec6` | `227a9dd7bec6` | validated |
| 002221 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002221` | sound `har115-rnop-002221-661e1d7ac8fa` | `661e1d7ac8fa` | validated |
| 002224 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002224` | sound `har115-rnop-002224-5e42d8119c8d` | `5e42d8119c8d` | validated |
| 002229 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002229` | sound `har115-rnop-002229-2b4a9ee29a46` | `2b4a9ee29a46` | validated |
| 002230 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002230` | sound `har115-rnop-002230-2a625da07b0b` | `2a625da07b0b` | validated |
| 002231 | pandas._libs extensions | keep-build-outputs | broken_environment `har113-nop-002231` | sound `har115-rnop-002231-72355a73c510` | `72355a73c510` | validated |
| 002281 | gensim Cython extensions | keep-build-outputs | broken_environment `har113-nop-002281` | sound `har115-rnop-002281-14cb2299e4c9` | `14cb2299e4c9` | validated |
| 002307 | pre_commit package metadata | keep-build-outputs | broken_environment `har113-nop-002307` | sound `har115-rnop-002307-2232110e2cd8` | `2232110e2cd8` | validated |
| 002353 | _black_version.py | keep-build-outputs | broken_environment `har113-nop-002353` | sound `har115-rnop-002353-bb683e0a553f` | `bb683e0a553f` | validated |
| 002400 | pyproj._network extension | keep-build-outputs | broken_environment `har113-nop-002400` | sound `har115-rnop-002400-5aa6b7569d22` | `5aa6b7569d22` | validated |
| 002410 | pylsp/_version.py | keep-build-outputs | broken_environment `har113-nop-002410` | sound `har115-rnop-002410-52d60b01c0b0` | `52d60b01c0b0` | validated |
| 002451 | satpy/version.py | keep-build-outputs | broken_environment `har113-nop-002451` | sound `har115-rnop-002451-78f8541d0e9d` | `78f8541d0e9d` | validated |
| 002483 | rasterio extensions (rasterio.errors fails to import at pytest start) | keep-build-outputs | grader_suspect `har113-nop-002483` | sound `har115-rnop-002483-e74de2cac39e` | `e74de2cac39e` | validated |
| 002644 | sherpa.utils._utils extension | keep-build-outputs | broken_environment `har113-nop-002644` | sound `har115-rnop-002644-349be3539d16` | `349be3539d16` | validated |
| 002739 | statsmodels/_version.py | keep-build-outputs | broken_environment `har113-nop-002739` | sound `har115-rnop-002739-adde3189ea52` | `adde3189ea52` | validated |
| 002873 | tox/version.py | keep-build-outputs | broken_environment `har113-nop-002873` | sound `har115-rnop-002873-52c6ab8d6d04` | `52c6ab8d6d04` | validated |

Superseded attempt, `rejected` on its own nop: 001176's `7554f2f5ee8e`
(`env-login-path@2`; the interpreter was already right, `bzt` was not on its
path: `ModuleNotFoundError: No module named 'bzt'`), replaced by `52862dc0cdbb`.

002649: HAR-113's nop ended in `DaytonaConflictError`; `har115-nop-002649` is `sound`.

### Remaining of the 51

| task | label | evidence | why not repaired |
|---|---|---|---|
| 000124 | grader_suspect | `sqlalchemy.exc.OperationalError: no such table: user` in fixture setup | the fixtures expect tables that Alembic migrations would create (`init_db.py`: "Tables should be created with Alembic migrations"); no environment fix identified |
| 000393 | broken_environment | `Failed to pre-validate. {'driver': [{'name': ['unallowed value delegated']}]}` (`har115-diag2-000393`) | molecule's schema rejects the `delegated` driver the tests configure; installed plugins (molecule-docker 0.3.3, molecule-podman 0.3.0, ansible-base 2.10.8) vs the base commit [INFERENCE]; not attempted |
| 002848 | broken_environment | `No module named 'src.DownloadModels'` | the instruction names the class `DownloadModel`, not the module; likely the agent's own work, but the classifier rule does not reach it without the module name |
| 001146 | grader_suspect | `TypeError: get() got an unexpected keyword argument 'operation_id'` | tests run and fail on the missing feature; no count line |
| 002595 | broken_environment | nested pytest: `AttributeError: 'LazySchema' object has no attribute 'hooks'` | tests run; the object error names no module, so no rule excuses it; not an environment defect on the evidence |

## 2. Census completion

`specs.py` wrote one `har115-nop-<id>` per remaining `unknown` row, lightest
image first; `runner.py --prefix har115- --cap 1.95` ran repairs and diagnoses
first, then the 198 nops (all finished at $1.7753), and two later rounds ran
under the card's $2.25. `census_update.py` rebuilt the catalog's
`task_qualification.parquet` (1416 nop jobs: HAR-113's runs from its locked
worktree, HAR-115's from this checkout) and then `task_health.parquet`.

The 198 heaviest images added 33 tasks that are not sound. Under Peter's
2026-09-30 scope ("document them, either fix them or discard them... and move
on"; Research-Harbor's HAR-115 comment: fix only with a known repair kind, at
most one diagnosis per unknown error), the 21 whose error line names a known
kind got one repair nop each (below); the rest are discarded or in review in
the ledger with their census evidence as the reason. The last column was the
guess before the repair nops:

| task | label | project | split | evidence | likely repair [INFERENCE] |
|---|---|---|---|---|---|
| 000022 | broken_environment | format-code-task-000022 | train | `ConftestImportFailure: ModuleNotFoundError: No module named 'ape.version' (from /testbed/tests/conft` | keep-build-outputs |
| 000023 | broken_environment | ape | train | `/testbed/mimo_test_command.sh: line 5: pytest: command not found` | login-path |
| 000183 | grader_suspect | parcels | train | `no test results; last output: /testbed/mimo_test_command.sh: line 8: 457 Segmentation fault (core du` | diagnose |
| 000238 | broken_environment | Lib | train | `/testbed/.build_env/test_command.sh: line 4: cargo: command not found` | login-path |
| 000392 | broken_environment | github.com/openstack/ansible-collections-openstack | train | `raise DistributionNotFound(req, requirers)` | keep-build-outputs |
| 000469 | broken_environment | asreview | train | `____________________ ERROR collecting tests/test_metrics.py ____________________` | diagnose |
| 000614 | broken_environment | camelot | train | `trial exception: SandboxBuildFailedError` | backend/setup |
| 000737 | broken_environment | format-code-task-000737 | train | `raise DistributionNotFound(req, requirers)` | keep-build-outputs |
| 000966 | broken_environment | caching | train | `/testbed/mimo_test_command.sh: line 5: py.test: command not found` | login-path |
| 000974 | broken_environment | sunpy | train | `ModuleNotFoundError: No module named 'setuptools_scm'` | diagnose |
| 000994 | broken_environment | socorro | train | `/testbed/mimo_test_command.sh: line 5: nosetests: command not found` | login-path |
| 001073 | broken_environment | pandas | train | `/usr/bin/python: No module named pytest` | keep-build-outputs |
| 001078 | broken_environment | doctr | train | `/testbed/mimo_test_command.sh: line 5: pytest: command not found` | login-path |
| 001079 | broken_environment | ape | train | `INTERNALERROR> ModuleNotFoundError: No module named 'ape.version'` | keep-build-outputs |
| 001080 | broken_environment | gammapy | train | `/testbed/mimo_test_command.sh: line 4: pytest: command not found` | login-path |
| 001081 | broken_environment | docscrape | train | `/testbed/mimo_test_command.sh: line 5: python2: command not found` | login-path |
| 001092 | broken_environment | electricitymap | train | `/usr/bin/python: No module named pytest` | login-path |
| 001095 | broken_environment | girder | train | `ModuleNotFoundError: No module named 'cherrypy'` | diagnose |
| 001124 | broken_environment | socorro | heldout | `/testbed/mimo_test_command.sh: line 5: nosetests: command not found` | login-path |
| 001125 | broken_environment | scrapy | train | `_____ ERROR collecting tests/test_downloadermiddleware_httpcompression.py ______` | diagnose |
| 001138 | broken_environment | arches | train | `ModuleNotFoundError: No module named 'django'` | diagnose |
| 001160 | broken_environment | opentrons | train | `/testbed/mimo_test_command.sh: line 6: pytest: command not found` | login-path |
| 001163 | broken_environment | ezdxf | train | `ModuleNotFoundError: No module named 'pytest'` | diagnose |
| 001168 | broken_environment | mlflow | train | `ImportError while loading conftest '/testbed/tests/conftest.py'.` | diagnose |
| 001174 | broken_environment | xblock | train | `_________________ ERROR collecting xblock/test/test_runtime.py _________________` | diagnose |
| 001226 | broken_environment | cortex | train | `running build_ext` | keep-build-outputs |
| 001461 | broken_environment | bleak | train | `trial exception: SandboxBuildFailedError` | backend/setup |
| 001834 | broken_environment | arcticdb | heldout | `ImportError while loading conftest '/testbed/python/tests/conftest.py'.` | diagnose |
| 001853 | broken_environment | marimo | train | `______________ ERROR collecting tests/_utils/test_file_watcher.py ______________` | diagnose |
| 002016 | broken_environment | github.com/napari/napari | heldout | `ModuleNotFoundError: No module named 'napari._version'` | keep-build-outputs |
| 002127 | broken_environment | opentelemetry | train | `trial exception: SandboxBuildFailedError` | backend/setup |
| 003009 | broken_environment | pyte | train | `________ ERROR collecting usercase-test-coderl/test_diffscreen_dirty.py ________` | diagnose |
| 003014 | broken_environment | format-code-task-003014 | train | `_________________ ERROR collecting tests/dialects/test_smt.py __________________` | diagnose |

Round four, the repair nops (`specs.py` `KEEP_BUILD_4`, `LOGIN_PATH_4`; a
rejected repair is not retried):

| task | cause | repair | nop after | variant | status |
|---|---|---|---|---|---|
| 000022 | ape.version, a generated version module | keep-build-outputs | sound `har115-rnop-000022-57763bd07c79` | `57763bd07c79` | validated |
| 000023 | pytest: command not found | login-path-pyenv | sound `har115-rnop-000023-1d9811ed4138` | `1d9811ed4138` | validated |
| 000238 | cargo: command not found | login-path-pyenv | broken_environment `har115-rnop-000238-a3485fb06ded`: `/testbed/.build_env/test_command.sh: line 4: cargo: command not found` | `a3485fb06ded` | rejected |
| 000392 | package metadata (DistributionNotFound) | keep-build-outputs | sound `har115-rnop-000392-4193bdb781fb` | `4193bdb781fb` | validated |
| 000737 | package metadata (DistributionNotFound) | keep-build-outputs | sound `har115-rnop-000737-d9096c3e229d` | `d9096c3e229d` | validated |
| 000966 | py.test: command not found | login-path-pyenv | sound `har115-rnop-000966-918b471e2cb1` | `918b471e2cb1` | validated |
| 000974 | setuptools_scm missing for the non-login python | login-path-pyenv | broken_environment `har115-rnop-000974-3db9afa4324a`: `ModuleNotFoundError: No module named 'setuptools_scm'` | `3db9afa4324a` | rejected |
| 000994 | nosetests: command not found | login-path-pyenv | sound `har115-rnop-000994-e22775f3015e` | `e22775f3015e` | validated |
| 001073 | /usr/bin/python has no pytest | login-path-pyenv | broken_environment `har115-rnop-001073-39c2a76f357c`: `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | `39c2a76f357c` | rejected |
| 001078 | pytest: command not found | login-path-pyenv | broken_environment `har115-rnop-001078-cd85a596321e`: `_______ ERROR collecting tests/common/test_dataloader_backend_neutral.py _______` | `cd85a596321e` | rejected |
| 001079 | ape.version, a generated version module | keep-build-outputs | sound `har115-rnop-001079-90f59eb79181` | `90f59eb79181` | validated |
| 001080 | pytest: command not found | login-path-pyenv | sound `har115-rnop-001080-6ca9c62cdf6a` | `6ca9c62cdf6a` | validated |
| 001081 | python2: command not found | login-path-pyenv | sound `har115-rnop-001081-bcb1d8b56a9e` | `bcb1d8b56a9e` | validated |
| 001092 | /usr/bin/python has no pytest | login-path-pyenv | sound `har115-rnop-001092-8d49432685d0` | `8d49432685d0` | validated |
| 001095 | cherrypy missing for the non-login python | login-path-pyenv | sound `har115-rnop-001095-eb2334e4dedd` | `eb2334e4dedd` | validated |
| 001124 | nosetests: command not found | login-path-pyenv | sound `har115-rnop-001124-00d10ce4d002` | `00d10ce4d002` | validated |
| 001138 | django missing for the non-login python | login-path-pyenv | sound `har115-rnop-001138-493f544b83ce` | `493f544b83ce` | validated |
| 001160 | pytest: command not found | login-path-pyenv | broken_environment `har115-rnop-001160-d187122ce9ea`: `ConftestImportFailure: ModuleNotFoundError: No module named 'opentrons_shared_data' (from ` | `d187122ce9ea` | rejected |
| 001163 | pytest missing for the non-login python | login-path-pyenv | sound `har115-rnop-001163-ea51dfa22d93` | `ea51dfa22d93` | validated |
| 001226 | a compiled extension (the nop re-runs build_ext) | keep-build-outputs | broken_environment `har115-rnop-001226-a1eb01868dce`: `running build_ext` | `a1eb01868dce` | rejected |
| 002016 | napari._version, a generated version module | keep-build-outputs | sound `har115-rnop-002016-8bbbe0974e77` | `8bbbe0974e77` | validated |

## 3. Classifier false positives

`evallab.task_health` excuses a nop failure that is only the agent's missing
work. Its gaps, now closed (`tests/test_task_health.py`, `docs/mimo-task-catalog.md`):

| task | decisive line | was | rule |
|---|---|---|---|
| 000211 | `FileNotFoundError: ... '/testbed/posthog/.../source/config.py'` | grader_suspect | a missing `.py` file whose path the instruction names |
| 001981 | `ImportError: cannot import name STAGE_JOBS` | broken_environment | quotes around the name are optional |
| 002207 | `ModuleNotFoundError: import of lzma halted; None in sys.modules` | broken_environment | a stdlib import the tests halt on purpose, named by the instruction |
| 001860 | `cannot import name 'RoomGuestAccessEvent' from 'nio.events' (/testbed/nio/events.py)` | broken_environment | a name the hidden patch adds, missing from a workspace module the instruction names |
| 002307 repair nop | `<module 'pre_commit.languages.docker' ...> does not have the attribute '_get_container_id'` | broken_environment | the same, for `mock.patch` on a missing attribute |

Regression check: the classifier re-labelled every reachable census nop (979);
only the four census rows above changed. Real breakage (`pandas._libs.tslib`,
`statsmodels._version`, `PackageNotFoundError`, `No module named 'django'`,
`pytest: command not found`) stays broken.

## Spend

$1.9807 Daytona over 274 trials (`qualify-collect` estimate): census nops
$1.6702 (199, including the 002649 re-nop), repair nops $0.2860 (66),
diagnoses $0.0245 (9). No model calls.

## Reproduce

From the worktree root:

```bash
D=research/experiments/har115-census
uv run python $D/specs.py > /tmp/order.txt          # variants, repairs.json, specs/
uv run python research/experiments/har113-variants/runner.py $(cat /tmp/order.txt) \
  --prefix har115- --cap 2.25 --parallel 10 --wave 40
uv run python $D/outcomes.py --verdicts               # results.json + record verdicts
uv run python research/experiments/har113-variants/census_update.py
```
