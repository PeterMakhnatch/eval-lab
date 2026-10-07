# Python task ledger

One row per MiMo-V2.6-RL Python code task in the census pool (1,180), in
[`ledger.csv`](ledger.csv). Built by [`build.py`](build.py) from committed
inputs only; re-run it after any census or variant change:

```bash
uv run python research/experiments/python-task-ledger/build.py
```

Set up in HAR-115 under Peter's 2026-09-30 decision ("we'll document them,
either fix them or discard them... and move on").

## Counts

Since 2026-10-01 status rests on mechanical evidence only. Peter dropped the
agent- and model-reviewed labels: the HAR-111/112 GLM checker and the
rater-agent "hand" labels no longer change any status, and the columns that
carried them are gone. That returned 165 checker-only `review` tasks and 13
judgment-based discards to `usable`.

| split | usable | review | discarded | unchecked | total |
|---|---|---|---|---|---|
| train | 1014 | 2 | 31 | 0 | 1047 |
| heldout | 132 | 0 | 1 | 0 | 133 |
| all | 1146 | 2 | 32 | 0 | 1180 |

- **Selected pool 1148 (1146 usable + 2 review):** 843 run the original, 189 a leak-closed variant (`pypi_fix_released`), 116 a validated repair variant. The two digest-bound HAR-161 copied-pass cases, 002402 and 002552, remain in the oracle study rather than silently disappearing from its denominator.
  - Seven of the repairs are `env-prefetch-network@1` variants: the census nop (no lock) is sound on the original, but grading needs the network the mandatory egress lock blocks, so the row runs the prefetch variant. HAR-146: 000450 hera, 002978 kwave. HAR-158: 002289 guillotina, 002452 satpy, 002488 ray, 002755 strawberry, 002975 vyper (each locked nop sound, `har158-rnop-*`).
  - The leak-closed variants only add PyPI hosts to the `/etc/hosts` blocklist, which a root agent can rewrite. They are not a leak guarantee; network isolation is.
- **discarded 32:**
  - 18 broken environments (census nop) with no validated repair; for 8 a repair was tried and its nop rejected it;
  - 5 whose image never built on Daytona (`SandboxBuildFailedError`);
  - 4 census `grader_suspect` whose grade cannot be confirmed (000124, 000183, 001146, 001150);
  - 3 diagnosed with no repair kind (000393, 002595, 002848);
  - 2 defects found after the census, bound to the digest that showed them (`RUN_DEFECTS` in `build.py`): 001269's image holds the fixed module under `/testbed/build/lib`, and G2's 001269-a2-r2 copied it; 002078's Cython test build fails under the egress lock (a compile error, not a network fetch).
  - `reason` gives each one's evidence.

## Run history

[`task_history.csv`](task_history.csv), built by [`history.py`](history.py): one row per ledger task with its stored agent runs (controls excluded) and how they ended: `clean_pass`, `copied_pass` (HAR-143 copy check; unknown, not a failure), `fail`, `infra` (no verifier reward), plus `last_run` and the trial paths. Re-run it after new runs land. A clean pass is the first direct evidence that a task is solvable.

### Same-failing-test evidence (HAR-179)

[`failing_tests.csv`](failing_tests.csv), built by [`failing_tests.py`](failing_tests.py),
is one evidence row per ledger task for HAR-177, **not a keep/fix/discard verdict**:

```bash
uv run python research/experiments/python-task-ledger/failing_tests.py
```

Inputs are the exact paths in `task_history.csv`, the HAR-146 locked-nop manifest,
and retained HAR-140/HAR-146 publications under `~/Developer/eval-lab-results`.
`--history`, `--locked-nops`, `--results`, `--snapshot-tasks` and `--output`
support explicit local inputs. No model, sandbox, network or raw-run write occurs.
The command prints input/output hashes, coverage and candidate tasks with trial paths.

- `runs` preserves the history's publication count; `unique_runs` deduplicates
  native `result.json.id`. The frozen input has 262 entries but 259 physical
  trials on 91 tasks. `models` counts distinct declared `model_names`, including
  the named adapter; unknown identities are counted separately.
- These retained trials have **no CTRF**. The opt-in `probe03._verifier_passage`
  reader prefers CTRF when available, otherwise uses terminal pytest/unittest
  records, explicitly labelled in `verifier_sources`. `ctrf_runs` never includes
  stdout recovery; `verifier_runs` reports usable evidence of either kind.
  Incomplete/mixed sessions, malformed evidence and missing verifier output do
  not become failures. Setup/collection errors and skips are not assertion failures.
- `always_failing_tests` requires complete evidence from **every** physical model
  trial, including the infra attempts in its denominator.
  `observed_common_failing_tests` is separately labelled when coverage is partial;
  `model_evidence_state` distinguishes partial/missing coverage from a clean result.
- `test_evidence` contains per-test assertions, literal instruction membership
  (`true`/`false`/`null`), instruction paths/hashes, and nop overlap
  (`yes`/`no`/`mixed`/`unknown`). Lists/objects are JSON inside CSV cells.
  An empty assertion list means unsupported or unavailable, **not absent**.
  Instructions come from recorded task paths or digest-matched, instruction-preserving
  ancestry into the pinned HF snapshot; a newer ledger variant is never substituted.
- Locked controls require both `applied: true` and `network_block_all: true`.
  Pre-repair and repaired controls retain their separate trial paths.
  A nop's `sound` label means grading ran, not that its tests passed.
- `candidate_broken_test` is true only when a common failed test has the **same
  supported literal absent from the instruction in every model trial**.
  Unsupported/computed assertions and missing instructions cannot satisfy it.
  This is a review signal: concrete fixture strings can legitimately be absent
  from a specification, and repeated failures from one model do not prove a broken test.

### History-oracle reference controls (HAR-191)

[`extract.py`](../leak-oracle/extract.py) selects a source-only patch from an
image's own Git history. It is a read-only, standard-library script with
explicit selection and operational failure states. A clean apply check is
**not** a passing verifier. Divergent-history patches can include other source
evolution; receipts distinguish the introducing `fix_commit` from `patch_tip`.

[`prepare.py`](../leak-oracle/prepare.py) freezes the selected package bytes:
`usable` **or** `review`, not `verdict=keep` (oracle findings themselves can
change verdicts). It verifies the exact ledger `run_digest`; a missing or
changed repair is never replaced with an original package. Unknown image sizes
stay unknown and sort last.

```bash
uv run python research/experiments/leak-oracle/prepare.py \
  --ledger /path/to/frozen-ledger.csv \
  --snapshot-tasks /path/to/snapshot/tasks \
  --variants /path/to/task-store/variants \
  --output /path/to/study/cohort.json

# Plan only: no provider allocation.
uv run --with modal==1.6.1 python research/experiments/leak-oracle/sweep.py \
  --manifest /path/to/study/cohort.json --evidence /path/to/study
```

`--execute` is billable and requires the matching recorded authorization.
HAR-191's delegate-approved exception is **direct Modal verifier replay**,
not a Harbor trial, ATIF trajectory, model call, or generic queue exception.
The runner executes the selected setup and its own verifier in separate fresh
oracle/nop sandboxes, under the network lock. The inner test-command exit and
reward must agree: outer `test.sh` success alone does not prove a test pass.

- Hard resource limits are **1 physical CPU core (=2 vCPU), 4 GiB, 180 seconds**
  per sandbox. A timeout, OOM, failed setup, missing evidence or ambiguous
  extraction remains operationally unknown, not an oracle failure.
- Each batch has a fresh billing App, at most 100 tasks, and a durable
  pre-allocation reservation covering **both** locked sandboxes per task.
  Default batches contain 50 tasks. The $2 admission fence sums each App's
  larger observed gross cost or retained runtime bound, so delayed billing
  cannot erase another batch's exposure. Cold pulls remain the explicitly
  accepted residual risk; this is not a provider account dollar-kill switch.
- At most 20 fresh open-egress confirmations share that same budget.
  Only a source/patch-identical observed open pass can promote a locked
  failure to `oracle:fail-network`. Incomplete confirmation preserves the
  valid locked failure and records its separate uncertainty.
- `budget.json` retains reservations, per-batch task IDs, actual cost snapshots
  and cleanup custody. Never reset it, change evidence directories to evade
  the cap, or regenerate an admitted manifest. Missing custody or unconfirmed
  termination refuses further launches.

[`results.py`](../leak-oracle/results.py) exports the scientific
[`oracle_sweep.csv`](oracle_sweep.csv) columns `task_id,label,fix_commit,patch_tip,`
`how_chosen,evidence_path,run_digest`. Executed references bind retained patch/log
bytes, image digest, base, test patch, selected package and fresh sandbox identities.
Patch conflicts retain the candidate identity and failed apply-check diagnostics,
not the rejected patch bytes. `nop:pass` takes precedence; `oracle:none` means the
extractor found no usable source fix, not proof that no solution exists. Rejected
no-fix candidates are not published as fix commits.

Only the six accepted scientific labels enter the CSV. Every requested task,
including budget-stopped, unrun and infrastructure cases, remains in the
companion `coverage.json`. `summary.json` separates observed provider charges
from conservative exposure. The Data consumer overrides pilot labels only for
observed CSV rows and fails closed on unmapped labels or changed run digests.

#### Recorded result (2026-10-07)

The cap admitted **491 of 1,148 tasks**: 347 classified, 144 operationally unknown,
and **657 unrun** when another paired reservation would exceed the fence. This is
ordered partial coverage, not 1,148 completed verifier pairs.

| observed label | tasks |
|---|---:|
| `oracle:pass+nop:fail` | 193 |
| `oracle:none` | 78 |
| `oracle:fail` | 52 |
| `oracle:patch-conflict` | 23 |
| `oracle:fail-network` | 1 |
| `nop:pass` | 0 |

All 20 open confirmations completed: one passed, 19 failed. They were the first
eligible failures in frozen image-size/task order, not a random population sample.
The final read-only billing observation was **$1.09520036 posted**, with
**$1.98388775 conservative exposure**; billing was not settled.

[`summary.json`](../leak-oracle/summary.json) records counts, source hashes and full
external coverage/custody paths. [`manual-checks.json`](../leak-oracle/manual-checks.json)
contains three checks each for none/fail/conflict and the sole observed network
positive (three network examples were unavailable; no nop-pass was observed).
Candidate misses, docs-only conflicts and historical API drift remain extractor
limitations, not evidence that the underlying task is unsolvable.

## Status rule

| status | rule |
|---|---|
| `usable` | the census nop is sound on the package to run (the original, or a validated repair variant). A `pypi_fix_released` task runs its leak-closed variant |
| `review` | `pypi_fix_released` with no leak-closed variant, or a copied-pass exploit bound to the probed run digest (`PROBE_CRACKED` in `build.py`) |
| `discarded` | `broken_environment` with no validated repair; census `grader_suspect` with no repair; or a run defect in `RUN_DEFECTS` |
| `unchecked` | no census nop |

## Columns

| column | meaning |
|---|---|
| `task_id`, `split`, `project`, `image_mib` | from the census (`project` is the census project key; `format-code-task-*` means no repository was found) |
| `status`, `reason` | as above |
| `run`, `run_digest` | `original`, `leak-closed` or `repair`, and the digest to run: the census `task_version_digest`, or the variant's `variant_digest`. The package is at `derived/task-store/variants/<slug>/<digest12>/` |
| `run_transform`, `run_variant_status` | the variant's transform and its record status |
| `census_label`, `census_nop_job`, `census_evidence` | the census nop |
| `leak_channel` | census leak channel |
| `evidence` | repo paths: census row, chosen variant record, rejected repair records |

## Proposed for HAR-120 (30)

[`har120_proposal.csv`](har120_proposal.csv) holds 30 then-`usable` train tasks. It is a frozen HAR-120 input (read by `har120-data-batch/make_specs.py` and the failure atlas); `build.py` no longer regenerates it.

- **Stratified:** one per repository.
- **Order:** lightest image first.
- **Excluded:**
  - held-out tasks;
  - the 10 HAR-116 Part A tasks, plus its 5 Part B tasks;
  - tasks whose repository is unknown (`project` is the task id), so that no repository can be picked twice under two names.
- **Pool:** 671 usable train tasks with a known repository.
- **Repository match:** keys are compared by their last path segment, ignoring case and treating `-` and `_` as the same. So `github.com/psf/black` and `black` count as one repository.

The proposal mixes run types:
- 17 run the original;
- 12 run a leak-closed variant (9 `candidate`, 3 `validated`);
- 1 runs a repair.

| # | task | repository | image MiB | run | variant status |
|---|---|---|---|---|---|
| 1 | 001647 | environ | 363 | leak-closed | validated |
| 2 | 000803 | mdutils | 386 | leak-closed | validated |
| 3 | 001870 | markdownify | 386 | leak-closed | candidate |
| 4 | 000341 | vyper | 396 | original | - |
| 5 | 001897 | linkpreview | 397 | original | - |
| 6 | 002938 | krakenex | 398 | original | - |
| 7 | 001710 | pbxproj | 402 | original | - |
| 8 | 001399 | django_filters | 403 | leak-closed | candidate |
| 9 | 002680 | sc3 | 403 | original | - |
| 10 | 001661 | friends | 404 | original | - |
| 11 | 000813 | django_recaptcha | 406 | original | - |
| 12 | 000838 | ntfy | 407 | leak-closed | candidate |
| 13 | 001373 | google | 409 | original | - |
| 14 | 001609 | fabulous | 410 | original | - |
| 15 | 002356 | black | 410 | original | - |
| 16 | 001265 | pelican | 412 | leak-closed | validated |
| 17 | 000552 | nse | 414 | original | - |
| 18 | 000865 | logstash | 414 | original | - |
| 19 | 002555 | webpush | 414 | original | - |
| 20 | 002552 | miio | 419 | original | - |
| 21 | 001865 | DnD_battler | 423 | original | - |
| 22 | 001269 | responses | 424 | leak-closed | candidate |
| 23 | 000227 | github.com/devpi/devpi | 425 | leak-closed | candidate |
| 24 | 000666 | pyromat | 425 | original | - |
| 25 | 001618 | github.com/jazzband/pip-tools | 427 | repair | validated |
| 26 | 002416 | ropetest | 427 | leak-closed | candidate |
| 27 | 000941 | pooch | 433 | leak-closed | candidate |
| 28 | 001820 | github.com/lovasoa/marshmallow_dataclass | 434 | leak-closed | candidate |
| 29 | 002104 | ollama | 435 | original | - |
| 30 | 002393 | wheel | 435 | leak-closed | candidate |
