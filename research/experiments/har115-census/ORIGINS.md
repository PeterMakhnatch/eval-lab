# HAR-115: defect origins (Xiaomi / FineEnvs / ours)

For each HAR-113 defect cluster: is the defect in Xiaomi's original tasks/images, introduced by the FineEnvs Harbor conversion, or introduced by our Eval Lab harness? Evidence only; nobody was contacted. Anything not directly observed is marked [INFERENCE].

Short version: the `git clean` breakage is Xiaomi's own cleanup, ported verbatim. The login-PATH breakage is FineEnvs' (`sh -c` instead of Xiaomi's `bash -lc`). The PyPI hole is FineEnvs' blocklist (Xiaomi's pods needed none). The wrong-package images are Xiaomi's. Root-as-agent is ours (FineEnvs matches us on code tasks).

|cluster|tasks|origin (Xiaomi / FineEnvs / ours / unknown)|evidence link|
|---|---|---|---|
|keep-build-outputs|32 HAR-113 + 40 HAR-115 validated|Xiaomi|[mimoagent base.py keep lists](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/datasets/base.py#L644-L683)|
|keep-files|3 (002696, 001246, 002055)|Xiaomi|[same cleanup, same port](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/datasets/base.py#L709-L736)|
|login-path (incl. PYTHONPATH)|13 HAR-113 + 16 HAR-115 validated|FineEnvs (image layout: Xiaomi)|[code_test.sh `sh -c`](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/mimo_harbor/templates/code_test.sh#L22) vs [k8s `bash -lc`](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/kubernetes.py#L474)|
|pin|4 HAR-113 + 2 HAR-115 (001618, 002191)|Xiaomi|trial logs below; deps are image-baked, setup installs nothing|
|HAR-113 unknown-wave errors|27 (+24 matched to kinds up front)|mostly Xiaomi (cleanup), then FineEnvs (login shell); 4 were not defects|§5: HAR-115 repair nops|
|census false positives|000211, 001981, 002207, 001860 (+ repaired 002307)|ours (census classifier)|`src/evallab/task_health.py` missing-work rules, fixed in HAR-115|
|unbuildable / unfixable|6|mixed per-task (see §6)|census rows + logs below|
|PyPI blocklist gap|247 `pypi_fix_released`|FineEnvs|[ANSWER_HOSTS has no PyPI](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/app/runner/opencode.py#L36-L44)|
|root agent|all code tasks|FineEnvs (we match it; Xiaomi uses the container default user)|`agents/mimo_opencode.py:41-61` in the snapshot; ours: `src/evallab/harbor_terminus.py:242-263`, `src/evallab/task_catalog.py:879`|

Dataset snapshot paths below are under `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/`. Trial runs are under the read-only worktrees `~/Developer/eval-lab/.worktrees/har108-census-20260930/runs/` and `~/Developer/eval-lab/.worktrees/har113-variants-20260930/runs/`. Upstream links are commit-pinned except the FineEnvs branch HEAD (`ac898e4`, may move).

## 1. keep-build-outputs: Xiaomi

Xiaomi's harness runs `git clean -fdx` at setup with a keep list that deliberately deletes build output. `base.py` says so: "git clean removes everything else untracked/ignored — including build OUTPUT ... which is what we want gone" ([base.py L644-649](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/datasets/base.py#L644-L649)). The python keep list is only `[".venv", "venv"]` ([L659-660](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/datasets/base.py#L659-L660)); code rows carry no `language`, so the fallback union applies ([L130-134](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/datasets/base.py#L130-L134)). Xiaomi's own training enables it: all four verl code harnesses set `anti_hack_cleanup: true` on `environment_class: kubernetes` ([mini-bash.yaml L21-31](https://github.com/XiaomiMiMo/verl/blob/a2ad9f6160b03ff2d47e59832bfb6b289f37c917/config/agent/code/mini-bash.yaml#L21-L31)).

FineEnvs ports this verbatim, not approximately. The explorer comment says `CLEAN_KEEP` is "base.py _CLEAN_KEEP_COMMON + _CLEAN_KEEP_UNKNOWN: these rows carry no `language`, so mimoagent keeps the union" ([domains.py L77-80](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/app/runner/domains.py#L77-L80)), and the generated `setup.sh:25` exclude list matches that union exactly. The NOTICE file says the keep list "is ported from XiaomiMiMo/mimoagent": true, and the port is faithful.

The images leave the build outputs in the repo tree (Xiaomi's images). Proof: excluding them from the clean fixes grading without installing anything, e.g. `har113-rnop-002195` goes broken→sound, and the parent log shows the image's own `.so` files missing at import: `ImportError: C extension: 'hashtable' not built ... run 'python setup.py build_ext --inplace --force'` (`runs/har108-nop-002195/har108-nop-002195__rMxJU8U/verifier/test_output.log:1`). So the cleanup (Xiaomi) deletes files the image (Xiaomi) needs. FineEnvs reproduced both halves exactly: origin Xiaomi.

## 2. keep-files: Xiaomi

Same mechanism as §1. Sourmash's `_lowlevel*.py` and burnman's `burnman/data/*` are git-ignored files the image left in the repo; the ported `git clean -fdx` deletes them. Burnman log: `FileNotFoundError: [Errno 2] No such file or directory: '/workspace/repo/burnman/data/input_masses/atomic_masses.dat'` (`runs/har108-nop-001246/har108-nop-001246__qAfQcL6/verifier/test_output.log`). Origin Xiaomi, same citations as §1.

## 3. login-path: FineEnvs (image layout: Xiaomi)

Xiaomi runs every command, including the test command, under a login shell on its training backend: `exec_argv = ["timeout", str(timeout), "/bin/bash", "-lc", full_command]` ([kubernetes.py L474](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/kubernetes.py#L474)); all verl code harnesses use `environment_class: kubernetes`. (Docker would be `bash -c`, non-login, but training does not use it.) The images rely on this: "language toolchains are on PATH with their environment exported" is a stated image expectation ([opensource_code.py L45-53](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/datasets/opensource_code.py#L45-L53)).

FineEnvs grades with `timeout 1800 sh -c "$(cat /tests/test_command.sh)"` ([code_test.sh L22](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/mimo_harbor/templates/code_test.sh#L22), same line in snapshot `tasks/format-code-task-000527/tests/test.sh:34`): non-login `sh`, not even bash. The venv/pyenv paths the images put on the login PATH are invisible there. Diagnosis from inside the image proves it: `== which -a pytest:` (empty) vs `== login-shell PATH: /testbed/.venv/bin:/root/.pyenv/shims:...` (`results.json`, `diagnosis/har113-diag-000527` listing). Nop symptom: `/testbed/mimo_test_command.sh: line 4: pytest: command not found` (`runs/har108-nop-000527/.../verifier/test_output.log`), identical for 001406 (`runs/har113-nop-001406/.../verifier/test_output.log`: `line 5: pytest: command not found`).

Split: the shell downgrade is FineEnvs'. The venv-on-login-PATH-only layout is Xiaomi's image design, but it works under Xiaomi's harness, so it is not a Xiaomi defect on its own.

## 4. pin: Xiaomi

Setup installs nothing; every dependency comes baked in the image. What the image has wrong, only an install step can fix:
- 000655/002496: mixed setuptools install (newer files under an older dist-info): `ImportError: cannot import name 'FileError' from 'setuptools.errors'` (`runs/har108-nop-002496/.../verifier/test_output.log`).
- 002078: image's Cython 3.2.5 fails numpy's `test_cython` build; pinning 3.0 passes.
- 002893: twisted runs from source with no distribution metadata: `importlib.metadata.PackageNotFoundError: No package metadata was found for twisted` (`runs/har108-nop-002893/.../verifier/test_output.log`).
- 001561 (rejected pin, see §6): `/root/.venv` built for a later indico: `ImportError: cannot import name 'dict2schema' from 'webargs'` (`runs/har108-nop-001561/.../verifier/test_output.log`).

All four are properties of the image as Xiaomi shipped it. Origin Xiaomi.

## 5. HAR-113 unknown-wave errors: attributed by repair (HAR-115)

HAR-115 repaired these (`research/experiments/har115-census/README.md` has every nop). A repair that turns a broken nop sound, changing only the setup step named, attributes the defect to whoever owns that step:

- Setup's `git clean` deleted a build output (keep-build-outputs, 35 sound): pandas `_libs` ×17, package metadata (000158–000161, 000354, 002307), generated version modules (001607, 002017, 002353, 002410, 002451, 002739, 002873), compiled extensions (000190, 002281, 002400, 002483, 002644) → Xiaomi, as §1. 002055's git-ignored `frontend/dist` (keep-files) → Xiaomi, as §2.
- The grader's non-login shell (login-path, 6 sound): 001058, 001068, 001406 (venv/pyenv interpreter), 001024 (pyenv 3.7 has `cgi.escape`, the non-login Python 3.11 does not), 000968 (pyenv 2.7 for `py.test` on Python 2 code), 001176 (the image's `.bashrc` exports `PYTHONPATH=/testbed`: `har115-diag2-001176`) → FineEnvs, as §3.
- The image's packages (pin, 2 sound): 001618 (pip 20.2.4's dist-info over a later pip's files, so `python -m pip` fails on import: `har115-diag-001618`, `har115-diag2-001618`; pip-tools 4.5.0 needs pip < 20.1), 002191 (`/testbed/.venv` pyOpenSSL 26.2.0 lacks the `OpenSSL.tsafe` the base werkzeug imports) → Xiaomi, as §4.
- Not defects: 001860 (the tests import `RoomGuestAccessEvent`, the class the task adds) and 002307's post-repair failure (`mock.patch` of `_get_container_id`, the function the task adds) were census false positives, fixed in the classifier → ours. 002649 was a `DaytonaConflictError`; its re-nop is sound → ours (backend).
- Still open: 000393 (molecule's schema rejects its own `delegated` driver: `{'driver': [{'name': ['unallowed value delegated']}]}`, `har115-diag2-000393`; the image's molecule plugins vs the base commit [INFERENCE] → unknown, likely Xiaomi image), 000124 (`no such table: user`: the fixtures expect tables Alembic would create; `har115-diag-000124`) → unknown, 002848 (`No module named 'src.DownloadModels'`; the instruction names the class, not the module) → unknown, 001146 and 002595 (tests run and fail on assertions; census label only) → not environment.

## 6. unbuildable / unfixable: mixed per-task

- 000613, 002142: unknown. `SandboxBuildFailedError` in `result.json` (`n_errors: 1`, `exception_stats: SandboxBuildFailed...`) means our Daytona backend could not build Xiaomi's image. Whether the image is unbuildable or the backend is limited cannot be told from the logs.
- 000738: Xiaomi. Census evidence: `ImportError: CuPy is not correctly installed` (`task_health.parquet`, `format-code-task-000738`, nop `har105-qual-py-000738`). The image/test needs CUDA; CPU-only backends cannot pass it. [INFERENCE] it likely passes on Xiaomi's GPU infra, so this is a capability mismatch, not necessarily a reportable bug.
- 001150: Xiaomi, unless §3 applies. Python-2 base source (`SyntaxError: Missing parentheses in call to 'print'`) against the non-login Python 3.14 (`runs/har108-nop-001150/.../verifier/test_output.log`). [INFERENCE] 000968 had the same symptom and its image carries pyenv 2.7.18 on the login PATH (`har115-diag-000968`); 001150 was not probed, so it may be a FineEnvs login-shell case instead.
- 002373: Xiaomi. The hidden test itself is buggy: `NameError: name 'List' is not defined` (`runs/har108-nop-002373/.../verifier/test_output.log`). A test fix, not an environment fix.
- 001561: Xiaomi. `/root/.venv` was built for a later indico; the base commit's `requirements.txt` does not install on the image's Python 3.11 (celery 5.0.2 metadata needs pip<24.1, then lxml 4.6.1 builds from source without headers). Both pins rejected with nop evidence.

## 7. PyPI blocklist gap: FineEnvs

The shipped blocklist (33 lines incl. header, `tasks/format-code-task-000527/environment/setup/files/blocklist`) blackholes code hosts, bug trackers, Go proxies and search engines — but no PyPI host. That list is FineEnvs' `ANSWER_HOSTS` ([opencode.py L36-44](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/app/runner/opencode.py#L36-L44)), written into the task by the adapter ([adapter.py L240-242](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/mimo_harbor/adapter.py#L240-L242)).

It is not Xiaomi's list with PyPI removed: mimoagent takes `answer_leak_blocklist` from per-run config ([base.py L197-208](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/datasets/base.py#L197-L208)), and none of the four verl code harness specs sets it (verified at pinned SHA `a2ad9f6`). Xiaomi's pods needed no hosts list: "Xiaomi's pods had no route to the internet, so their agents could not look answers up" ([opencode.py L11-15](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/app/runner/opencode.py#L11-L15)). The hosts-blocklist approach exists only because HF Sandbox has internet; forgetting the package index for Python tasks is FineEnvs' omission. Our harness applies the same list (`harbor_terminus.py:242-263`), so we inherit the hole rather than introduce it.

## 8. root agent: FineEnvs, and we match it

Code tasks set no agent user: no `[agent].user` in `task.toml` (e.g. `tasks/format-code-task-002195/task.toml`), no `agent_user` file or reference anywhere under the task dir, and `jobs/code.yaml` sets none. FineEnvs' reference agent only drops privileges when `/var/lib/mimo/agent_user` exists (`agents/mimo_opencode.py:41-61`); the adapter writes `agent_user = "agent"` only for cyber and general ([adapter.py](https://github.com/adithya-s-k/FineEnvs/blob/ac898e41a6caf6bfda9982670dccbd2e5cf8c860/mimo-explorer/mimo_harbor/adapter.py) cyber/general builders). So on code tasks FineEnvs' agent runs as root too — the "unprivileged `agent`" claim in the dataset README holds for cyber/general only.

Our harness likewise runs the agent as root: an absent `[agent].user` defaults to `"root"` (`src/evallab/task_catalog.py:879`), and the blocklist is applied as root (`src/evallab/harbor_terminus.py:242-263`, called at `:824`). A root agent can rewrite `/etc/hosts` (`src/evallab/task_health.py:591-593`). Xiaomi's side defaults to the container user (`exec_user: str | None = None ... None = container default` ([kubernetes.py L89-90](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/environments/kubernetes.py#L89-L90)); the image default user was not verified (see below). Origin of the bypassability on our runs: ours.

## Why FineEnvs' validation could not catch these

Their stated check: "With Harbor's no-op agent every dataset scores 0 and every verifier runs to completion: no free rewards" (snapshot `README.md` validation section), i.e. "every verifier runs to completion" plus score 0. The adapter README spells the assumption out: "untouched Code repos fail their hidden tests". A nop that crashes at import (`ModuleNotFoundError`, conftest `ImportError`), finds no `pytest` at all (`pytest: command not found`), or errors at collection also scores 0 with the verifier running to completion — the check cannot distinguish a sound task (hidden tests fail = 0) from a broken one (environment collapses = 0). Only the per-task log text tells them apart, and the validation never reads it. The 6-task parity subset (overlapping score ranges) is likewise blind to defects outside those 6 tasks.

## Could not verify

- Whether Xiaomi's training actually ran (and scored) the specific broken tasks; no training logs are public. [INFERENCE] everywhere this file extrapolates from harness code to training behavior.
- Whether Xiaomi's pods had PyPI egress blocked at the network layer; inferred from the explorer docstring plus the absence of `answer_leak_blocklist` in all four verl code specs.
- Image internals (default USER, whether `frontend/dist` or `*.egg-info` ever existed): no image pulls were done; presence is inferred from repair-nop transitions (a file kept from `git clean` that fixes grading existed before it).
- 000613/002142 build failures: image-side vs Daytona-side undetermined.
- §5's open tasks (000393, 000124, 002848), and the census waves' 33 new finds: 15 were repaired by §1/§3 kinds (attributed as those sections), 6 such repairs were rejected and 12 were not attempted (discarded in the ledger, `research/experiments/python-task-ledger/`, except 000183's grader_suspect, which is in review), so those 18 are not attributed.
- FineEnvs adapter/branch links are pinned to HEAD `ac898e4` of a mutable branch; Xiaomi links are pinned to the SHAs the dataset itself references (`467f0a1`, `a2ad9f6`).
