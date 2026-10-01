# Python task ledger

One row per MiMo-V2.6-RL Python code task in the census pool (1,180), in
[`ledger.csv`](ledger.csv). Built by [`build.py`](build.py) from committed
inputs only; re-run it after any census, variant or label change:

```bash
uv run python research/experiments/python-task-ledger/build.py
```

Set up in HAR-115 under Peter's 2026-09-30 decision ("we'll document them,
either fix them or discard them... and move on").

## Counts

| split | usable | review | discarded | unchecked | total |
|---|---|---|---|---|---|
| train | 866 | 0 | 181 | 0 | 1047 |
| heldout | 106 | 0 | 27 | 0 | 133 |
| all | 972 | 0 | 208 | 0 | 1180 |

- **usable 972:**
  - 746 run the original;
  - 134 run a leak-closed variant (`pypi_fix_released`);
  - 92 run a validated repair variant (HAR-113/HAR-115 repairs derive from the leak-closed variant where there is one).
  - 122 of the 224 variants are leak-closed `candidate`s: their nop evidence is the original's. The blocklist reaches `/etc/hosts` only through the agent harness, so a nop cannot tell the two apart.
  - 3 of the 972 left review in HAR-127 by majority: 000927, 001868 and 002757 (see [Review triage](#review-triage-har-127)).
- **review 0:** the 174 review tasks were triaged in HAR-127.
- **discarded 208:**
  - 165 instruction gaps where the HAR-112 checker says broken (HAR-127 triage);
  - 11 hand-labelled broken (8 by two raters, 3 by adjudication);
  - 2 where most of the judges say broken (HAR-127 triage: 002407, 002628);
  - 4 census `grader_suspect` whose grade cannot be confirmed (HAR-127 triage: 000124, 000183, 001146, 001150);
  - 8 broken where a repair was tried and its nop rejected it;
  - 10 broken with no known repair kind matching the error, not attempted;
  - 5 whose image never built on Daytona (`SandboxBuildFailedError`);
  - 3 diagnosed with no repair kind (000393, 002595, 002848).
  - `reason` gives each one's evidence.

## Status rule

From Research-Harbor's HAR-115 comment:

| status | rule |
|---|---|
| `usable` | the nop is sound on the package to run (the original, or a validated repair variant); not hand-labelled broken; not checker-broken. A `pypi_fix_released` task runs its leak-closed variant |
| `review` | the environment is fine but only the checker, or only one hand rater, says broken; or census `grader_suspect` with no repair |
| `discarded` | `broken_environment` with no validated repair; or hand-labelled broken by two raters, or by adjudication |
| `unchecked` | no census nop |

Hand labels come from two sources: HAR-111 raters 1 and 2 (`har111/census_labels.jsonl`), and HAR-112 raters A and B plus the adjudicator (`har112/hand_{a,b,adj}/`). An adjudication decides when present. A checker or hand `suspect` does not change status; the label is kept in the row.

## Review triage (HAR-127)

HAR-127 part 4: fix each `review` task with a known repair kind and re-nop it, or discard it with a reason. The known repair kinds in `library/task-variants` change the environment (`env-*`) or close a leak. None of them restates an instruction or confirms a grade, so none of them fixes a review task, and no re-nop was run ($0). `build.py`'s `triage` then decides each task:

| review cause | count | decision |
|---|---|---|
| HAR-112 checker alone says broken | 165 | `discarded`. Every one has at least one `not_inferable` item; `reason` gives the checker's sample agreement (91 at 3/3, 73 at 2/3, 1 at 1/3), the not-inferable item kinds and one test. These are the candidates for a future instruction-disclosure repair. |
| one hand rater says broken | 5 | majority of the judges, meaning the hand raters plus the checker (`suspect` is not `broken`). 002407 (checker + rater 2) and 002628 (checker + rater 1) are `discarded`; 000927, 001868 and 002757 (1 of 3) become `usable`. |
| census `grader_suspect`, no repair | 4 | `discarded`, because the grade cannot be confirmed. 000124 is missing Alembic tables; 000183 has a pytest segfault (exit 139); 001146 has no count line; in 001150 the tests import Python 2 source under Python 3.14. |

The HAR-120 proposal is unchanged by the triage.

## Columns

| column | meaning |
|---|---|
| `task_id`, `split`, `project`, `image_mib` | from the census (`project` is the census project key; `format-code-task-*` means no repository was found) |
| `status`, `reason` | as above |
| `run`, `run_digest` | `original`, `leak-closed` or `repair`, and the digest to run: the census `task_version_digest`, or the variant's `variant_digest`. The package is at `derived/task-store/variants/<slug>/<digest12>/` |
| `run_transform`, `run_variant_status` | the variant's transform and its record status |
| `census_label`, `census_nop_job`, `census_evidence` | the census nop |
| `leak_channel` | census leak channel |
| `checker_label` | HAR-112 checker (`pool_labels.jsonl`) |
| `hand_labels` | `rater=label` pairs: `har111-1`, `har111-2`, `har112-a`, `har112-b`, `adj` |
| `evidence` | repo paths: census row, chosen variant record, rejected repair records, checker row and hand-label files |

## Proposed for HAR-120 (30)

[`har120_proposal.csv`](har120_proposal.csv) holds 30 `usable` train tasks.

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
