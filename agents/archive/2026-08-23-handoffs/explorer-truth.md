Status: done
Last: merged as PR #66 (`7456ac8`)
Next: none
Blockers: none

# EXPLORER-TRUTH

Status: review-wanted
Last: both defects fixed with negative controls run; full suite + ruff clean; PR opened
Next: integrator review and merge; the two architectural questions below need Peter/integrator, not a worker
Blockers: none

Branch `role/explorer-truth`, worktree `.worktrees/explorer-truth`, off `origin/main`
at `fa11f18`. Lease as briefed: `src/evallab/explorer.py`, `dashboard/explorer.py`,
`dashboard/app.py`, `dashboard/queries.py`, `dashboard/tests/`,
`tests/test_explorer.py`, `docs/run-explorer.md`, this file. `src/evallab/cli.py`,
`queue.py`, `facts.py`, `status.py`, `projection.py`, and `policy/` untouched.

## Defect 1 — withheld evidence rendered as if present

**Three-state presentation chosen.** `Provenance` gains a fifth label,
`withheld`, beside `observed`/`derived`/`draft`/`unavailable`
(`src/evallab/explorer.py:51`). Reusing the repository's existing label
mechanism rather than adding a parallel one means every surface that already
renders a `Labeled` gained the distinction for free, and the states map onto
what a reader actually needs to know:

| State | Label | Carries |
|---|---|---|
| content available | `observed` 🟢 | readable chars / bytes |
| content withheld by redaction | `withheld` 🔒 | `withheld_bytes` + every marker's `sha256`, plus chars still readable |
| content genuinely missing | `unavailable` ⚪ | the reason (absent field, empty, unresolvable) |

Applied at four render points, not one:

1. **Steps.** `TrajectoryView.steps` was `tuple[dict]` holding only
   `{step_id, source, n_tool_calls}` — the envelope, identical whether the text
   was present or removed. It is now `tuple[StepRow]` with a `message: Labeled`.
   Proven live: step 1 of `terminal-bench-html-js-filter__5rgjEEt` renders
   `🔒 withheld 4876 bytes (sha256:6866d85ebcbb…)`; step 6 renders
   `🟢 readable · 283 chars`.
2. **Trial headline.** `TrajectoryView.redaction` states it before any table:
   `5 of 21 step messages were removed before promotion (10394 bytes)`.
3. **Citations.** `CitationResolution` gains `content`. **Resolution and
   readability are now separate questions** — a citation into a redacted prompt
   still resolves correctly, and that is exactly why resolution alone could
   never separate the two. `citation_state()` returns
   `unresolved` ⛔ / `withheld` 🔒 / `absent` ⚪ / `readable` ✅.
4. **Artifacts.** Read from `PROMOTION.json`, which is an exact per-file
   manifest, so nothing is inferred from file names or sizes. This surfaced two
   cases the brief did not mention and I found by running the system:
   - `verifier/test-stdout.redacted.txt` is **107 bytes of a 77,080-byte
     original** (rule R3). The artifacts table showed `107 bytes` and nothing
     else — it read as a complete, very short verifier log.
   - A **194,005-byte raw rollout** (`agent/sessions/…/rollout-*.jsonl`) was
     removed from the bundle entirely (rule R2). It appeared in **no** surface,
     because it is not a file any artifact list can enumerate. Now reported as
     `TrialView.omitted_files` with its original size and digest.

**Presentation lives in `src/`, deliberately.** Streamlit is intentionally not a
project dependency, so `dashboard/*.py` cannot be imported by the test suite —
which is why the page's behaviour was previously unassertable. The wording of
every state is now `evallab.explorer.content_summary` / `citation_state`, and
`dashboard/explorer.py` is a thin map from those strings to a glyph. That turns
"a withheld step never reads like a verbatim one" into a tested guarantee.

### Not fixed, deliberately: `steps.parquet` has no message column

The brief notes this. `src/evallab/facts.py` is outside my lease, so the
`steps.parquet` schema is untouched. Anything reading the Parquet — not the
explorer — still cannot tell withheld from verbatim. Recommend a follow-up
lease adding a per-step `message_state` + `withheld_bytes` + `message_sha256`
column; the marker parsing to reuse is `explorer._markers` / `content_label`.

## Defect 2 — F-04, nested `jobs_dir`

**Scope narrowed on the Integrator's steering mid-mission, and I agree with the
narrowing on evidence.** I had first implemented a bounded depth-agnostic walk.
I reverted it, because:

`harbor/viewer/scanner.py:50` and `:86` scan **exactly two levels**
(`jobs_dir.iterdir()` for jobs, `job_dir.iterdir()` for trials). So Harbor's
browser has the identical blind spot, and the executor writes exactly that shape
(`runner.py:601`). A deeper private walk in this explorer would have produced a
surface that silently disagrees with both Harbor and the writer — a second
convention, which `AGENTS.md` forbids and which nobody decided.

What shipped instead:

- **The phantom job is gone.** Root cause was `_is_trial_dir` treating *any*
  directory with a `result.json` as a trial, so a nested job directory was read
  as a *trial of its parent*. Concretely, before the fix
  `runs/nightly/2026-08-16/job-pass/` produced a trial keyed
  `2026-08-16/job-pass` whose trajectory was `unavailable: missing
  trajectory.json` — a fabricated trial named after a real job. `_is_job_result`
  now identifies a job roll-up positively by `n_total_trials` + `stats`, the same
  test `results.discover_job_dirs` already uses.
- **The mismatch is loud and actionable.** A directory that is not a job is
  named, with the run below it, its trial count, and the jobs root that *would*
  find it:
  `nightly/ is not a job directory, but a run exists below it —
  nightly/2026-08-16/job-pass (1 trial). … To see it, add a jobs root at
  nightly/2026-08-16 …`. The test asserts the named remedy actually works.
- **An unlinked analysis says why.** This was the symptom a human actually saw.
  `AnalysisView.link` is now `unavailable` with `source trial <id> was not found
  among the N trials discovered under the configured jobs roots`, plus an index
  note. Previously `trial_key` was silently `None` and `index.notes` was empty.

**Root cause is upstream and outside every lease here:** `ExperimentSpec.jobs_dir`
is a free-form string (`schemas.py:27`) while every reader assumes
`<jobs-root>/<job>/<trial>`. Either constrain the field to a single path segment
or make both viewers agree. That is a queue/schema decision.

## Leaderboard honesty check (no statistic changed)

`—` in pass@1 meant "trials ran, none scorable" and was indistinguishable in
isolation from "no data". `leaderboard()` now also returns `scorable` and
`unscored_no_reward`; `pass_rate`, `ci_95_low`, `ci_95_high` are byte-identical.
`dashboard/app.py` adds a `pass@1 basis` column
(`unscorable — 3 trials, 2 raised an exception and 1 recorded no reward`), renders
the CI as `not defined` instead of `— – —`, and the caption now defines `—`. The
empty table says "no catalog trials are indexed yet — no data, as distinct from
unscorable data."

## Other places a surface implied more than the data supports

1. **Observations were invisible.** `_trajectory_view` read
   `step["observations"]`, a list shape that exists **only** in
   `tests/fixtures/explorer` — ATIF validates `step.observation.results`
   (`atif.py:299`) and all nine promoted trajectories use it. So the explorer
   saw **0 of 58** real observation results while its tests passed on a fixture
   that does not match reality. Fixed with one reader handling both
   (`_observation_results`); the fixture itself is outside my lease and I did not
   touch it. **The fixture drift is worth a follow-up** — it is a test suite that
   cannot fail on the real data shape.
2. **`exit_code` is structurally always `None` on real evidence.** 0 of 58
   observation results record `command_exit_code`. Not misleading, but the `exit`
   column is dead weight; captioned rather than removed, since removing a column
   is a UI decision.
3. **Job identity is ambiguous across roots.** Two jobs with the same name in
   different roots collide on `trial_key`; the second was skipped with a bare
   note. The note now names both directories. `JobView.jobs_root` was added so a
   reader can tell a live run from promoted evidence — the page rendered neither.

## Verification

| Check | Result |
|---|---|
| `uv run pytest` | **proven live** — 580 passed, 0 failed |
| `uv run ruff check .` | **proven live** — all checks passed |
| `uvx ty@0.0.71 check src/ --output-format=concise` | **proven live** — 28 diagnostics, ratchet is 33; 0 in `explorer.py` |
| Streamlit page renders | **proven live** — `AppTest.from_file("dashboard/explorer.py")`, `at.exception` empty, against real `research/evidence/runs` |
| Withheld citation renders 🔒 | **proven live** — demo root citing step 1 of a promoted trial rendered `🔒 … withheld 4876 bytes (sha256:6866d85ebcbb…)` beside `✅ … readable` for step 6; scratch under gitignored `runs/`+`derived/` removed afterwards |
| Catalog unchanged | **proven live** — `72 jobs`, `23 trajectory_documents` after all work |
| Corpus measurements reproduced | **proven live** — 116 steps, 49 withheld (42.2%), 92,592 bytes, all `system`/`user`, 58 tool calls, 58 observation results, 0 exit codes |

### Negative controls (each fix reverted in place, then restored)

| Control | Reverted to | Result |
|---|---|---|
| 1 | step `message` = envelope only (`observed(None)`) | 5 tests **fail**: `withheld_step_never_renders_like_a_verbatim_one`, `citation_into_a_withheld_step_is_marked_withheld`, `promoted_codex_evidence_reports_its_withheld_bytes_and_digest`, `rendered_summaries_of_the_three_states_are_all_different`, `citation_states_separate_withheld_from_readable_and_missing` |
| 2 | original two-level walk + `_is_trial_dir` = any `result.json` | 3 tests **fail**: `nested_jobs_dir_run_is_named_with_its_location_not_dropped` (`assert ['nightly'] == []`), `job_roll_up_result_is_never_counted_as_a_trial`, `directory_with_no_trials_is_reported_rather_than_rendered` |
| 2b | silent unlink (`link = observed(None)`, no note) | `analysis_whose_source_trial_is_not_indexed_says_why` **fails** |
| 3 | `scorable=True`, `unscored_no_reward=0` | `unscorable_cohort_is_distinguishable_from_no_data` **fails** |
| 4 | `PROMOTION.json` ignored | `redacted_artifact_states_how_little_of_it_survived`, `files_promotion_removed_entirely_are_still_reported` **fail** |

No paid agent was invoked. No `codex`, no `claude-code`, no queue submission, no
`docker compose`, no LaunchAgent touched, no write to the primary checkout, no
write to the shared catalog.

## Requested opinion: what this repository's surfaces uniquely provide

Evidence first. Harbor 0.21.0 at `~/.local/share/uv/tools/harbor`:
`grep -rc "evallab-redacted" <harbor package>` returns **zero matches**, and
`viewer/server.py:2449-2469` serves the raw `trajectory.json` straight through.

**Where I agree with your reading.** Policy and spend state, cohort statistics
with intervals, canary drift against a rolling baseline, calibration history,
and DISCOVERIES are facts about the lab's judgement. Harbor cannot produce any of
them, because none exist in a job directory. Your framing — facts about the lab's
judgement vs facts about a job — is the right axis.

**Where I'd sharpen it, on redaction specifically.** "Harbor will render a
redacted step with no indication either" is not quite right, and the correction
matters. Harbor serves the trajectory verbatim, so a reader in Harbor's step view
sees the literal string `<<evallab-redacted: 4876 bytes, sha256:...>>` — crude,
but visible. **The lab's explorer was the surface that hid it**, precisely because
it rendered only envelopes and dropped `message` entirely. So the honest claim is
not "only we can show redaction"; it is "we uniquely can *account* for it":
the 42.2%/92,592-byte rollup, the 77,080→107-byte verifier truncation, and the
194 KB rollout that is in no file at all. Harbor cannot show the last one under
any circumstances — it enumerates files that exist.

**Where I disagree, or would go further than you.**

- **Citation resolution is the strongest unique value, and it is not on your
  list.** Verifying that a model's claim points at a step and tool call that
  actually exist — and now, that it points at something readable — requires
  joining a sidecar to a trajectory. Harbor has no concept of an analysis. This
  is the one thing in `dashboard/explorer.py` I would defend without
  qualification.
- **`ArtifactLink`, the trials table, the jobs expander, and the trajectory
  tool-call table do not earn their keep.** They are a worse `harbor view`. The
  jobs expander in particular renders three fields Harbor renders with 15
  columns, sorting, and search.
- **Two things do earn their keep and are easy to miss.** The path jail (task
  `tests/`/`solution/` are never listed or read, so a viewer cannot leak a
  verifier answer key) and outcome classification — `infra-exception` is held
  strictly apart from `reward-failure` and its reward renders `unavailable`,
  never `0`. Harbor's browser shows an error count; it does not encode "this is
  evidence about the harness, never about the model." Whatever the division of
  labour becomes, those two must not be lost.
- **`next_actions_for_*` is a genuine third category:** the *lab's* safe next
  command (oracle/nop control, `analyze plan`, `submit`/`approve` under Peter's
  ceilings). Harbor cannot know the lab's policy.

**My recommendation, for you to decide with the Sponsor, not for a worker.** If
the explorer is reduced to what only it can do, it is an *analysis and provenance*
surface, not a browser: citations with resolution + readability, redaction
accounting, outcome classification, the jail, and Next Action. Everything
job-shaped goes to Harbor. That is a bigger change than a bug fix, so I did not
start it, did not delete anything, and — as instructed — added no link-out to
`harbor view`. Note the ordering constraint: **the F-04 root cause survives that
change**, because Harbor's scanner has the same two-level limit, so handing job
browsing to Harbor does not fix nested `jobs_dir` — only constraining
`ExperimentSpec.jobs_dir` does.
