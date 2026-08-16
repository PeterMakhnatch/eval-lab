# Run & analysis explorer

M005 (Platform). Logic: `src/evallab/explorer.py`. Page:
`dashboard/explorer.py`. Tests: `tests/test_explorer.py` (fixture-driven plus
the committed promoted bundle; zero host state). Fixtures:
`tests/fixtures/explorer/`.

## What it answers

Select a task, job, trial, trajectory, or analysis and see: what ran
(agent/model/config), what happened (reward, exception, timing, cost,
artifacts), why it was classified (analysis sidecar with citations resolved
to the actual step and tool call), and the exact safe command to act next.

```bash
uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py   # repo evidence
EVALLAB_EXPLORER_ROOT=tests/fixtures/explorer \
  uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py # fixture demo
```

## Contracts (tested)

- **Read-only.** Building the index twice leaves the evidence byte-identical
  (`test_index_build_performs_zero_writes`). No page control mutates state.
- **Provenance on every field**: `observed` (read from evidence), `derived`
  (computed here, with the rule named), `draft` (unreviewed model output —
  all analysis conclusions render this way until reviewed), `withheld`
  (removed on purpose before promotion, with the byte count and sha256 of the
  original), `unavailable` (missing/malformed, with the reason).
- **Withheld evidence never reads as present.** Promotion replaces the text of
  every `system`/`user` step with
  `<<evallab-redacted: N bytes, sha256:...>>`, rewrites oversize verifier
  strings, and drops raw rollouts entirely
  (`scripts/promote_codex_bundle.py`, rules R1/R3/R2). Three states are
  distinguished wherever a step, citation, or artifact is rendered —
  🟢 readable, 🔒 withheld, ⚪ absent — and the withheld state carries the byte
  count and digest so the claim stays auditable. Measured on the committed
  bundle: 49 of 116 steps withheld (42.2%), 92,592 bytes, all `system`/`user`;
  0 of 58 tool calls and 0 of 58 observations.
  A citation into a withheld step still *resolves* — resolution answers "does
  this exist", content answers "can a human read it", and collapsing the two
  is the defect this page was fixed for
  (`test_withheld_step_never_renders_like_a_verbatim_one`).
- **Infrastructure exceptions ≠ reward failures.** Exception trials render in
  their own section; their reward is `unavailable`, never 0.
- **Citations are verified, not trusted.** Each analysis citation resolves
  against the trial: file exists inside the jail, step exists in the
  trajectory, and the tool call belongs to that exact step — or it renders ⛔
  with the reason. An analysis whose source trial is not indexed says which
  trial id it wanted and how many trials were searched, instead of a bare
  `unlinked`. Duplicate source trial IDs leave analyses unlinked, with the
  reason.
- **Jobs are addressed as `<jobs-root>/<job>/<trial>`** — the shape the
  executor writes (`runner.py:601`) and the only shape Harbor's own viewer
  scans (`harbor/viewer/scanner.py:50,86`). `ExperimentSpec.jobs_dir` is
  free-form (`schemas.py:27`), so a run can land where neither looks. The
  explorer deliberately does not search deeper — a private convention here
  would disagree with every other reader of the same directories — but it
  names the directory, the nested run below it, its trial count, and the jobs
  root that would find it, instead of rendering an empty job (F-04,
  `test_nested_jobs_dir_run_is_named_with_its_location_not_dropped`). A job's
  own roll-up `result.json` is never counted as a trial.
- **Registration is explicit.** The repository view reads
  `library/registry`; absence there is observed as `not registered`. Fixture
  roots without a registry label registration unavailable.
- **Path jail.** `..`, absolute paths, and anything under task `tests/` or
  `solution/` resolve to refusal; artifact links are trial-relative only.
- **No secrets.** Key-shaped names in any rendered mapping are `[redacted]`.
- **Cold start navigable.** Missing roots, malformed sidecars, absent
  trajectories, and duplicate trial keys degrade into labeled notes.
- **No ranking of incomparable cohorts.** The explorer renders one entity at
  a time and never aggregates across agents/models; comparisons stay in
  `evallab compare`, which enforces cohort declarations.

## Where the presentation lives

Streamlit is intentionally not a project dependency, so `dashboard/` cannot be
imported by the test suite. The wording of every content state therefore lives
in `evallab.explorer.content_summary` / `citation_state`, and the page is a thin
map from those strings to a glyph. That is what makes "a withheld step never
reads like a verbatim one" a tested guarantee rather than a claim about a page.

## Next Action (emits, never executes)

Task → oracle/nop control commands. Trial → `harbor view <jobs-root> --jobs`
(the folder shape Harbor actually accepts) and
`evallab analyze plan <trial-dir>`; infra exceptions add a status re-check at
the repository/scratch root. Queue → `evallab submit` / `evallab approve`
(policy ceilings still apply). Every path is shell-quoted, placeholders are
non-executable literals, and the explorer holds no executor.
