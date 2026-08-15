Status: reviewed; exact-head CI pending
Last: integrator corrections pass 22 focused tests, headless AppTest, 371-test premerge, operational smoke, Ruff, and ty ratchet
Next: push reviewed head, require fresh exact-head checks, then integrator may merge PR #44
Blockers: none

# M005 handoff — run and analysis explorer

**Executing agent/model (recorded per mission order): Claude Code
(interactive session), model claude-opus-5[1m] (Opus 5, 1M context).**

Lease (exact): `src/evallab/explorer.py`, `tests/test_explorer.py`,
`tests/fixtures/explorer/`, `dashboard/explorer.py`, `dashboard/app.py`
(additive pointer block only), `dashboard/README.md`, `docs/run-explorer.md`,
this file. Untouched: CLI, queue, execution, policy, profiles, analysis
generation, raw runs/evidence, tasks, registry, ACTIVE.md.

## Sequencing (on the record)

M005 was gated on M002. I armed a merge watch on PR #42, prepared reads while
blocked, and created this worktree only after #42 merged (base `4f0824e`).
No work started against a pre-M002 base; the shared `dashboard/app.py` was
edited only after M002's version landed, additively.

## What was built

- `explorer.py`: read-only index over job dirs + analysis sidecars. Every
  field is a `Labeled(value, provenance, reason)` with
  observed/derived/draft/unavailable. Outcome classes keep
  `infra-exception` (exception_info present → reward `unavailable`, never a
  score) disjoint from `reward-failure`/`pass`/`no-verdict`. Trajectory view:
  ordered steps, tool calls with exit codes via linked observations,
  repeated identical (function, arguments) signatures, verify-before-done
  (verification-shaped call in the final five). Citations resolve
  file+step+call against the trial or render unresolved with the reason.
  `jail()` refuses `..`, absolute paths, and task `tests/`+`solution/`;
  `redact_mapping()` blanks key-shaped names. Analyses' conclusions are
  always `draft`; only their validation_status is `observed`.
- `dashboard/explorer.py`: three-tab Streamlit page; infra exceptions in a
  visually separate section; Next Action rendered as copyable
  `evallab run/analyze plan/submit/approve` + `harbor view` commands (all
  verbs verified against the real CLIs); nothing executable from the page.
- Fixtures: pass (verify-before-done + artifact), reward-failure with a
  4× identical failing tool loop, harness exception, missing trajectory,
  valid sidecar, sidecar citing a nonexistent step, malformed sidecar,
  poisoned config (`FAKE_API_KEY`) proving redaction.

## Evidence

```
$ uv run pytest tests/test_explorer.py -q
................                                                         [100%]
16 passed
```

Coverage mapping: pass/fail/harness-exception (test_pass_fail_and_exception…),
missing trajectory, tool loop, verification behavior, artifact links,
valid/invalid citations, duplicate IDs (dual-root copytree), path escape,
cold start, status/explorer consistency (scratch-layout snapshot agreement),
zero writes (full-tree size+mtime snapshot before/after double build).

Streamlit render verified two ways against committed fixtures (details, no
screenshots — repo does not require them):

```
headless server: http=200, 0 tracebacks in runs/_m005-smoke.log
AppTest (streamlit==1.61.1, EVALLAB_EXPLORER_ROOT=tests/fixtures/explorer):
PASS title rendered / tabs rendered / infra exception separated /
     provenance badges present / broken sidecar warned /
     next-action code blocks / harbor view command / no secret rendered
AppTest render: ALL PASS
```

Premerge (final): the first full run FAILED at ty 29 > 28 — my citation
resolver had a `Path | None` narrowing gap; fixed in explorer.py (no baseline
change). Final run:

```
$ bash scripts/premerge.sh
premerge green: Python 3.12; ty 28 <= 28
```

## Notes for the integrator

- `dashboard/app.py` change is a 7-line additive pointer block at EOF.
- The explorer never loads Postgres/Parquet directly in this slice; it reads
  raw jobs + sidecars (the status snapshot covers store health). Extending
  to Parquet-backed views is a natural M00x follow-up.

## Integrator review — 2026-08-15

Rebased onto current main after M008. Semantic review found that the emitted
`harbor view` command pointed at a trial directory even though Harbor accepts
a folder containing job directories, the exception status command pointed at
a job directory that status interprets as an empty repository, and a citation
could claim a tool call from a different step. It also found shell-active
angle-bracket placeholders/unquoted evidence paths and silent ambiguity when
two trials share an ID.

The reviewed head now:

- emits quoted `harbor view <jobs-root> --jobs`, analysis, and correct-root
  status commands with inert placeholders and sanitized job names;
- requires a cited tool call to belong to the cited step;
- leaves duplicate-ID analyses unlinked with a visible note;
- reads explicit registry absence as observed `not registered`;
- recursively redacts key-shaped mappings inside lists; and
- degrades malformed agent/reward/config shapes without raising.

Focused evidence after review:

```
$ uv run pytest tests/test_explorer.py -q
22 passed
$ uv run ruff check src/evallab/explorer.py dashboard/explorer.py tests/test_explorer.py
All checks passed!
$ EVALLAB_EXPLORER_ROOT=tests/fixtures/explorer ... AppTest
AppTest PASS 11 expanders 13 commands
$ bash scripts/premerge.sh
371 passed; Docker-free operational smoke PASS; Ruff clean; ty 28 <= 28
```
