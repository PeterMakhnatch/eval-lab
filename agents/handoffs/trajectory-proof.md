Status: review-wanted
Last: closed the trajectory hop over promoted Codex evidence — trial terminal-bench-html-js-filter__5rgjEEt (21 steps, 15 tool calls, reward 0.0) ingested into throwaway DB evallab_trajproof, stage-5 sidecar 1687ce14-e4b0-43cf-ac4e-a735c8d14a50 produced through the saved-response stub with source_digests.trajectory sha256:f112fc24… (M009 had null), all 6 citations resolving in the validator, in the catalog+Parquet join and on the run explorer, negative control refused 3/3; evallab trace --dry-run EXIT=0 with 34 spans and an AGENT root; shared evallab catalog still reports 23 trajectory_documents; rebased onto origin/main 8f3ee8c after PR #60 merged and re-verified — 565 passed, ruff clean
Next: independent review of PR "ANALYSIS: close the trajectory hop over promoted Codex evidence (F-05)" — the two judgement calls needing a second opinion are the split verdict on the trajectory->analysis hop (machinery `proven live`, model authorship `blocked`) and the partial refutation of M009's span-join claim (no spec_id/job_id/trial_id, but session.id bridges to trajectory_documents.session_id, 1:1 in practice and unenforced); do not merge from this role
Blockers: none

# TRAJECTORY-PROOF handoff

Agent/model: Claude Opus 4.5 (Anthropic), Oh My Pi harness, subscription-only.
Worktree `.worktrees/trajectory-proof`, branch `role/trajectory-proof`, from
`origin/main` = `972866d`.

Lease: `research/analysis/`, `docs/checkpoints/2026-08-16-trajectory-hop-proof.md`,
this file. Nothing else was touched — no `src/`, `tests/`, `policy/`, `library/`,
`research/evidence/`, `research/experiments/`, `dashboard/`, `scripts/`.

## What this mission answered

**Do citations into a real trajectory resolve end to end? Yes.** The full record
with every command and exit status is
`docs/checkpoints/2026-08-16-trajectory-hop-proof.md`. Summary:

`task -> run -> evidence -> ingest -> facts -> trajectory -> analysis -> operator
surface` is **`proven live` over real agent behaviour**. The hop M009 called
`blocked` (`facts -> trajectory`) is closed: `trajectory_documents` holds three
valid ATIF-v1.7 rows and
`experiment_trial_analysis_path.trajectory_document_id` is non-NULL for the first
time. The `trajectory -> analysis` hop splits: the **machinery** is `proven live`,
**authorship of a finding by a model** stays `blocked`.

## Identifiers

| | |
|---|---|
| Job | `canary-terminal-bench-html-js-filter-codex-20260815`, `jobs.id 03c50e09-d16f-4058-93b9-893bb9cae9da` |
| `experiment_id` | `01M021T5QYSJKEQV0AVH1WDBJC` |
| Trial | `terminal-bench-html-js-filter__5rgjEEt`, `trials.id 1e40baab-3f5b-4030-89a0-439c25638328` |
| Steps / tool calls / reward | 21 / 15 / **0.0** |
| ATIF | `ATIF-v1.7`, `session_id 01a00428-7bb4-7b30-a87c-c6e43aa9f6a2`, `sha256:f112fc24…3075a2`, **redacted** under rule R1 |
| `trajectory_documents.id` | `2bc076460b8ebd69529224d55f735713753c9156e27f796b094b9f7e97fb3b46` |
| `analysis_id` | `1687ce14-e4b0-43cf-ac4e-a735c8d14a50` (`validation_status: valid`) |
| `review_id` | `e1ed512d-7dd3-4de6-954f-03ec015d2b1c` (`accepted`) |
| Throwaway DB | `evallab_trajproof` on `eval-lab-postgres-1` |

## Shared resources

- Shared `evallab` catalog: **read-only, unchanged.** `trajectory_documents` =
  **23** before and after. Every write went to `evallab_trajproof`.
- `research/evidence/` byte-identical (`git status --porcelain research/evidence/`
  empty). Nothing promoted into it.
- Nothing written to the primary checkout, including its `derived/parquet`
  (`find ~/Developer/eval-lab/derived -newermt "2026-08-16 17:00"` empty). This
  needed care: from a linked worktree `evallab ingest` and
  `evallab trajectories --export` default their Parquet root to the **primary**
  checkout by design (`paths.py`, `shared_checkout_root`). Both invocations passed
  explicit `--derived-dir` / `--output-dir`. Noted in
  `research/analysis/README.md` so the next worktree writer does not trip on it.
- No paid model call, no cloud, no deploy, no Phoenix POST, no `docker compose`,
  no API-key environment variable, no change to `policy/`. Only `analyze plan`,
  `analyze stub` (saved response) and read-only probes ran.

## Findings worth a reviewer's attention

1. **Redaction resolves but does not evidence.** A citation into a redacted
   system step resolves *identically* to a verbatim agent step on every surface,
   because every surface consumes only the step envelope (`step_id`, `source`,
   counts) and never message text. Step 1 yields 0 readable characters where 4876
   bytes were withheld; step 6 yields 283 verbatim characters plus a verbatim
   `exec` call and its observation. Corpus-wide: **49 of 116 steps (42.2%)
   redacted, all system/user, 92592 bytes withheld; 0 of 58 tool calls and 0
   observations redacted.** So the promoted corpus is `proven live` for
   agent-behaviour analysis and `blocked` for instruction-content analysis — and
   no surface warns the reader of the difference.
2. **M009's span-join claim: premise confirmed, conclusion partially refuted.**
   The converted OTLP payload carries 4 resource and 14 span attribute keys and
   **no** `spec_id`, `job_id`, or `trial_id` — confirmed, and it cannot, since
   `harbor-atif2otel` 0.1.0 converts the ATIF document alone. But `session.id`
   = `trajectory_documents.session_id` is a real bridge into the research graph
   (23/23 populated, 23 distinct in the shared catalog, **no unique
   constraint**). Nothing in the repository uses it. Making it first-class is a
   Platform/Research call.
3. **`trace --dry-run` never exited 1 for a missing trajectory** — then or now.
   M009's `EXIT=1` transcripts both omit `--dry-run`; `cli.py:1102-1106` returns
   1 only on a *failed* conversion or on "nothing shipped and not a dry run", and
   a trajectory-less trial is `skipped`, never `failed`. The refusal M009 saw is
   real and reproduces; its exit code came from the shipping path.
4. **The brief's "126 steps" is wrong; the observed total is 116.** `evallab
   trajectories` summed over all 11 trials: `trials=11 steps=116 tools=58`. The
   58 tool calls figure is correct.
5. **The stub disagrees with the draft label, visibly.** `evallab analyze
   agreement` matched the sidecar to
   `research/calibration/trajectory-labels/terminal-bench-html-js-filter__5rgjEEt.json`,
   digested both, and reported `exact_match: false` — stub
   `verification_behavior` vs draft `implementation`. That is a Research-lane
   judgement on a draft versus a stub, not settled here. No taxonomy label is
   cited as ground truth anywhere in the checkpoint; all nine remain
   `review_status: draft_pending_research_review`.

## Known defects encountered — recorded, not fixed

- **F-01 — no CLI stages an analysis request.** `analyze worker-plan` computes
  `request_id 3f6fd86fc7c07134` and calls the trial `eligible`; `analyze
  worker-run-one 3f6fd86fc7c07134` then exits **2** with a bare
  `FileNotFoundError` for `derived/analyses/worker/requests/…/request.json`.
  `AnalysisWorker.stage()` has two callers, both inside the full `nightly` cycle.
  **Obstruction:** the worker path — the only route to a model-authored finding,
  and the route that would have exercised M006's calibration gate — is
  unreachable from a role worktree. Routed around with `analyze stub`.
  **Consequence:** hop 7b is `blocked` and the gate refusal is recorded as
  designed behaviour rather than observed, because I could not reach the gate.
- **F-04 — the explorer silently loses nested jobs roots.** Reproduced as an A/B:
  flat root `research/evidence/runs` gives `jobs=5 trials=11`, `trial_key` linked,
  **0/6** citations unresolved; one level up gives `jobs=1` (named `runs`),
  `trials=5`, `trial_key=None`, **6/6** unresolved, and `index.notes` **empty**.
  **Obstruction:** none, because `research/evidence/runs` is already the flat root
  `dashboard/explorer.py` hardcodes. Worth naming anyway: it is the cheapest way
  to make this mission's positive result look false, with no warning.

Both belong to a future mission. Neither was touched.

## Verification

- `uv run pytest` — **565 passed** on the rebased head (`8f3ee8c` + this commit).
  545 passed on the pre-rebase base `972866d`; PR #60 added the other 20.
- `uv run ruff check .` — **All checks passed!**
- No project-wide formatter, no `scripts/premerge.sh`, per mission constraints.
- Shared catalog re-checked after all work: **23**.

## Artifacts

Committed: `research/analysis/stub-codex-html-js-filter-analysis.json` (the
hand-authored saved response, with provenance in `research/analysis/README.md`),
the checkpoint, this handoff.

Not committed, per `research/analysis/README.md` ("Every generated table,
comparison, and analysis sidecar lives under ignored `derived/`"): the sidecar and
its review, the 25 Parquet partition files, the agreement report, and the 21
transcripts plus six read-only probe scripts under `derived/proof/`. The sidecar is
reproducible from the two committed inputs; only `analysis_id` and `created_at`
differ per invocation. The throwaway database `evallab_trajproof` is disposable.
