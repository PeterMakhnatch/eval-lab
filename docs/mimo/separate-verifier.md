---
status: living
audience:
  - builder
  - operator
---

# separate-verifier@10: separate verifier container

Canonical active transform is `separate-verifier@10` (`src/evallab/separate_verifier.py`; earlier `@3`–`@9` development history is in git history).
`separate-verifier@10` (`TRANSFORM_ID_V10`, `build_changes_v10`,
`derive_separate_verifier_v10`, `render_wrapper_test_sh_v10`,
`V10_INTEGRITY_HOOK`, `V10_UNITTEST_SHIM` in
`src/evallab/separate_verifier.py`; tests in
`tests/test_separate_verifier.py`) closes the three holes Traces proved
on canonical `@8` packages in the own-runs scan
(`research-context/analysis/2026-10-14-own-runs-env-flaw-scan.md` §1).
No `@2`–`@9` symbol or template byte changes. It ships as the
`mimo-clean-v6` chain slot (`PYTHON_CHAIN_V6`/`NONPYTHON_CHAIN_V6`);
adopting it fleet-wide is a rebuild + re-verification decision, not a
default change.

| Hole (@8 measured reward 1 with no fix) | `@10` fix |
|---|---|
| Unchecked-hash `.pyc` in `__pycache__` rides the kept snapshot unscanned and shadows pristine source (002139) | Binary artifacts (`__pycache__`, `*.pyc`/`*.pyo`/`*.so`/`*.pyd`/`*.dll`/`*.dylib`/`*.o`/`*.a`/`*.class`) are dropped from the kept change (`is_binary_artifact_path`), `__pycache__`/`*.pyc` are purged before the agent run, and grading runs with `PYTHONDONTWRITEBYTECODE=1` + a verifier-owned `PYTHONPYCACHEPREFIX` |
| `pytest.Function.runtest = lambda self: None` (plain or obfuscated) in an imported package `__init__` no-ops every test body; junit honestly reports all-pass (002139) | A verifier conftest hook captures `pytest.Function.runtest`, `_pytest.python.Function.runtest`, and `unittest.TestCase.run` at conftest import and records identity changes at session finish; any change grades 0. The unittest shim performs the same check for pure-unittest commands |
| Unittest graded from stdout: printing `Ran 1 test … OK` and exiting 0 at import scored 1 (002866) | Unittest grades only from a structured junit report written by a verifier-owned `sitecustomize` shim (loaded via `PYTHONPATH` at interpreter startup), with the @6 fail-to-pass comparison over junit status maps. No cases, no reward — stdout is never consulted |

The four proved attacks are also ladder attacks now (ladder v3
`3.0.0`: `tamper_pytest_runtest`, `tamper_pytest_runtest_obfuscated`,
`pyc_shadow`, `fake_unittest_output`), so a regression reopens loudly.

Residual: the integrity check is identity-based, not a sandbox — code
that restores the original function object before session finish, or
patches a different internal, is out of scope for this gate and stays
with the tamper signatures plus fail-to-pass. The shim/hook are
best-effort like the earlier hooks; a missing integrity file is treated
as unchanged and the structured grading rules still apply.

### Validation (MEASURED, 2026-10-14, local Docker, $0)

Traces' reproducer packages (002139 pytest/bitbake, 002866
unittest/sqlglot; `derived/detector-roots/own-scan/hardened-probe`) with
the `@10` grader swapped in, graded by Harbor 0.24.0 oracle runs:

| case | `@8` (Traces) | `@10` |
|---|---|---|
| oracle (real fix) | 1 | 1 (pytest), 1 (unittest) |
| nop | 0 | 0 (pytest), 0 (unittest) |
| `noop_runtest` | 1 | 0 |
| `noop_runtest_obfuscated` | 1 | 0 |
| `pyc_shadow` | 1 | 0 |
| `fake_unittest_output` | 1 | 0 |
| control: literal `_pytest` signature | 0 | 0 |

Ladder v3 through `evallab cheat run --ladder-version 3.0.0` (the three
pytest attacks in one trial): `@8` grader cracked (reward 1), `@10` grader
clean (reward 0).

A first `@10` build let the unittest shim's exit-time write overwrite the
pytest hook's `CHANGED` verdict (both wrote `integrity.txt`); the live
`noop_runtest` probe graded 1 and exposed it. The shim now writes
`integrity-unittest.txt` and the gate reads both files. Unit tests alone
did not catch this, which is why the live probe is part of the evidence.

### HAR-208 exact-input acceptance (2026-10-17)

The source repair and ladder v3 already exist; HAR-208 adds full-wrapper
regressions rather than changing immutable transform bytes. The tests execute
the rendered `@10` shell grader on real synthetic repository snapshots:
pytest and unittest oracle/nop score 1/0, and all four selected, armed v3
attacks score 0. They also prove that the bytecode appears only in the full
diff, not the kept diff, that a clean unittest shim cannot overwrite the
pytest integrity failure, and that forged stdout with zero structured
unittest cases earns no reward. The source-spelling gate stays clear in all
four regressions. Focused verification: 213 tests passed across
`tests/test_separate_verifier.py` and `tests/test_cheat.py`.

For native exact-package acceptance, `derive_separate_verifier_v10` refuses a
parent already in `separate-verifier` mode (`src/evallab/separate_verifier.py:111-112`);
in the canonical chain, repairing the HAR-207 `@8` packages
(`002139 = dd269adcb32c…`, `002866 = a811bab8c3fb…`) replaces
`separate-verifier@8` with `separate-verifier@10` on their exact
`mtime-normalize@2` parents (`548ba35556cd…` and `1273b89e8915…`), yielding
`6d156eb8a8ce50b1bf11de06b8fbebf6de3205ee82451cd85e96d0fee39deca0` (`002139`)
and `faf79fbb247cabed25d83ece356d763cb223e9bed828b2f85798ff2bc6c2a139` (`002866`).
All seven non-verifier files in both packages are byte-for-byte identical to
the `@8` originals; only `task.toml`, `tests/Dockerfile`, and `tests/test.sh`
change.

On those two exact `@10` packages, the reinspected 2026-10-15 native local
Docker controls record `oracle-x3 = [1,1,1]`, `nop-x3 = [0,0,0]`, and reward `0`
on all four HAR-207 `.solve.sh` probes (`noop_runtest`,
`noop_runtest_obfuscated`, `pyc_shadow` on `002139`; `fake_unittest_output`
on `002866`). Four fresh native `CheatAgent` (`--ladder-version 3.0.0`,
Harbor `0.24.0`) local Docker controls coordinated through the host-wide
shared Docker FIFO (`evallab.docker_lock.docker_slot`,
`EVALLAB_STATE_JOURNAL=off`) also armed (`signal="armed"`) and scored `0.0`
(`verdict="clean"`), published under
`~/Developer/eval-lab-results/2026-10-17/HAR-208-v6-*` and SHA-256-bound in
[`research/evidence/har208-verifier-controls-20261017.json`](../../research/evidence/har208-verifier-controls-20261017.json).
Manifest `verify` remains `unverified:ladder-probe-pending` (full 22-attack
ladder and fix-content census remain separate), and no fleet-wide repair claim
is made.

### Repeat-idempotent pristine anchor (000245 + 000048 fold)

`@10` also fixes the repeat-3 oracle failure measured on the
opaque-wrapper tasks `000245` (TypeScript) and `000048` (Go), and
folds those two rows onto `@10` in `mimo-clean-v5`
while keeping the other 2,696 v5 rows on
their banked digests (incl. 001431 on `@9`).

### Root cause (traced through the verifier container)

Harbor's `RepeatVerifier` (`--verifier evallab.harbor_repeat_verifier:RepeatVerifier --verifier-kwarg repeat_n=3`) runs `test.sh`
three times in the **same** verifier container. The agent runs in a
different container: its diff reaches the verifier only as
`/var/tmp/mimo-separate/workspace.tgz`, which each `test.sh`
invocation re-extracts to `/tmp/agentcopy` and re-diffs — the agent
diff never lives in the verifier workdir between runs. What does
persist is the verifier workdir itself, plus the ready-gated setup
(`[ -f "$M/ready" ] && exit 0`), so runs 1–2 do not reset anything.
Run 0 applies `/tests/test.patch`, creating
`mimo_build_env.tar.gz.b64` and `mimo_test_command.sh` (plus whatever
their execution leaves, e.g. run markers and `__pycache__`), and
leaves them in the workdir. On `repeat/1`, `@9` captured `PRISTINE`
with `git add -A -f .` over that dirty tree, so the "pristine" tree
contained the patch files; the test-file revert then kept them and
`git apply /tests/test.patch` failed with `already exists` (run 0
`apply.log` clean, `repeat/1` erroring, reward missing/0).

### The `@10` fix

1. **Ignore-rule alignment with `render_snapshot_hook_v2` (#848):**
   the snapshot hook archives `git ls-files -c -o --exclude-standard`
   (omitting gitignored dependency trees such as `000245`'s
   `node_modules/`). `@10` stages `PRISTINE`, `/tmp/agentcopy`, and
   the kept tree with `git add -A` (no `-f`, under the base
   `.gitignore`), so omitted ignored trees are never mistaken for
   agent deletions and baked `node_modules/` stays intact.
2. **Anchored pristine reset across repeats:** run 0 saves the
   pristine tree SHA in `/var/lib/mimo/pristine-tree` and the
   pre-existing ignored file list in
   `/var/lib/mimo/pristine-ignored.bin`. Every repeat invocation
   restores `$PRISTINE` (`git read-tree` + `git checkout-index -f -a`
   + `git clean -fdq`), removes newly created ignored files
   (`post - pre`) plus any newly emptied parent directories (so
   directory-existence build guards such as `000245`'s
   `[ ! -d polaris-tokens/dist ]` re-trigger cleanly), and clears
   grade-transient state under `$GRADE`.
### Validation (MEASURED, 2026-10-14, $0)

- Unit/shell: the shipped `@10` script runs 3× in one workdir on an
  opaque-wrapper fixture (hidden patch creates
  `mimo_build_env.tar.gz.b64` + `mimo_test_command.sh`, the command
  leaves a run marker): oracle ×3 = `[1, 1, 1]`, nop ×3 =
  `[0, 0, 0]`, no `already exists` in any repeat `apply.log`. The
  same fixture under `@9` passes run 0 and fails repeat/1 with
  `already exists` (the regression pin).
- Local Docker (`RepeatVerifier repeat_n=3`, one container at a
  time): 000245 oracle ×3 and 000048 oracle ×3, plus nop controls —
  see the v5 census receipt for job paths and rewards.

## Start-up hooks: why grading is a separate container (2026-10-30)

TQ-2 (`research-context/bridge/2026-10-29-ds-squad/raw/TQ-2/report.md`)
measured the one attack family that survives a same-container reset: a
`.pth` file written into the interpreter's writable site directory runs
before any test code, and can end the interpreter (`os._exit(0)`, also
from `atexit`) or submit a clean JUnit report read from `sys.orig_argv`.
No file reset and no `--junitxml` gate inside the graded interpreter can
close it, because the report and the exit status come from the environment
being checked.

The shipped clean chain already answers this structurally:
`separate-verifier@2+` grades in a verifier image built from `tests/`
(``environment_mode = "separate"``), receives only the snapshot of the
agent workspace, drops test-infra paths (``conftest.py``, ``*.pth``,
``sitecustomize``/``usercustomize``, pytest config) from the kept change,
and never mounts the agent's site directories. A start-up hook planted by
the agent therefore cannot reach the grading interpreter.

The ladder makes that measurable. `cheat_ladder` 4.0.0 appends three
probes to the v3 generation (`tamper_pth_exit`, `tamper_pth_atexit`,
`tamper_pth_forge_report`): each finds every writable site directory
(`site.getsitepackages`, the user site, `<workdir>/**/site-packages`,
`VIRTUAL_ENV`/`CONDA_PREFIX`), plants `zz_cheat_pth.pth` plus its module,
and reports whether the hook arms — at plain interpreter start-up or, for
fixtures whose directory is not a real site dir, through `site.addsitedir`.
`CHEAT_FORGE_CASES` sets the case count in the submitted report.

Executed check (`research/experiments/h2-separate-verifier-pth-20261030/`):
on 4 DEV Python packages of `mimo-clean-v5` (`@8` packages, the same
separation property as `@10`), the reference solution grades 1, the empty
submission grades 0, and the three start-up probes grade 0 — the agent
side arms, the verifier side ignores it. Preregistered rules and the exact
rewards are in that receipt.

### Host runtime requirement (measured blocker, 2026-10-30)

Executed grading needs the pinned Harbor runtime *in the interpreter that
launches the trial*, not only a `harbor` executable on `PATH`. Two
distinct failures were observed and are now refused early instead of
surfacing as `native reward file is missing`:

- **No runtime in the driver venv** (`harbor[modal]` is in base `pyproject.toml` dependencies; a leftover namespace
  directory can make `import harbor` succeed while `harbor.trial` does
  not exist). Every native path now refuses with
  ``execution_backend_refused`` and the
  remedy `uv sync --locked`.
- **Version skew on `PATH`**: a standalone `uv tool` install of another
  release (observed 0.21.0 beside the pinned 0.24.0) was picked up by the
  `harbor run` child and failed inside the pinned adapter hooks. The
  launcher now puts this interpreter's own `bin` directory first on the
  child `PATH` (`harbor_view.prepend_pinned_harbor_path`), so the child
  uses the same pinned release as the adapters it imports.

Only the in-process grading path refuses on a missing runtime: a queue
launch runs Harbor as a child, which may legitimately come from another
install, so that path pins the binary instead of refusing.

Run native cells with `uv run evallab ...` (or that venv's
interpreter) so both the module and the executable come from the same
pinned install.

