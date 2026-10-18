# Research findings and evidence

## Purpose
`research/` is the durable repository for knowledge produced by eval-lab. It stores
experiment matrices, calibration ground truth, capability explorations, analytical
queries, and reviewed, immutable evidence bundles.

## What lives here / entry points
- `research/experiments/`: Experiment matrices, campaign manifests, and execution configurations (e.g. `research/experiments/local-controls.json`).
- [Reef continuous-improvement program](experiments/reef/README.md): current studies, results, and scoped contributions to Eval Lab and Reef.
- [Card → folder → result index](CARD-INDEX.md): which HAR card lives in which `explorations/`/`experiments/` folder and where its principal result is.
- `research/evidence/runs/`: Reviewed, immutable promoted control and trial bundles (e.g. `research/evidence/runs/event-summary-oracle-evidence/result.json`).
- `research/calibration/`: Judge ground truth, test rubrics, calibration corpora, and evaluator answer keys.
- `research/analysis/`: Reusable analysis scripts, rubric definitions, and queries.
- `research/cards/`: Evaluated run and cohort evaluation cards (e.g. `research/cards/TEMPLATE.md`).
- `research/lessons.md`: Synthesis of learnings and operational observations.

## Invariants or rules
- Provenance requirement: Every research artifact must cite its source data, runs, or corpus digest (cited in `agents/STRUCTURE.md`).
- Immutable evidence: Run evidence promoted to `research/evidence/runs/` is immutable (cited in `AGENTS.md`).
- Analysis denominator: Metrics and rates are computed only over the featured analysis corpus (`status = 'featured'`) with explicit denominators (cited in `docs/NOW.md`).

## Tests or checks
- `uv run pytest tests/test_cards.py`
- `uv run pytest tests/test_campaigns.py`
- `uv run pytest tests/test_external_corpus.py`

## What not to add here
- Do not store unreviewed, ephemeral execution scratch or raw Harbor runs here; use `runs/`.
- Do not add unverified capability claims without backing evidence or control runs.
- Do not store platform implementation code or task definitions here; use `src/` or `library/`.

## Held-out v7 retained-submission replay

`experiments/heldout-v7/regrade.py inventory --inputs <source-inputs.json> --out <inventory.json>`
binds all four canonical arms, native job/trial identities, full frozen packages and exact kept-diff bytes without execution. Missing native trials and missing/empty diffs remain distinct; corrected canonical nulls never inherit a raw reward.

Use `regrade.py run --help` for the native campaign. Execution requires a released original33 v7 manifest, its explicitly Verifier-accepted exact SHA, the independent receipt, and retained native cost calibration. The cumulative $3 journal and exclusive lease live alongside the bound source-input configuration, not in a new per-invocation work directory. Reservations survive failed calls and restarts; they are estimates, not provider bills or a native billing tag.

Outputs are `report.json` and QA Trace's `regrade-v7.json`. Primary B–C, B–Base and C–Base use one identical qualified attempt-1 task cohort, exact sign tests and paired-task bootstrap difference intervals. Full B2 comparisons are secondary; diagnostics and quarantined canaries never enter primary rates. `real_pass`/`wrong_pass` describe original-v6-pass grading correctness, not shortcut behavior or intent.

Explicitly authorized pre-release measurement uses `regrade.py plan-staged` then `run-staged`. Plans freeze the original33, repairs, controls, packages and kept diffs. Changed graders need native replay; identical graders inherit only canonical measured binary rewards. Nulls stay unmeasured and historical agent exceptions stay visible.

Staged labels are provisional, noncanonical and noncitable. `promote` still requires the exact independent release and matching grader/package/control/source-diff bindings. Staging and canonical execution share one cumulative allowance; restarting never refreshes it.

## Fresh repaired-data native generation

`experiments/repaired-data-sft-v2/control_admission.py` audits typed native controls and immutable candidate records. Its campaign profile admits fresh generation only: no fabricated ledger ancestry, candidate-status promotion or global admission-policy change.

For the bounded 2026-10-18 campaign, `native_generation.py plan --config <settings> --out runs/<campaign>` materializes nineteen exact repaired train packages and seventy-six distinct base cells. Run `admit --plan <plan> --envelope <budget>` before paid `run`, which also requires a hash-bound authenticated serving receipt and time for both sandbox TTLs.

The shared envelope separates serving, native cells and replay, with explicit Sandbox prices and image/provider buffers. Unknown billing extras are acknowledged; reservations are not provider invoices. Cells have hard CPU/RAM limits, provider TTLs, one SDK create call, no transient retry and no reservation refund.

`collect` admits only actual repaired passes with generation-time diff, effective lock interval, exact model/control/package binding and faithful full-conversation custody. Held-out or answer-visible tasks, historical rows, incomplete evidence and duplicate executions cannot inflate fresh counts.

Fifty-one qualifying fresh rows is a minimum-count gate, not automatic training approval. Token-matched control construction and an admitted training budget remain separate requirements.

