# M032 GYM-RUN — cycle 2: EXP-S03 `extra_instruction_path`

Status: complete — ready for review
Last: added `extra_instruction_path` to `ExperimentSpec`, threaded it through
`RunRequest` → `build_command` → the queue dispatcher, fenced it with the existing
repo-relative path validator, and updated the frozen contract golden additively.
Next: submitting the EXP-S03 treatment arm is still blocked — the registry is empty,
so no spec is submittable. The arm must be paired with the ledger's 2026-08-15
control; never fabricate a second control.
Blockers: none for this cycle.

## Why this field is the elicitation lever

`harbor run` takes an extra instruction file and appends it to the task
instruction. Verified against the installed Harbor 0.21.0 rather than assumed:

```
$ harbor run --help | grep -i extra-instruction
│ --extra-instruction-path          <path>  Path to an extra instruction file  │
```

That makes the preamble a first-class, recordable part of the elicitation tuple
(agent version, model pin, preamble hash, toolset, k) instead of something a
runner smuggles in. EXP-S03's treatment arm varies it; the control leaves it unset.

## What changed

| File | Change |
|---|---|
| `src/evallab/schemas.py` | `extra_instruction_path: str | None = None` on `ExperimentSpec`, with a description saying it is the elicitation lever; added to the existing `paths_are_repo_relative` validator field list |
| `src/evallab/runner.py` | `RunRequest.extra_instruction_path: Path | None`; `build_command` appends `--extra-instruction-path <path>` only when set |
| `src/evallab/queue.py` | dispatcher resolves it through `self._safe_repo_path(...)` beside `jobs_dir` and forwards it into `RunRequest` |
| `tests/fixtures/contracts/ExperimentSpec.json` | frozen golden updated **additively** — verified `added={'extra_instruction_path'}`, `removed=set()` before writing |

The dispatcher hop is the one that matters: a spec field that never reaches the
runner is precisely the defect class this repo keeps finding, so the test asserts
the **argv**, not the field.

## Tests, and the mutations that prove they bite

Added to `tests/test_runner.py`: the flag appears with the right value; it is
**absent** when unset (and no empty-string argv element); the existing argv prefix
order is unchanged so goldens and callers are unaffected.
Added to `tests/test_contracts.py`: the field defaults to `None`; accepts a
repo-relative file; and rejects `/etc/passwd`, `../../etc/passwd`,
`library/../../x`.

```
MUT 1 — delete the argv append in build_command
FAILED tests/test_runner.py::test_extra_instruction_path_is_forwarded_to_harbor
  - assert '--extra-instruction-path' in ['harbor', 'run', '--path', ...]

MUT 2 — remove extra_instruction_path from the path validator
FAILED tests/test_contracts.py::test_extra_instruction_path_cannot_escape_the_repository[/etc/passwd]
FAILED ...[../../etc/passwd]
FAILED ...[library/../../x]
  - DID NOT RAISE ValidationError

restored -> green
```

Note the schema-drift gate did its job unprompted: adding the field failed
`test_golden_schemas_match_live` ("ExperimentSpec schema drift") until the golden
was regenerated. That is the frozen-contract guarantee working, and the
regeneration was checked to be purely additive.

## Gate

```
$ bash scripts/premerge.sh
1451 passed, 2 skipped, 1 xfailed
premerge green: Python 3.12; ty 27 <= 28
```

One honest note on process: the first premerge run failed
`test_repomap.py::test_check_passes_on_real_repository_tree` because the generated
`docs/repo-map.md` was stale relative to the new module surface. Regenerated, then
green — recorded because "regenerate the generated docs" is a step that is easy to
skip and the suite is right to catch it.

## Scope discipline

No drive-by refactors. `cli.py` is untouched — there is deliberately **no new CLI
flag** in this cycle: the field travels spec → dispatcher → harbor, which is the
path the campaign uses. Exposing it on `evallab run` is a separate decision and a
separate lease.
