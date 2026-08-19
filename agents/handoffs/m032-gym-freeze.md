# M032 GYM-RUN — cycle 1: freeze gym-v0

Status: complete — ready for review
Last: built the freeze generator, froze gym-v0, and locked the freeze contract with
mutation-verified tests. The frozen set is **empty**, which is the finding.
Next: the campaign cannot proceed past this cycle until tasks are registered. That
is the existing Peter decision (register the curated-nominee slice, or reject the
study) — not a new one, and not something a worker can resolve.
Blockers: none for this cycle.

## What landed

| Path | What it is |
|---|---|
| `library/frozen/gym-v0/_freeze.py` | generator: reads the registry at runtime via `TaskRegistry.from_repo`, projects each record's digests + battery evidence pointers, renders deterministic JSON |
| `library/frozen/gym-v0/manifest.json` | the frozen record: `task_count: 0`, dated, commit-stamped `a9e9075` |
| `library/frozen/gym-v0/README.md` | the freeze contract, in the directory it governs |
| `tests/test_gym_freeze.py` | 8 tests for determinism, immutability, and honesty |

Placement note: the generator sits under `library/frozen/gym-v0/` to stay inside
GYM-RUN's declared lease, following the existing `library/curated/_emit_card.py`
precedent. It is code, not a hand-written JSON file, because the manifest's whole
value is that it reports what was true rather than what someone typed.

## gym-v0 is the empty set, and that is the honest answer

```
$ uv run python -m evallab.cli registry list
No task records found in library/registry/.

$ uv run python library/frozen/gym-v0/_freeze.py --repo-root .
froze 0 task(s) -> library/frozen/gym-v0/manifest.json
```

`library/registry/` contains only `.gitkeep`. `registry.py` refuses any spec whose
task is not registered, so no trial can be submitted against gym-v0 at all.

Three things were deliberately **not** done, because each would have manufactured a
baseline that does not exist:

- did not substitute the four directories under `library/tasks/` for registry
  records — available ≠ registered, and the campaign's provenance depends on the
  distinction;
- did not widen the definition of "registered" to include `candidate` records;
- did not withhold the manifest to avoid an awkward zero.

The manifest carries a `note` field stating the measurement, the exact command
output, and that promotion is human-only.

## Tests, and the mutations that prove they bite

```
MUT 1 — allow overwriting a frozen manifest (`if path.exists()` -> `if False`)
FAILED tests/test_gym_freeze.py::test_writing_over_a_frozen_manifest_is_refused
  - DID NOT RAISE <class 'gym_freeze.FreezeRefused'>

MUT 2 — hand-edit the manifest to claim an unregistered task
FAILED tests/test_gym_freeze.py::test_manifest_only_claims_tasks_the_registry_registers
  - AssertionError: manifest claims unregistered tasks: {'registered/invented-task'}

restored -> 8 passed
```

The overwrite refusal is the test that gives "frozen" meaning: without it the file
is a cache that quietly tracks today's registry, and every number citing it becomes
uncomparable. The out-claim test is what stops the manifest from becoming fiction.

## Gate

`premerge.sh` output pasted in the PR.
