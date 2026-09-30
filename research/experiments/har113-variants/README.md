# HAR-113: leak-closed and repaired Python census tasks

Part of HAR-113. Fixes for tasks in HAR-108's Python census
([`../har108-python-census/`](../har108-python-census/)) as task variants
([`docs/task-variants.md`](../../../docs/task-variants.md)): the pinned snapshot is
never edited. Each variant's record is committed under
`library/task-variants/<task_slug>/<digest12>.json` (lineage, changed files,
rationale, nop evidence and status); its package lives in the shared
`derived/task-store/variants/` store and is reproducible from the parent plus
the record (`materialize`).

| Part | Result |
|---|---|
| PyPI leak (`pypi_fix_released`) | 247 of 247 leak-closed (`leak-close-pypi@1`); 0 left open (none needs PyPI at test time) |
| Leak proof | `pip download <project>==<fixed release>`: exit 0 on 5 of 5 parents, exit 1 on 14 of 14 variants |
| Leak nop sample | 14 tasks, 14 different projects: 13 sound before and after (`validated`), 1 broken before and after (001618, parent broken) |
| Broken-environment repairs | 51 of the 61 census tasks labelled `broken_environment`/`grader_suspect` at card start: broken before, `sound` after (`validated`) |
| Not fixed | 10: 3 are legitimate fail-to-pass nops the census mislabels, 7 have no environment-only fix here (table below) |
| Unknown census waves | 428 of 626 `unknown` tasks nopped, lightest images first: 377 sound, 48 broken, 3 grader_suspect; 198 left at the spend cap |
| Spend | $2.4389 Daytona over 552 trials (cap $2.50); no model calls |

The census after this card (`task_health.parquet`, [`SUMMARY.md`](../har108-python-census/SUMMARY.md)):
sound 870, broken_environment 107, grader_suspect 5, unknown 198 (was 493 / 59 / 2 / 626).
The census labels original tasks only: a variant nop carries the variant's digest,
which is not in the pool, so a repaired task still reads `broken_environment`
there and its admissible version is the `validated` variant below.

## 1. PyPI leak

**Count.** The card says 127 `pypi_fix_released` tasks; the census has **247**.
127 is the `unknown`-label subset. By label: unknown 127, sound 97,
broken_environment 23; by split: train 224, held-out 23. All 247 are variants.

**Change** (`leak_variants.py`, transform `leak-close-pypi@1`). The task's
answer-leak blocklist (`environment/setup/files/blocklist`) gains `pypi.org`,
`files.pythonhosted.org` and `pypi.python.org`, and `environment/setup` is
re-embedded in `task.toml`'s healthcheck payload, which is what actually runs.
Nothing else changes. `verify()` passes for all 247 and every embedded payload
contains the new hosts.

**Left unfixed: none.** A task would stay open if grading needed PyPI. Setup
cannot: it runs in the healthcheck, before the harness applies the blocklist.
The verifier runs after, so `leak_variants.py` checks what it runs: the test
command must not install or fetch packages, and the hidden tests must not name
a PyPI host or install anything. Three matches were reviewed by hand and are
harmless (all three are in the nop sample and grade the same with the
blocklist applied): 001618 (pip-tools builds a `PyPIRepository` with the
default index URL; constructing it makes no request), 002388 (PyPI file URLs
are expected strings in a mocked mirror fixture), 002427 (asserts on the text
of mypy's `--install-types` hint).

**Proof** (`leak_specs.py`, `probe_solve.sh`). Harbor's oracle agent runs
`probe_solve.sh` on a copy of the package (under the gitignored
`derived/har113-probes/`, a different digest, so it never enters the census):
it appends the task blocklist to `/etc/hosts` exactly as
`harbor_terminus.apply_mimo_blocklist` does, then runs
`pip download --no-deps <project>==<fixed release>`, fetches
`https://pypi.org/pypi/<project>/<release>/json` and opens an IPv6 connection
to pypi.org and files.pythonhosted.org. The verifier then grades the
untouched repo with the blocklist in place.

- 000226 parent: pip exit 0, downloaded `waitress-1.3.1-py2.py3-none-any.whl`,
  pypi.org JSON HTTP 200. Variant: pip exit 1 (`[Errno 111] Connection refused`);
  IPv6 `Network is unreachable`: Daytona sandboxes have no IPv6 route, so an
  AAAA answer cannot bypass the `0.0.0.0` entries.
- 000927 (soupsieve 1.9), 002256, 002864, 002407 parents: pip exit 0. All 14
  sample variants: pip exit 1. Graded with the blocklist, the 13 sound variants
  stay sound.

**Nop sample** (`har113-vnop-*`: the nop agent never applies the blocklist, so
this checks the re-embedded setup; the probe above checks the blocklist):

| task | project | fixed release | parent `pip download` | variant `pip download` | nop before | nop after | variant | status |
|---|---|---|---|---|---|---|---|---|
| 000226 | waitress | 1.3.1 | exit 0 | exit 1 | sound `har105-qual-py-000226` | sound `har113-vnop-000226-9666803bf548` | `9666803bf548` | validated |
| 000803 | mdutils | 1.3.0 | — | exit 1 | sound `har108-nop-000803` | sound `har113-vnop-000803-ac99a511015a` | `ac99a511015a` | validated |
| 000927 | soupsieve | 1.9 | exit 0 | exit 1 | sound `har105-qual-py-000927` | sound `har113-vnop-000927-0b8d70b58210` | `0b8d70b58210` | validated |
| 001265 | pelican | 4.5.0 | — | exit 1 | sound `har108-nop-001265` | sound `har113-vnop-001265-3cdbc249d78b` | `3cdbc249d78b` | validated |
| 001618 | pip-tools | 4.5.0 | — | exit 1 | broken_environment `har113-nop-001618` | broken_environment `har113-vnop-001618-1b8e64964d4e` | `1b8e64964d4e` | candidate |
| 001647 | django-environ | 0.4.5 | — | exit 1 | sound `har108-nop-001647` | sound `har113-vnop-001647-e435a9443935` | `e435a9443935` | validated |
| 002014 | napalm | 3.0.1 | — | exit 1 | sound `har108-nop-002014` | sound `har113-vnop-002014-2e97492456af` | `2e97492456af` | validated |
| 002256 | persist-queue | 1.1.0b0 | exit 0 | exit 1 | sound `har105-qual-py-002256` | sound `har113-vnop-002256-8f0e6de1d93b` | `8f0e6de1d93b` | validated |
| 002308 | pre-commit | 2.15.0 | — | exit 1 | sound `har108-nop-002308` | sound `har113-vnop-002308-2d2501dcc8c2` | `2d2501dcc8c2` | validated |
| 002388 | bandersnatch | 6.4.0 | — | exit 1 | sound `har108-nop-002388` | sound `har113-vnop-002388-fd4ee569a141` | `fd4ee569a141` | validated |
| 002407 | control | 0.9.4 | exit 0 | exit 1 | sound `har105-qual-py-002407` | sound `har113-vnop-002407-b6c045c41987` | `b6c045c41987` | validated |
| 002427 | mypy | 0.900 | — | exit 1 | sound `har108-nop-002427` | sound `har113-vnop-002427-77d3f78ec4ec` | `77d3f78ec4ec` | validated |
| 002864 | sqlglot | 26.4.0 | exit 0 | exit 1 | sound `har105-qual-py-002864` | sound `har113-vnop-002864-25b94c8b4e4e` | `25b94c8b4e4e` | validated |
| 002974 | vyper | 0.1.0b7 | — | exit 1 | sound `har108-nop-002974` | sound `har113-vnop-002974-617c20ca349a` | `617c20ca349a` | validated |

The other 233 leak variants stay `candidate`: the same one-file change, not
individually nopped.

**Limits.** All 1180 census tasks run the agent as root
(`leak_hosts_bypassable`): a root agent can rewrite `/etc/hosts` and undo any
blocklist. The blocklist only takes effect under a harness that applies it
(`apply_mimo_blocklist`); setup keeps its network by design, which the repairs
below rely on.

## 2. Environment repairs

Repairs change only `environment/setup/setup.sh` (re-embedded in the
healthcheck payload) and derive from the task's leak variant when it has one,
so a repaired package keeps PyPI blocked (`repair_variants.py`,
`repair_variants.json`). Root causes came from the nop logs, the
`BrokenClusterRootCause` survey and oracle diagnoses (`diag_specs.py`,
`diag_solve.sh`, `har113-diag*-*`):

| Kind (transform) | Cause | Tasks validated |
|---|---|---:|
| keep-build-outputs (`env-keep-build-outputs@1`) | setup's `git clean -fdx` deletes git-ignored build outputs the image left in the repo: compiled extensions (pandas `_libs`, h5py, pyproj, …), `*.egg-info`, generated `version.py`, `./configure` output. Adds `--exclude` patterns for them | 32 |
| keep-files (`env-keep-files@1`) | the same for one project's generated files (sourmash's `_lowlevel*.py`, stacked on keep-build-outputs; burnman's git-ignored `burnman/data/*`) | 2 |
| login-path (`env-login-path@1`) | the grader runs the test command in a non-login `sh -c` whose PATH misses the interpreter the image puts on the login shell's PATH (`/testbed/.venv`, pyenv, `/opt/*-venv`); setup writes `/usr/local/bin` exec wrappers | 12 |
| login-path-pyenv (`env-login-path@2`) | as @1, resolving a pyenv shim with the login shell's `pyenv which` (000984: under @1 the shim picked pyenv's global `system` version, found the wrapper again and exec'd in a loop until the 1800 s timeout) | 1 |
| pin (`env-pin-dependency@1`) | setup installs what the image has wrong: a clean setuptools over a mixed install (000655, 002496), `Cython==3.0.12` for numpy's Cython test build (002078), twisted's own distribution metadata via `setup.py egg_info` (002893: pytest's unittest plugin reads it; the C test extension does not build on Python 3.11) | 4 |

Every repair, with its nop before (the census nop) and after:

| task | cause | repair | nop before | nop after | variant | status |
|---|---|---|---|---|---|---|
| 000080 | config.vars from ./configure | keep-build-outputs | broken_environment `har108-nop-000080` | sound `har113-rnop-000080-70cf87693117` | `70cf87693117` | validated |
| 000225 | linopy/version.py | keep-build-outputs | broken_environment `har108-nop-000225` | sound `har113-rnop-000225-cbecf3e09a70` | `cbecf3e09a70` | validated |
| 000377 | wordcloud/_version.py | keep-build-outputs | broken_environment `har108-nop-000377` | sound `har113-rnop-000377-d287a33499bc` | `d287a33499bc` | validated |
| 000437 | coremltools libmilstoragepython; extensions built for the login shell's pyenv 3.10 | keep-build-outputs + login-path | broken_environment `har108-nop-000437` | grader_suspect `har113-rnop-000437-d98eb7de428f` | `d98eb7de428f` | candidate |
| 000527 | pytest in /testbed/.venv | login-path | broken_environment `har108-nop-000527` | sound `har113-rnop-000527-2416eb70254e` | `2416eb70254e` | validated |
| 000651 | chainer.egg-info | keep-build-outputs | broken_environment `har108-nop-000651` | sound `har113-rnop-000651-0763948829e0` | `0763948829e0` | validated |
| 000652 | chainer.egg-info | keep-build-outputs | broken_environment `har108-nop-000652` | sound `har113-rnop-000652-11343f2dd28f` | `11343f2dd28f` | validated |
| 000655 | setuptools 59.8.0 dist-info over a newer release's files (more_itertools import) | pin | broken_environment `har108-nop-000655` | sound `har113-rnop-000655-e39d00fb1e4f` | `e39d00fb1e4f` | validated |
| 000817 | DGL build/libdgl.so | keep-build-outputs | broken_environment `har108-nop-000817` | sound `har113-rnop-000817-e7490f8dcfc6` | `e7490f8dcfc6` | validated |
| 000960 | pytest in /opt/torax-venv | login-path | broken_environment `har108-nop-000960` | sound `har113-rnop-000960-fdcf7e3f176e` | `fdcf7e3f176e` | validated |
| 000972 | django in a pyenv version | login-path | broken_environment `har108-nop-000972` | sound `har113-rnop-000972-84adb64365eb` | `84adb64365eb` | validated |
| 000973 | pulp in a pyenv version | login-path | broken_environment `har108-nop-000973` | sound `har113-rnop-000973-a120ded537d3` | `a120ded537d3` | validated |
| 000984 | pytest in the pyenv-virtualenv socorro-env, which only the login shell activates | login-path-pyenv | broken_environment `har108-nop-000984` | sound `har113-rnop-000984-3fd2405b548f` | `3fd2405b548f` | validated |
| 001018 | pyenv 3.7 has the base code's asyncio.coroutine | login-path | broken_environment `har108-nop-001018` | sound `har113-rnop-001018-bd1445e23457` | `bd1445e23457` | validated |
| 001019 | pyenv 3.9 has the base code's collections.MutableMapping | login-path | broken_environment `har108-nop-001019` | sound `har113-rnop-001019-21853f4b8a2f` | `21853f4b8a2f` | validated |
| 001020 | pytest in /testbed/.venv | login-path | broken_environment `har108-nop-001020` | sound `har113-rnop-001020-cbfbab48351b` | `cbfbab48351b` | validated |
| 001064 | tox/version.py | keep-build-outputs | broken_environment `har108-nop-001064` | sound `har113-rnop-001064-6d92670436eb` | `6d92670436eb` | validated |
| 001072 | pytest in /testbed/.venv | login-path | broken_environment `har108-nop-001072` | sound `har113-rnop-001072-4058abd010c4` | `4058abd010c4` | validated |
| 001087 | deepchecks package metadata | keep-build-outputs | broken_environment `har108-nop-001087` | sound `har113-rnop-001087-8149fa52561c` | `8149fa52561c` | validated |
| 001103 | pytest in a pyenv version | login-path | broken_environment `har108-nop-001103` | sound `har113-rnop-001103-7eaecbd8e2b1` | `7eaecbd8e2b1` | validated |
| 001139 | pytest in /testbed/.venv | login-path | broken_environment `har108-nop-001139` | sound `har113-rnop-001139-865a28902e41` | `865a28902e41` | validated |
| 001179 | pytest in /testbed/.venv | login-path | broken_environment `har108-nop-001179` | sound `har113-rnop-001179-27f6937f3371` | `27f6937f3371` | validated |
| 001246 | burnman's data files, git-ignored by /burnman/data/*, are not in git | keep-files | broken_environment `har108-nop-001246` | sound `har113-rnop-001246-91dae7160069` | `91dae7160069` | validated |
| 001422 | h5py extensions | keep-build-outputs | broken_environment `har108-nop-001422` | sound `har113-rnop-001422-03762dc0058a` | `03762dc0058a` | validated |
| 001561 | /root/.venv was built for a later indico (webargs 7.0.0b1, no bleach); install the base commit's pinned requirements (celery 5.0.2's metadata needs pip<24.1) | pin | broken_environment `har108-nop-001561` | broken_environment `har113-rnop-001561-faaa18c27d98` | `faaa18c27d98` | rejected |
| 001590 | isso.egg-info | keep-build-outputs | broken_environment `har108-nop-001590` | sound `har113-rnop-001590-18d62f5b781c` | `18d62f5b781c` | validated |
| 001813 | locust/_version.py | keep-build-outputs | broken_environment `har108-nop-001813` | sound `har113-rnop-001813-743653a7c327` | `743653a7c327` | validated |
| 001927 | doctr/version.py | keep-build-outputs | broken_environment `har108-nop-001927` | sound `har113-rnop-001927-fc586d627959` | `fc586d627959` | validated |
| 002032 | pytest in pyenv 3.8 | login-path | broken_environment `har108-nop-002032` | sound `har113-rnop-002032-0d162b2989fb` | `0d162b2989fb` | validated |
| 002078 | /opt/numpy-venv Cython 3.2.5 fails numpy's test_cython build; pin Cython 3.0 | pin | broken_environment `har108-nop-002078` | sound `har113-rnop-002078-425d3bc13b84` | `425d3bc13b84` | validated |
| 002172 | drgn internal version module | keep-build-outputs | broken_environment `har108-nop-002172` | sound `har113-rnop-002172-2fc06276b8e7` | `2fc06276b8e7` | validated |
| 002195 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002195` | sound `har113-rnop-002195-ec31b6235430` | `ec31b6235430` | validated |
| 002198 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002198` | sound `har113-rnop-002198-d2b32392c830` | `d2b32392c830` | validated |
| 002201 | pandas._libs extensions | keep-build-outputs | broken_environment `mimo-qual-c-format-code-task-002201` | sound `har113-rnop-002201-f516830bb470` | `f516830bb470` | validated |
| 002204 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002204` | sound `har113-rnop-002204-4556e13cde6d` | `4556e13cde6d` | validated |
| 002205 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002205` | sound `har113-rnop-002205-e61a7fd5c4e6` | `e61a7fd5c4e6` | validated |
| 002209 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002209` | sound `har113-rnop-002209-33aebc8c1059` | `33aebc8c1059` | validated |
| 002210 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002210` | sound `har113-rnop-002210-362f105e4b2e` | `362f105e4b2e` | validated |
| 002218 | pandas._libs extensions | keep-build-outputs | broken_environment `har105-qual-py-002218` | sound `har113-rnop-002218-8efe1f83348c` | `8efe1f83348c` | validated |
| 002219 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002219` | sound `har113-rnop-002219-20a023bee773` | `20a023bee773` | validated |
| 002222 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002222` | sound `har113-rnop-002222-ee7fbb400ead` | `ee7fbb400ead` | validated |
| 002223 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002223` | sound `har113-rnop-002223-60857d336231` | `60857d336231` | validated |
| 002226 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002226` | sound `har113-rnop-002226-a5981d6b8553` | `a5981d6b8553` | validated |
| 002228 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002228` | sound `har113-rnop-002228-1a1ce521fa3b` | `1a1ce521fa3b` | validated |
| 002233 | pandas._libs extensions | keep-build-outputs | broken_environment `har108-nop-002233` | sound `har113-rnop-002233-f5d31583f98a` | `f5d31583f98a` | validated |
| 002248 | fastTSNE._tsne | keep-build-outputs | broken_environment `har108-nop-002248` | sound `har113-rnop-002248-cd99435b755a` | `cd99435b755a` | validated |
| 002354 | _black_version.py | keep-build-outputs | broken_environment `har108-nop-002354` | sound `har113-rnop-002354-66c415a09f96` | `66c415a09f96` | validated |
| 002401 | pyproj._context | keep-build-outputs | broken_environment `har105-qual-py-002401` | sound `har113-rnop-002401-3311b8e2c62e` | `3311b8e2c62e` | validated |
| 002496 | setuptools 57.5.0 dist-info over a newer release's files (FileError import) | pin | broken_environment `har108-nop-002496` | sound `har113-rnop-002496-1e53514f3dbe` | `1e53514f3dbe` | validated |
| 002601 | skbio.metadata._intersection | keep-build-outputs | broken_environment `har108-nop-002601` | sound `har113-rnop-002601-a8f0fe0b67f8` | `a8f0fe0b67f8` | validated |
| 002696 | sourmash._lowlevel; sourmash's milksnake modules beside _lowlevel__lib.so | keep-build-outputs + keep-files | broken_environment `har108-nop-002696` | sound `har113-rnop-002696-cb5be55d4c36` | `cb5be55d4c36` | validated |
| 002872 | tox/version.py | keep-build-outputs | broken_environment `har108-nop-002872` | sound `har113-rnop-002872-2a0f904494b0` | `2a0f904494b0` | validated |
| 002893 | twisted 16.3.0 runs from source with no distribution metadata, which pytest's unittest plugin reads; its C test extension does not build on Python 3.11, so write only the metadata | pin | broken_environment `har108-nop-002893` | sound `har113-rnop-002893-d12955c21a17` | `d12955c21a17` | validated |

Superseded attempts, `rejected` with their own nop as evidence (a verdict is
final, so a retry is a new variant from the same parent):

| task | superseded variant | its nop | replaced by |
|---|---|---|---|
| 000437 | `afb488483536` (env-keep-build-outputs@1, rejected) | broken_environment `har113-rnop-000437-afb488483536`: ImportError while loading conftest '/testbed/coremltools/converters/mil/conftest | `d98eb7de428f` |
| 000984 | `7d130a0ced7a` (env-login-path@1, rejected) | grader_suspect `har113-rnop-000984-7d130a0ced7a`: no test output | `3fd2405b548f` |
| 001561 | `a82d9b7e0d70` (env-pin-dependency@1, rejected) | broken_environment `har113-rnop-001561-a82d9b7e0d70`: ModuleNotFoundError: No module named 'bleach' | `faaa18c27d98` |
| 002696 | `5578cb237819` (env-keep-build-outputs@1, rejected) | broken_environment `har113-rnop-002696-5578cb237819`: _________________ ERROR collecting tests/test_cmd_signature.py _________________ | `cb5be55d4c36` |
| 002893 | `351be1d5b705` (env-pin-dependency@1, rejected) | broken_environment `har113-rnop-002893-351be1d5b705`: trial exception: HealthcheckError | `d12955c21a17` |

## 3. Not fixed

| task | census label | reason |
|---|---|---|
| 000211 | grader_suspect | legitimate fail-to-pass: the nop fails because the module the instruction asks for (`posthog/.../source/config.py`) does not exist yet. Census false positive |
| 001981 | broken_environment | legitimate fail-to-pass: `STAGE_JOBS` in `socorro.cron.crontabber_app` is the symbol the agent must add. Census false positive |
| 002207 | broken_environment | legitimate fail-to-pass: the hidden tests hide `lzma` and specify an lzma-tolerant import. Census false positive |
| 000437 | broken_environment | stacked keep-build-outputs + login-path (`d98eb7de428f`, left `candidate`): the tests now run and fail on the missing behaviour (progress `FFF.FFFFFFF`), but pytest prints no final count line, so the census rule reads `grader_suspect` |
| 000613, 002142 | broken_environment | the pinned image fails to build on Daytona (`SandboxBuildFailedError`); needs a repushed image |
| 000738 | broken_environment | needs a CUDA GPU (CuPy); no CPU environment passes |
| 001150 | grader_suspect | the base source is Python 2 (`print` statements); the image's Python is 3.14 |
| 002373 | broken_environment | the hidden test uses `List` without importing it (`NameError` at collection): a test fix, not an environment fix |
| 001561 | broken_environment | `/root/.venv` was built for a later indico (webargs 7.0.0b1, no bleach). Pinning webargs 5.5.3 moved the error to `bleach`; the base commit's `requirements.txt` does not install on the image's Python 3.11 (celery 5.0.2's metadata needs pip<24.1, then lxml 4.6.1 builds from source without libxml2/libxslt headers). Both pins `rejected` |

## 4. Unknown census waves

`unknown_specs.py` writes one nop spec per `unknown` task, lightest image
first; `runner.py` ran them in waves of 40 under the spend cap, re-measuring
actual spend (`qualify-collect`) before each wave and sizing it at an
upper-tail $0.012 per trial. It stopped at $2.4389 with 428 of 626 run.
`census_update.py` rebuilt the catalog's `task_qualification.parquet` (1172
nop jobs: every job HAR-108 used, with HAR-104's shelved `mimo-ops` runs read
from the wt-prune archive, plus this card's `har113-nop-*`, `-vnop-*` and
`-rnop-*`) and then `task_health.parquet` and `SUMMARY.md`. No label from
HAR-108 changed.

Still `unknown`: 198 (train 179, held-out 19): 185 with images of 3.1 GiB or
more, 13 with no image size in the pool.

The waves found 51 more tasks that are not `sound`. None has a variant yet
(no budget left to nop one); the repair column is a guess from the evidence:

| task | label | project | split | evidence | likely repair [INFERENCE] |
|---|---|---|---|---|---|
| 002649 | broken_environment | format-code-task-002649 | train | `trial exception: DaytonaConflictError` | backend conflict: re-nop |
| 000124 | grader_suspect | app | train | `no test results; last output: ======================== 7 warnings, 11 errors in 2.01s ====` | diagnose |
| 000159 | broken_environment | format-code-task-000159 | train | `___________________ ERROR collecting tests/test_simplify.py ____________________` | diagnose |
| 000160 | broken_environment | format-code-task-000160 | train | `___________________ ERROR collecting tests/test_simplify.py ____________________` | diagnose |
| 000161 | broken_environment | format-code-task-000161 | train | `___________________ ERROR collecting tests/test_simplify.py ____________________` | diagnose |
| 000190 | broken_environment | pydp | train | `__ ERROR collecting tests/algorithms/test_laplacian_batch_stream_contract.py ___` | diagnose |
| 000393 | broken_environment | format-code-task-000393 | train | `_______ ERROR at setup of test_default_env_double_nested_does_not_raise ________` | diagnose |
| 000968 | broken_environment | f5 | train | `______ ERROR collecting f5/bigip/test/test_REST_interface_collections.py _______` | diagnose |
| 001024 | broken_environment | supervisor | train | `______________ ERROR collecting supervisor/tests/test_process.py _______________` | diagnose |
| 001058 | broken_environment | delfin | train | `ModuleNotFoundError: No module named 'oslo_config'` | diagnose |
| 001068 | broken_environment | openduty | train | `ModuleNotFoundError: No module named 'django'` | diagnose |
| 001146 | grader_suspect | faststream | train | `no test results; last output: !!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!` | diagnose |
| 001176 | broken_environment | bzt | train | `ModuleNotFoundError: No module named 'bzt'` | diagnose |
| 001607 | broken_environment | github.com/pallets/click | train | `ImportError while loading conftest '/testbed/tests/conftest.py'.` | diagnose |
| 001618 | broken_environment | github.com/jazzband/pip-tools | train | `ImportError while loading conftest '/testbed/tests/conftest.py'.` | diagnose |
| 001860 | broken_environment | format-code-task-001860 | train | `_____________________ ERROR collecting tests/event_test.py _____________________` | diagnose |
| 002055 | broken_environment | httpx | train | `ImportError while loading conftest '/testbed/tests/conftest.py'.` | diagnose |
| 002191 | broken_environment | format-code-task-002191 | train | `ImportError: cannot import name 'tsafe' from 'OpenSSL' (/testbed/.venv/lib/python3.11/site` | diagnose |
| 002307 | broken_environment | github.com/pre-commit/pre-commit | train | `ImportError while loading conftest '/testbed/tests/conftest.py'.` | diagnose |
| 002353 | broken_environment | format-code-task-002353 | train | `_____________________ ERROR collecting tests/test_black.py _____________________` | diagnose |
| 002400 | broken_environment | github.com/geopandas/geopandas | train | `ImportError while loading conftest '/testbed/test/conftest.py'.` | diagnose |
| 002410 | broken_environment | github.com/python-lsp/python-lsp-server | heldout | `ImportError while loading conftest '/testbed/test/conftest.py'.` | diagnose |
| 002451 | broken_environment | github.com/pytroll/satpy | train | `ImportError while loading conftest '/testbed/satpy/conftest.py'.` | diagnose |
| 002483 | grader_suspect | github.com/mapbox/rasterio | train | `no test results; last output: warnings._OptionError: invalid module name: 'rasterio.errors` | diagnose |
| 002595 | broken_environment | schemathesis | train | `____________ ERROR collecting test_hooks_apply_per_test_function.py ____________` | diagnose |
| 002644 | broken_environment | sherpa | train | `ImportError while loading conftest '/testbed/sherpa/conftest.py'.` | diagnose |
| 002848 | broken_environment | github.com/TNTwise/real-video-enhancer-models | train | `E ModuleNotFoundError: No module named 'src.DownloadModels'` | diagnose |
| 002873 | broken_environment | format-code-task-002873 | train | `ImportError while loading conftest '/testbed/tests/conftest.py'.` | diagnose |
| 000158 | broken_environment | format-code-task-000158 | train | `raise PackageNotFoundError(name)` | keep-build-outputs |
| 000354 | broken_environment | format-code-task-000354 | train | `raise DistributionNotFound(req, requirers)` | keep-build-outputs |
| 002017 | broken_environment | github.com/napari/napari | heldout | `ModuleNotFoundError: No module named 'napari._version'` | keep-build-outputs |
| 002281 | broken_environment | github.com/RaRe-Technologies/gensim | train | `E RuntimeError: Cython extensions are unavailable. Without them, this gensim functionality` | keep-build-outputs |
| 002739 | broken_environment | statsmodels | train | `ModuleNotFoundError: No module named 'statsmodels._version'` | keep-build-outputs |
| 002196 | broken_environment | pandas | train | `ModuleNotFoundError: No module named 'pandas._libs.tslib'` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002197 | broken_environment | pandas | train | `ModuleNotFoundError: No module named 'pandas._libs.tslib'` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002199 | broken_environment | pandas | train | `ModuleNotFoundError: No module named 'pandas._libs.tslib'` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002202 | broken_environment | pandas | train | `ModuleNotFoundError: No module named 'pandas._libs.tslib'` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002206 | broken_environment | pandas | train | `ModuleNotFoundError: No module named 'pandas._libs.tslibs.conversion'` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002208 | broken_environment | github.com/pandas-dev/pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002211 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002212 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002214 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002215 | broken_environment | github.com/pandas-dev/pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002216 | broken_environment | pandas | heldout | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002220 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002221 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002224 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002229 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002230 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 002231 | broken_environment | pandas | train | `ImportError while loading conftest '/testbed/pandas/conftest.py'.` | keep-build-outputs (pandas `_libs`, as the 14 repaired) |
| 001406 | broken_environment | griptape | train | `/testbed/mimo_test_command.sh: line 5: pytest: command not found` | login-path |

## 5. Spend

$2.4389 Daytona over 552 trials, all `har113-*` jobs (`qualify-collect`
estimate): unknown nops $1.7127 (428), repair nops $0.2926 (58), diagnoses
$0.3867 (32), leak probes $0.0303 (20), leak nops $0.0166 (14). The 000984 loop
cost $0.3501 of that (its @1 nop hit the 1800 s test timeout, and its
diagnosis ran to the agent timeout before it was cancelled). No model calls.

## Reproduce

From the worktree root, in order (each step skips work already done):

```bash
D=research/experiments/har113-variants
uv run python $D/leak_variants.py        # 247 leak variants -> leak_variants.json
uv run python $D/leak_specs.py           # probe + nop specs for the 14-task sample
uv run python $D/repair_variants.py      # repair variants -> repair_variants.json, specs/repair/
uv run python $D/diag_specs.py           # oracle diagnoses -> specs/diag/
uv run python $D/unknown_specs.py        # census unknowns -> specs/unknown/
uv run python $D/runner.py <spec.json>... --cap 2.50 --parallel 10 --wave 40
uv run python $D/results.py              # label every trial -> results.json
uv run python $D/record_verdicts.py      # append nop evidence and status to the records
uv run python $D/census_update.py        # task_qualification + task_health + SUMMARY.md
```

Specs and probe copies are gitignored; `runs/har113-*` holds the trials.
