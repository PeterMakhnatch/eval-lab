Status: review-wanted
Last: PR #71 opened at head `7245cd1`; all five GitHub checks pass (lint, test 3.12, test 3.14, ty, profile)
Next: integrator merges #71 per `agents/CHECKS.md`; findings F1-F3 need a Platform lease this mission was forbidden
Blockers: none

# FIXTURE-DRIFT — make fixtures that cannot exist in reality fail the suite

Branch `role/fixture-drift`, worktree `.worktrees/fixture-drift`, forked from
`origin/main`. **Correction to the brief:** `origin/main` was `0960eea`, not
`7456ac8`. `7456ac8` (#66) is its parent; `0960eea` adds only
`docs/build-plan.md` (141 lines, docs-only, verified with
`git show --stat 0960eea`). Nothing in this mission depends on the difference.

**Lease extended mid-mission** by the integrator, on the evidence in the
inventory below: `tests/fixtures/analysis_worker/.../trajectory.json` ×3 and
`tests/fixtures/operability/complete/.../trajectory.json` were added to the
original lease (`tests/fixtures/explorer/`, `tests/test_explorer.py`, a new
conformance test, this handoff) so the guard could cover `tests/fixtures/**`
with no allowlist. `tests/test_analysis_worker.py`,
`tests/test_operator_surfaces.py` and `tests/test_status.py` were granted
conditionally on an assertion breaking; **none broke, so none were touched.**

## The headline measurement

Running the ingest's own validation (`atif._document_validation`, the function
`atif._project_payload` calls for every document it lands) over every tracked
JSON file whose `schema_version` starts with `ATIF-`:

| population | count | verdict |
|---|---|---|
| real documents (9 promoted Codex trajectories + `research/explorations/harbor-021/fixtures/trajectory.json`) | 10 | **all `valid`** |
| committed fixtures (before this PR) | 7 | **all `invalid`** |

`proven live` — measured in this worktree at `0960eea`, against committed files.

Validation was never the weak link; only the test inputs were, and nothing
compared the two populations. Every one of the seven failed first on
`agent.name must be a string` — no top-level `agent` object at all — which says
these documents were written against an imagined schema rather than derived
from a real one. That is the root cause, and it is why the drift was uniform
rather than incidental.

Independently corroborated: `harbor_atif2otel.validate.validate_trajectory`
(installed via `harbor-atif2otel>=0.1.0`, a different codebase from
`src/evallab/atif.py`) returns the same partition — same seven invalid, same
`agent` and `function_name` complaints.

## Inventory of every divergence

### A. Fabricated fields — a name that appears in no real document and no validator

| fixture | field it used | field the validator requires | deliberate? |
|---|---|---|---|
| `tests/fixtures/explorer/jobs/{job-pass,job-fail,job-exc}/t1/agent/trajectory.json` | `steps[].observations` | `steps[].observation.results` (`atif.py:296-306`) | no — fixed |
| `tests/fixtures/explorer/jobs/{job-pass,job-fail}/t1/agent/trajectory.json` | `steps[].tool_calls[].function.{name,arguments}` | `steps[].tool_calls[].function_name` + `.arguments` (`atif.py:291-294`) | no — fixed |
| `tests/fixtures/analysis_worker/jobs/{job-pass,job-fail,job-exc}/join-trial/agent/trajectory.json` | both of the above, plus `observations[].command_exit_code` | as above, plus `observation.results[].extra.exit_code` (`atif.py:446-454`) | no — fixed |
| `tests/fixtures/explorer/jobs/*/t1/config.json` | `"agent": "codex"` (string) and an invented `env` mapping | `"agent": {"name", "model_name"}`; real Harbor trial config holds only `agent`, `task`, `trial_name`, `trials_dir`, `job_id` — **no `env` in any committed evidence, and `src/evallab/` never writes one** | no — fixed |

`command_exit_code` on a raw observation deserves its own line: it is a
**derived projection column** (`atif.py:132`, `atif.py:744`, consumed by
`facts.py:233` and `report.py:163`), never a document field. Every occurrence in
the repository is the derived column — except `explorer.py:428`. See finding F1.

### B. Required fields absent

| fixture | missing | deliberate? |
|---|---|---|
| all 7 ATIF fixtures | `agent.name`, `agent.version` | no — fixed |
| `tests/fixtures/operability/complete/jobs/operability-join/join-trial/agent/trajectory.json` | non-empty `steps` (it was `[]`) | no — fixed, one step added |

### C. Minimal but real — **not** drift, left alone

Fixtures carry a subset of the real keys: no `lock.json` under explorer trials,
no per-step `metrics`/`llm_call_count`, no `final_metrics`, no
`evallab_redaction`. A subset of real fields is a smaller real document, not a
fiction, and `AGENTS.md` asks for small fixtures. The rule applied throughout:
**every field a validator requires or a consumer reads, and nothing decorative.**
`evallab_redaction` is correctly absent — these fixtures were never promoted,
and `test_live_run_artifacts_are_not_labelled_redacted` depends on that absence.

### D. Deliberate — invalid on purpose, left exactly as they were

| fixture | why | in ATIF guard scope? |
|---|---|---|
| `tests/fixtures/explorer/analyses/broken/analysis.json` (`{not json`) | `test_malformed_sidecar_becomes_a_note` | no — unparseable, so it claims to be nothing |
| `tests/fixtures/operability/malformed/analyses/bad/analysis.json` | malformed-sidecar path | no — same |
| `tests/fixtures/operability/malformed/jobs/broken-job/result.json`, `.../queue/approved/broken-spec.json` | malformed-store paths | no — not ATIF |
| `tests/fixtures/explorer/analyses/badstep/analysis.json` | cites step 99, which does not exist; `test_invalid_citation_is_flagged_not_hidden`. **Schema-valid** under `TrialAnalysisSidecar` — the defect is semantic, by design | n/a |
| `tests/fixtures/task_workbench/cases/*` (21 files) | each case is a deliberately defective candidate task (`hidden-leak`, `path-escape`, `forged-registration`, …) | no — no ATIF documents |

Both parseable sidecars (`analyses/valid`, `analyses/badstep`) already validate
against `TrialAnalysisSidecar` (#57). **No sidecar divergence found.** No
fixture promotion manifest exists, so there is no #58 divergence either — and
that absence is itself asserted, correctly, by
`test_live_run_artifacts_are_not_labelled_redacted`.

**Zero deliberately-invalid ATIF documents exist today.** The declaration
mechanism below is therefore proven on documents written inside the test rather
than by a committed example, so nothing unused was committed.

## The guard — `tests/test_fixture_conformance.py`

The durable output. It walks `tests/fixtures/**`, selects every document that
presents itself to the ingest as ATIF (the ingest's own test, from
`atif._initial_candidates`: `schema_version` starts with `ATIF-`), and pushes
each through `atif._document_validation`. Parametrised per file, so the test id
*is* the fixture path. 24 tests.

Three properties worth defending in review:

1. **No allowlist.** The integrator's condition, and the reason the lease was
   extended. A fixture cannot be excused by editing test code.
2. **Real evidence is held to the same bar.** A second parametrised test runs
   the identical check over `research/evidence/runs/`. If a promoted trajectory
   ever failed, the validator — not the fixture — would be the thing to fix, and
   the two tests would disagree loudly instead of the suite grading fiction.
3. **Deliberate invalidity is declarable but cannot mute.** A fixture that
   exists to be refused carries, in the document,
   `"evallab_fixture_expectation": {"validation_status": "invalid",
   "error_contains": "...", "why": "..."}`. It is checked in both directions: a
   document claiming to be invalid must actually fail, and must fail with the
   error it names. Invented field names are reported *before* any declaration is
   consulted, so drift cannot be laundered as a deliberate refusal. Carrying an
   extra top-level key is safe: an ATIF document is a plain mapping to both
   validators, and every promoted trajectory already carries `evallab_redaction`
   the same way.

Field names in class A are banned **by name** as well as validated, because
validation does not reject them — it simply never reads them. That is exactly
how 58 observation results stayed invisible while every test passed.

### Proven to fail, then proven to pass

`proven live`, both directions:

- **Fails on drift.** Planted `tests/fixtures/explorer/jobs/job-drift/t1/agent/trajectory.json`
  carrying the exact pre-2026-08-16 shape. `uv run pytest
  tests/test_fixture_conformance.py` exited 1 with:
  `FAILED ...::test_every_atif_fixture_is_a_document_that_could_exist[tests/fixtures/explorer/jobs/job-drift/t1/agent/trajectory.json]`
  and the message
  `tests/fixtures/explorer/jobs/job-drift/t1/agent/trajectory.json: steps[0].observations is a field no ATIF document has; the real one is steps[].observation.results`
  — fixture and field, both named. Planted fixture removed; suite green again.
- **Declaration mechanism, three directions.** A declaration naming the real
  error passes; one naming the wrong error fails with `different refusal than it
  claims`; one on a document that now validates fails with `no longer proves its
  refusal is stale`. All three are committed as tests
  (`test_a_declared_refusal_is_accepted_when_it_still_happens` and the two
  following), so the guard's own logic is covered rather than merely applied.

## The 0 → 58 measurement

Over the nine promoted Codex trajectories in `research/evidence/runs/`:

| reader | observation results rendered |
|---|---|
| pre-#66 (`step["observations"]` only) | **0** |
| current | **58** |

All 58 also reach a tool-call row with `observation.provenance == "observed"`,
so they are rendered as content and not merely counted. `proven live` —
measured with `explorer.build_index` over the committed bundle. #66 closed the
reader; this PR removes the fixtures that concealed it, so the reader's
fixture-shape fallback (`explorer.py:402`, `step.get("observations")`) is now
**dead code against every committed document** — see F3.

## Findings — source defects, reported not fixed

`git status --porcelain src/` and `git diff --stat src/` are both **empty**. No
change under `src/evallab/`.

### F1 — `explorer.py:428` reads a derived column off a raw document (open)

The same defect class #66 fixed, and #66 did not finish it. The explorer takes a
tool call's exit code from `obs.get("command_exit_code")`. No ATIF document
carries that key; it is the derived projection column. A raw observation carries
`extra.exit_code`, which is what `atif.py:446-454` reads and what
`tests/test_truth.py:109` writes. **The explorer has therefore never shown an
exit code for any real trajectory**, and `test_explorer.py:80`
(`all(c.exit_code == 2 ...)`) passed only because the fixture invented the key.

Left broken and visible, per the integrator's condition 2:
`test_explorer_shows_the_exit_code_of_a_failing_command` is
`@pytest.mark.xfail(strict=True)` with the full diagnosis in its `reason`.
Strict, so the suite fails the moment `explorer.py` starts reading the real
field — the marker cannot rot into a permanent excuse. Beside it,
`test_the_fixture_records_exit_codes_where_the_validated_shape_puts_them`
asserts via `atif.project_trial` that the conformed fixture really does carry
all four exit codes (`[("L0",2),("L1",2),("L2",2),("L3",2)]`), so the xfail is
provably about the explorer's reader and not about missing data.

Suggested fix (Platform, one line): read
`(obs.get("extra") or {}).get("exit_code")` — better, call
`atif._command_exit_code(obs)` so the three accepted spellings stay in one
place. Not applied; outside this lease.

### F2 — `atif.project_trial` raises on a relatively-pathed `JobRecord` (minor, open)

`load_job(Path("tests/fixtures/explorer/jobs/job-fail"))` followed by
`project_trial` raises
`ValueError: '...trajectory.json' is not in the subpath of 'tests/fixtures/explorer/jobs/job-fail/t1'`
at `atif.py:466`. `_initial_candidates` resolves candidate paths but `trial.path`
is left as given. Not reachable through `load_jobs`, which resolves in
`discover_job_dirs` (`results.py:235`), so this is a direct-caller trap only.
Worked around in the new test with an explicit `.resolve()` and a comment.

### F3 — `explorer.py:402` fixture-shape fallback is now dead (cleanup, open)

`_observation_results` reads `step.get("observations")` "because a bare
`observations` list is the older shape still present in
`tests/fixtures/explorer`". After this PR no committed document in the
repository uses it, and the guard prevents one returning. The fallback — and the
`call.get("function")` branch at `explorer.py:454-462` — can be deleted, which
would make the explorer read exactly one shape. Deliberately not done here: it
is a `src/` change, and deleting it *before* this PR merges would break the
suite it currently supports.

## M006 fail-closed guarantees — condition 1, verified intact

Required to be stated explicitly:

- `tests/test_analysis_worker.py::test_default_worker_stays_fail_closed_for_live_analysis`
  (line 1063) **passes**; the whole file passes (53 tests).
- The calibration gate is **still hardcoded closed**:
  `src/evallab/analysis_worker.py:1002` → `"calibrated_judges_only": lambda: False,
  # fail closed: no measured pass`.
- The default adapter is **unchanged**: `src/evallab/analysis_worker.py:1009` →
  `adapter=adapter or _no_adapter`, with `_no_adapter` defined at line 657.
- No assertion protecting either property was relaxed. Nothing in
  `tests/test_analysis_worker.py` was edited at all — the three conformed
  fixtures under `tests/fixtures/analysis_worker/` needed no test change, which
  is itself evidence that file asserts behaviour rather than shape.

## Verification

- `uv run pytest` — **695 passed, 1 xfailed** (the F1 marker), 0 failed.
- `uv run ruff check .` — **All checks passed!**
- `uv run pytest tests/test_fixture_conformance.py` — 24 passed.
- Shared catalog unchanged: `docker exec eval-lab-postgres-1 psql -U evallab -d
  evallab -tAc "select (select count(*) from jobs), (select count(*) from
  trajectory_documents);"` → `72|23`, both before and after.
- No paid agent executed. No `codex`, no `claude-code`, no cloud sandbox, no
  API-key environment variable, no `launchctl`, no `docker compose`. The primary
  checkout was read only — never written, never `git pull`/`reset`/`checkout`.
- `scripts/premerge.sh` and project-wide formatters deliberately **not** run,
  per the mission brief.

## Provenance of the conformed fixtures

Shapes were derived from the real committed documents, not imagined: the
envelope (`schema_version`, `agent.{name,version,model_name}`, per-step
`timestamp`/`model_name`, `observation.results[].{source_call_id,content}`) was
read out of
`research/evidence/runs/canary-transaction-reconciliation-codex-20260815/transaction-reconciliation__frxRezo/agent/trajectory.json`
and the Harbor 0.21 capture at
`research/explorations/harbor-021/fixtures/trajectory.json`. Payloads stay
synthetic and tiny (`"m"`, `"1 passed"`, `"make: *** [build] Error 2"`): **no
prompt text and no content underlying an `<<evallab-redacted:` marker was
copied**, and no fixture grew past what its tests read.

`session_id` values are now UUID-shaped, matching real captures, instead of
`s1`/`s2`/`s3`. Explorer fixtures moved to `ATIF-v1.7`, the version Harbor 0.21
actually emits; `analysis_worker` and `operability` stay at the equally
supported `ATIF-v1.6`, so both versions remain exercised and both are real.

One honest caveat on `extra.exit_code`: **no committed real observation carries
an exit code in any form** — all 58 hold exactly `source_call_id` and `content`,
because the Codex adapter records none. `extra.exit_code` is attested by
`atif.py:446-454` and `tests/test_truth.py`, i.e. by what the real code reads
and writes, not by a captured document. That is the criterion the mission set
("the shapes the real code validates"), and it is the only spelling in the
repository that any consumer reads. Stated rather than glossed.

Run-time documents built inside tests are intentionally outside the guard:
several exist to drive degradation paths (a step with no `message`, a
trajectory that is not JSON). `write_trial` in `tests/test_explorer.py` now
emits the real envelope so only the deliberately broken part of such a document
is fictional, and the guard's docstring records why.
