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
| train | 1016 | 0 | 31 | 0 | 1047 |
| heldout | 132 | 0 | 1 | 0 | 133 |
| all | 1148 | 0 | 32 | 0 | 1180 |

- **usable 1148:** 843 run the original, 189 a leak-closed variant (`pypi_fix_released`), 116 a validated repair variant.
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

## Status rule

| status | rule |
|---|---|
| `usable` | the census nop is sound on the package to run (the original, or a validated repair variant). A `pypi_fix_released` task runs its leak-closed variant |
| `review` | `pypi_fix_released` with no leak-closed variant (none today) |
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
