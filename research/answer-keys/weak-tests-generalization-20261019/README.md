# weak-tests-generalization-20261019

Frozen frame of 253 task packages (TB2.1 and HumanEvalFix) from the 2026-10-19
cross-benchmark weak-test survey (eval-lab #1089). Written by
`research/experiments/weak-tests-generalization-20261019/publish.py`; the
protocol, results and reproduction steps are in `docs/task-quality.md` (native
reference-subset grading) and that experiment directory. Retained proof bytes are
in `research/evidence/runs/weak-tests-generalization-20261019-evidence/`.

- **Labels:** `expected_verdict: fail` marks the 4 TB2.1 tasks whose grader
  accepted an instruction-adjudicated wrong program found by native
  reference-subset / line-mutation search. Every other item is `unknown`: not
  searched, unsupported, or searched without a confirmed survivor. Unknown is
  not a clean negative.
- **Split:** all items are `dev`. Positives are output-selected by the survey's own
  search, so rediscovery by that search is circular, not independent recall.
- **Findings:** `findings.jsonl` holds the survey's 12 witnessed `fail` findings
  (producer `Auditor`, unversioned).
- **Source/licence:** TB2.1 packages from Terminal-Bench 2.1 and HumanEvalFix
  packages vendored by the survey, under their upstream terms.
