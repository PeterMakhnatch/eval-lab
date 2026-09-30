# HAR-105 exploration task set

**Purpose (Peter, 2026-09-29 about 22:25Z):** explore the MiMo RL environments with a few runs, find task problems, fix tasks where needed, and rerun them to check they are fixed and still useful. There is no SFT and no RL. Engineering's HAR-104 runs 1 attempt per task with MiMo-V2.6-Flash.

`select.py` is deterministic and writes `selection.json` plus the music nop specs in `specs/`. The pool is the `train` side of the sealed split (`../har81-mimo-sft/split.json`). No task comes from the held-out side.

## Rule

1. **Drop known problems.** Remove tasks with an `error` finding, tasks on the pinned export-broken list, and the part-2 suspects (1634, 1789, 1702, `arvo_18737`, `arvo_57589`). Also remove 2684, whose pass was tainted: it has the same missing-stevedore environment as 1789.
2. **English only.** Of the MiMo tasks, 421 general and 504 music tasks are Chinese. English traces are the ones we can read and audit.
3. **general: drop "final response" tasks.** These are 501 of the 925. The grader reads the agent's final message from an OpenCode event log (`/logs/agent/*.txt`), which Terminus-2 does not write. It falls back to `answer.md`, but only 1 of 925 instructions mentions that file. A Terminus-2 agent therefore cannot deliver the graded answer. Music has the same log fallback, but every music instruction says "Write your complete reply to /app/answer.md", so music is unaffected.
4. **Order.** Rank tasks by `sha256("har105:" + task_id)` and take at most one task per `split_group` and per `metadata.category`.
5. **Nop check.**
   - **code, cyber, terminal:** a task needs an existing `ok` Daytona nop row for its `task_version_digest`. Its nop verifier output must also show no setup error (`SETUP_ERROR` in `nopspec.py`: a raised missing module or package, a pytest collection or fixture-setup error, or a missing command). That second condition is HAR-97's proposed rule, and a plain `ok` does not include it.
   - **general, music, webdev:** the top 3 per domain are candidates. The first 2 whose nop comes back clean make the set.

## The set (10 tasks)

| domain | task | category | nop (Daytona) |
|---|---|---|---|
| code | format-code-task-001483 | Python | ok, `mimo-qual-c-format-code-task-001483` (HAR-88) |
| code | format-code-task-000464 | Unknown | ok, `mimo-qual-c-format-code-task-000464` (HAR-88) |
| cyber | arvo_41330 | matio | ok, `har95-qual-y-arvo-41330` (HAR-95) |
| terminal | candidate-2262-operations-compliance | Terminal tasks | ok, `mimo-qual-t-candidate-2262-operations-compliance` (HAR-88) |
| music | music-gk-1037 | Pop & modern | ok, `har105-qual-m-music-gk-1037`: reward 0, "The agent wrote no answer." |
| music | music-gk-0515 | Ensemble | ok, `har105-qual-m-music-gk-0515`: reward 0, same message |
| general | s3k_0036_accounting_audit_tax_en_t2_rl_006 | Accounting, audit & tax | **blocked: judge key** |
| general | s3k_1345_healthcare_ops_en_t3_rl_007 | Healthcare operations | **blocked: judge key** |
| webdev | dasyn_260630_00319 | Other | **blocked: judge key** |
| webdev | dasyn_260638_00006 | Restaurant & food | **blocked: judge key** |

- **Spare.** `music-gk-1087` (Chinese traditional, still English) also nopped `ok`.
- **Nop spend.** The three music nops cost $0.0015 in total, about 21 s each.
- **Naming.** The qualification table records music ids as `music-gK-…`, with an upper-case K taken from the source id. This is the catalog's `mimo-id-case-collision` warning. Join on `task_version_digest`, not on the id.

## general and webdev cannot run through Eval Lab yet

Every general and webdev task grades with a model judge. The key comes from the host in each task's `task.toml` `[verifier.env]`:
- general: `GA_JUDGE_KEY = "${HF_TOKEN}"`, with default model `thinkingmachines/Inkling`;
- webdev: `WEBDEV_JUDGE_KEY = "${HF_TOKEN}"`, with default vision model `meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8`;
- both default to the Hugging Face router, `https://router.huggingface.co/v1`.

Three facts, all read from the code, block both nop and real runs:
1. **Harbor refuses the job at start** when a `${VAR}` without a default is unset (`harbor/cli/jobs.py`, "Missing Environment Variables"). `resolve_env_vars` raises again at verify time.
2. **Eval Lab's executor passes Harbor an allowlisted environment** (`execution_contracts.subscription_environment`). The allowlist has no judge key, so exporting `HF_TOKEN` would not reach Harbor either.
3. **There is no `HF_TOKEN` on this machine.** The Hugging Face snapshots were pulled anonymously.

**Consequences:**
- **Each graded run costs judge money.** The catalog already flags this as `mimo-paid-judge` on all 925 general and 2,093 webdev tasks. The webdev judge samples at temperature 1.0.
- **The key would enter the sandbox.** The verifier runs inside the agent's sandbox, as the catalog's `mimo-verifier-not-isolated` finding records. The general grader prints the first 12 characters of the key and then redacts its own log.
- **The judge can be pointed elsewhere.** `MIMO_TEXT_JUDGE`, `MIMO_VISION_JUDGE` and `MIMO_JUDGE_URL` override the model and endpoint. The key variable name stays `HF_TOKEN`.
- **A nop needs no judge call.** A webdev nop with no `dist/index.html` scores 0 before any judge call. A general nop with a failed gate skips the judge. So a placeholder key would be enough to nop-check both domains, but it still needs a way to pass the variable.

**A local nop is not a substitute.** Local Docker on this Mac failed webdev `dasyn_260630_00319` at the healthcheck with rc=127, before grading (`/private/tmp/har105-local-nop/`). HAR-88 saw the same local rc=127 on `music-gk-0000`, and that task passed on Daytona.

**general has more for HAR-104 to know:**
- the selected tasks declare 6 and 7 MCP servers;
- Terminus-2 gets them only as a list appended to the instruction (`terminus_2.py`, "MCP Servers"), so it must call them over HTTP itself;
- the agent phase runs as user `agent`, with `agent_timeout_sec` 3,600.

## Other notes for HAR-104

- **The cyber pick is unfixed on purpose.** `arvo_41330` carries HAR-97's submit-step gap: none of the 1,000 cyber instructions mention submitting. The whole instruction is three lines: role, tools, and the sanitizer crash line. It runs as an unfixed control next to the fixed `arvo_18737` and `arvo_57589` variants from part 2.
- **Code picks inherit HAR-98.** `tests/test.sh` diffs against git HEAD, which inflates diff sizes on some images. No reward changed in the pilot.
- **Timeouts** come from each task's own `[agent].timeout_sec`: code 3,600 s, cyber 3,600 s, terminal 900 s, music 900 s.

## Part 2: task fixes

Each fix is a task variant under `library/task-variants/`, derived with `evallab tasks derive` from the pinned snapshot. The snapshot itself is never edited. `fixes.py` writes a Daytona nop spec for each variant and `fixes.json`, which pairs the parent's existing nop with the variant's nop.

**Clean** means the variant's nop grades with reward 0, raises no exception, and hits no `SETUP_ERROR`.

All five are clean, and all five stay `candidate` until the model check.

| task | problem | fix | variant | nop before (parent) | nop after (variant) |
|---|---|---|---|---|---|
| candidate-1634-software-databases | (1) The grader asserts a literal source line, `def atomic(self, transaction_type=None, **kwargs):`, that the instruction never asks for. (2) Pristine `peewee.py` does `from collections import Callable`, which fails on the image's Python 3.12, so nothing imports. | `tests/test_outputs.py`: an AST check that `atomic` accepts `lock_type`, the instruction's requirement 1. `setup.sh`: rewrite that one import to fall back to `collections.abc`. | `8de24f28a748` | reward 0, pytest `ERROR collecting` (ImportError `Callable`) | reward 0: 5 failed, 1 passed, 0 setup errors |
| candidate-1702-ml-inference | The image lacks `tqdm`. The instruction's own runner, `tools/run_fixture.py`, dies importing it, both for the agent and inside the grader. | `setup.sh`: `pip install tqdm`. | `2da2a320d7f9` | reward 0, 7 `ModuleNotFoundError: tqdm` lines | reward 0: 6 failed. The runner now stops at `cannot import name 'resolve_max_length'`, the shared function the instruction's requirement 5 asks the agent to write. |
| candidate-1789-security-appsec | The image runs the vendored Bandit from source but never installed it. Its requirement `stevedore` is missing, and so is its distribution metadata: `import bandit` itself reads `metadata("bandit")`. | `setup.sh`: `pip install stevedore`, then register the vendored tree's metadata and entry points, taken from its own `setup.cfg`, as a dist-info in site-packages. `/app` is untouched. | `caa7772d131e` | reward 0, 4 tests `ERROR at setup` (stevedore) | reward 0: 4 failed, 3 passed, 0 setup errors |
| arvo_18737 | The grader scores only a PoC sent with `/home/agent/submit.sh`. The instruction never says so: none of the 1,000 cyber instructions mention submitting. | `instruction.md`: say to write a PoC, submit it with `bash /home/agent/submit.sh <poc>`, and that the last submission must crash in the named function with the named sanitizer error. | `2c9c97a36419` | reward 0, `submitted: false` | reward 0, `submitted: false` |
| arvo_57589 | Same as 18737. | Same text. | `634bbec7b602` | reward 0, `submitted: false` | reward 0, `submitted: false` |

The cyber nops are identical before and after by design: the fix changes only what the agent is told. Only a model run can show it works.

**Design choices worth a second look:**
- The 1634 check also accepts a bare `**kwargs`. The `lock_type` behavior tests still decide the reward.
- 1702 and 1789 install from PyPI at setup, which relies on the task's `network_mode = "public"`. Versions are unpinned: stevedore is `>=1.20.0`, from Bandit's own requirements.
- 1789 registers Bandit as version `0.0.0`, because the vendored tree carries no version.
- 1789's nop logs `Could not load 'sarif': No module named 'sarif_om'`. That is Bandit's optional sarif formatter, and a stock `pip install bandit` logs the same line.

### Superseded attempts (kept in `fixes.json` as `earlier_attempts`)

1. **The first three environment fixes edited `environment/setup/setup.sh` and nothing else. Their nops matched the parent exactly.**
   - **Why:** MiMo tasks never run that folder from disk. `[environment.healthcheck].command` carries it as an inline base64 tar.gz, unpacks it into `/var/lib/mimo` and runs `setup.sh`. The folder is a readable copy.
   - **Checked across the whole collection:** that copy matches the payload in all 7,780 tasks.
   - **Encoder:** `setup_payload.py` re-encodes the folder byte for byte as the adapter does, a USTAR tar with mode 0700, mtime 0 and a gzip header with mtime 0. `--check` reproduces every payload in the collection.
   - Each environment fix now changes `setup.sh` and `task.toml` together.
2. **1789, stevedore only:** `import bandit` then failed on the missing package metadata. The actual defect is that Bandit was never installed.
3. **1634, the first patch had a Python syntax error in its setup heredoc** (a string literal split across lines). `bash -n` passes it, and the subagent's own static checks missed it. It was caught in review, before any run.

### What the `tasks` commands cannot do cleanly

- **`derive` and `lint` don't know that the setup folder is only a copy.** An edit to `environment/setup/` produces a new digest but runs the old setup. A lint rule flagging "setup folder differs from the healthcheck payload" would have caught all three first attempts, with zero false positives upstream. Alternatively, `derive` could re-embed the folder automatically.
- **`lint` didn't detect any of the five problems.** Findings were the same before and after for all five: a verifier-isolation warning and a solution-present error. It has no check for a missing dependency, a literal-source assert, or a grading channel the instruction never mentions.
- **`derive` fails on the read-only snapshot**, because `copytree` keeps mode 0444 and the write then fails. It needs a writable copy of the parent. `--parent-source` has to be written by hand, even though the snapshot's `provenance.json` has it.
- **`replay` can't run a nop on a variant.** It swaps model and harness only. Variant nops went through queue specs instead. The queue accepts a variant package path once it's staged under the submitting checkout (`nopspec.stage`), because a worktree's `derived/` starts empty.
- **`qualify-collect` only collects existing jobs, and it rewrites the whole table.** Every earlier job has to be passed again. No verb runs a nop or an oracle.
- **`variant-status` records only a final verdict** (`validated` or `rejected`). It can't attach interim evidence such as a nop while a model check is pending. That is why these variants stay `candidate`.
- **Qualification `ok` is not a clean nop.** 1634, 1702 and 1789 were `ok` in the table while their graders crashed on imports.
- **`evallab tick --parallel`: 2 of 14 jobs failed catalog ingest with Postgres `DeadlockDetected`.** Re-running `evallab ingest runs/<job>` fixed each one.

### Model check for HAR-104 (1–2 runs each)

Packages live in the shared store, `derived/task-store/variants/<slug>/<digest12>` in the primary checkout. A queue spec in a worktree must stage the package under that worktree first. The specs in `specs/har105-fix-*.json` show the fields.

| task | package | what a run should show |
|---|---|---|
| candidate-1634-software-databases | `mimo-v2.6-rl__candidate-1634-software-databases/8de24f28a748` | a correct `lock_type` fix scores without matching one literal signature |
| candidate-1702-ml-inference | `mimo-v2.6-rl__candidate-1702-ml-inference/2da2a320d7f9` | the runner starts; the score reflects the context-length repair |
| candidate-1789-security-appsec | `mimo-v2.6-rl__candidate-1789-security-appsec/caa7772d131e` | Bandit imports offline; no agent `pip install` needed (compare 2684) |
| arvo_18737 | `mimo-v2.6-rl__arvo_18737/2c9c97a36419` | the agent submits (`submitted: true`) |
| arvo_57589 | `mimo-v2.6-rl__arvo_57589/634bbec7b602` | the agent submits (`submitted: true`) |

**Spend:** 14 Daytona nop jobs over both parts cost $0.0059: 3 music, 11 fix nops, superseded attempts included. No model calls.

## Part 3: Python code tasks only

**Scope (Peter via Research-Harbor, 2026-09-30 about 02:30Z):** narrow exploration to one niche, MiMo Python code tasks. They grade with hidden tests, not a model judge. The other domains and the part-2 variants are parked. 1634, 1789 and 1702 are terminal tasks, not code tasks, so none of them carry over.

`select_python.py` writes `python_selection.json` and the nop specs `specs/har105-qual-py-*.json`. The rule:

1. **Pool:** train-side code tasks with `metadata.category = "Python"` whose hidden test patch touches a `.py` file, minus the part-1 exclusions. That is 1,047 of the 1,179 Python code tasks (the other 132 are held out).
2. **One task per repo.** `split_group` names the repo for only 126 of the 1,047 and is wrong for two picks: 002401 is pyproj, not `user-attachments/assets`, and 002864 is sqlglot, not `apache/superset`. Otherwise the repo key is the project package the hidden tests import. The repo column below was checked by hand against test paths and nop tracebacks.
3. **Light images first.** Take the top 30 by `sha256("har105-py:" + id)` with distinct repos. Nop-check the 13 with the smallest compressed image (Docker Hub manifest sizes, cached in `image_sizes.json`). The set is the first 10 clean ones.

| task | repo | image | nop failures (all feature-level) | in set |
|---|---|---|---|---|
| format-code-task-000226 | Pylons/waitress | 407 MB | 6 of 13 unittest failures | yes |
| format-code-task-001896 | meyt/linkpreview | 416 MB | 16 failed, 2 passed (`no attribute 'favicon'`) | yes |
| format-code-task-000927 | facelessuser/soupsieve | 418 MB | 22 failed (`no attribute 'escape'`) | yes |
| format-code-task-002256 | peter-wangxu/persist-queue | 467 MB | 5 failed, 6 passed | yes |
| format-code-task-001832 | machow/siuba | 478 MB | collection error: `cannot import name 'rename'`, the verb the instruction asks for | yes |
| format-code-task-000738 | cupy/cupy | 498 MB | **broken:** "CuPy is not correctly installed", so the tests can't import | no |
| format-code-task-000383 | quickfix (Python FIX client) | 522 MB | 13 failed (`no attribute 'subscribe_to_data'`) | yes |
| format-code-task-002407 | python-control/python-control | 587 MB | 8 failed, 1 passed | yes |
| format-code-task-002391 | pypa/pip-audit | 752 MB | 6 failed, 2 passed | yes |
| format-code-task-002401 | pyproj4/pyproj | 1,045 MB | **broken:** `No module named 'pyproj._context'` (C extension never built) | no |
| format-code-task-002259 | PHAREHUB/PHARE | 1,098 MB | 5 failed, 3 passed (`KeyError` on the new MHD keys) | yes |
| format-code-task-002218 | pandas-dev/pandas | 1,183 MB | **broken:** conftest import fails, `pandas._libs.interval` not built | no |
| format-code-task-002864 | tobymao/sqlglot | 1,603 MB | 5 errors, all `ParseError` on the new syntax | yes |

Every nop has reward 0 and no trial exception. The qualification table marks all 13 `ok`, including the 3 broken ones. `nopspec.SETUP_ERROR` now also matches `ImportError while loading conftest`, `is not correctly installed` and `build_ext`, so it catches all three. Part 1's `selection.json` is unchanged under the wider rule.

**Spend:** 13 Daytona nops, $0.0535 (cap $0.10). No model calls.
