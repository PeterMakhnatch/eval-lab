# Calibration ground truth (EVIDENCE)

This directory is the lab's sealed ground truth. It is the only tree the
EVIDENCE role writes. Brief 09 (`evallab calibrate <family>`) is BUILDER
work and does not exist yet; this README is the consume contract that brief
must implement. Do not copy any file from here into a task `environment/`.

## Layout

```text
calibration/
  README.md                          this contract
  HANDOFF.md                         role protocol
  inventory.py                       shipped walker used by audits and by calibrate
  agreement.py                       shipped per-criterion comparison
  rubrics.py                         frozen CR/AQ/EF criterion names
  <family>/
    corpus.json                      document id, path, variant, provenance
    *.md                             labeled postmortems (no keys inline)
    answer-keys/<id>.json            sealed expected verdicts
  trajectory-labels/<trial_name>.json
```

Families in this corpus: `checkout-pool-exhaustion`, `retry-storm-backlog`.
Criterion names are taken from the read-only judged-output tasks, not reinvented.

## What `evallab calibrate <family>` consumes

`evallab calibrate <family>` scores **one family**. It must use only the
artifacts named below.

### 1. Labeled documents (input to the judge)

Locate the family's documents by calling
`calibration.inventory.iter_family_documents(family)` (or by reading
`calibration/<family>/corpus.json` and resolving each `path` against
`calibration/<family>/`). That walk is the corpus digest: the same order,
the same files, every time.

For each document:

1. Place the markdown at the path the family's judge already expects
   (`/app/postmortem.md`) together with the family's unchanged evidence
   directory. Do not inject `answer-keys/` into that environment.
2. Run the family's existing `causal_reasoning`, `action_quality`, and
   `evidence_fidelity` judges (the `judge.toml` / `contradictions.toml`
   rubrics). Deterministic gate checks are out of scope for this
   calibration record.

Variant tags (`correct`, `subtly-wrong-cause`,
`right-cause-useless-actions`, `fabricated-evidence`, `style-only-fluent`,
plus extra tags `empty` and `copied-evidence`) are inventory metadata. The
judge never sees `corpus.json`. HTML comments of the form
`<!-- calibration-variant: ... -->` may appear at the top of a document;
they are tags for humans and for the walker, not part of the rubric.

### 2. Sealed answer keys (gold for agreement)

For document id `D`, load `calibration/<family>/answer-keys/D.json`.
Each key has, for every named criterion in that family's
`causal_reasoning`, `action_quality`, and `evidence_fidelity` rubrics:

```json
"criteria": {
  "causal_reasoning": {
    "identifies_the_mechanism": {
      "verdict": "yes",
      "rationale": "one line"
    }
  }
}
```

`verdict` is the **judge's pre-inversion yes/no**. Several EF items and
`proposes_unsupported_work` are `negate = true` in the rubrics; the key
records whether the named flaw is present, not the post-inversion 0/1
score. `calibration.agreement.compare_document(family, key, judge_output)`
is the comparison. Agreement for a criterion is 1 iff the judge's
normalized verdict equals `key.criteria[dimension][name].verdict`.

Per-criterion agreement over the corpus is
`calibration.agreement.per_criterion_rates(...)`. The
`judge_calibrations` row brief 09 writes is:

- judge model
- rubric digest
- corpus digest (hash of the `corpus.json` walk plus file bytes)
- per-criterion agreement
- date

Policy `calibrated_judges_only` later gates on the latest row's mean
agreement (≥ 0.9). This directory does not implement that policy.

### 3. Trajectory labels — **out of scope for `calibrate`**

`calibration/trajectory-labels/*.json` are **not** consumed by
`evallab calibrate`. They label completed Harbor trials against the
failure taxonomy in `docs/analysis-loop.md` and exist for the analyst
agents (briefs 03 / 05 analysis loop). A calibrate run that reads this
directory is wrong.

Each label cites a real file under the trial (`path`) and an ATIF
`step` when `agent/trajectory.json` exists. `step` is null when the
trial has no ATIF steps; the cited path is then `result.json`,
`verifier/…`, `exception.txt`, or similar.

## What this directory does not contain

- No `evallab calibrate` CLI, DSPy program, or `src/` change.
- No answer key, expected-verdict file, or gold rubric inside any task
  `environment/`.
- No copies of harbor-practice evidence files (documents cite them; they
  are not vendored).

## Verification

From this worktree:

```bash
PYTHONPATH=. uv run python -m calibration.inventory --out "$PWD/../../scratch-out"
PYTHONPATH=. uv run pytest calibration/tests
```

`inventory` reprints corpus counts, the document↔key pairing, a search
for keys under `environment/`, and the trial↔label roster.
