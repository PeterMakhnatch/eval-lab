# weak-tests-generalization-20261019

Frozen frame of 253 task packages (TB2.1 and HumanEvalFix) from the 2026-10-19
cross-benchmark weak-test survey (eval-lab #1089). Written by
`research/experiments/weak-tests-generalization-20261019/publish.py`; the
protocol, results and reproduction steps are in `docs/task-quality.md` (native
reference-subset grading) and that experiment directory. Retained proof bytes are
in `research/evidence/runs/weak-tests-generalization-20261019-evidence/`.

- **Labels:** `expected_verdict: fail` marks the 3 TB2.1 tasks whose grader
  accepted an instruction-adjudicated wrong program found by native
  reference-subset / line-mutation search. Every other item is `unknown`: not
  searched, unsupported, or searched without a confirmed survivor. Unknown is
  not a clean negative.
- **Split:** all items are `dev`. Positives are output-selected by the survey's own
  search, so rediscovery by that search is circular, not independent recall.
- **Findings:** `findings.jsonl` holds the survey's 7 witnessed `fail` findings
  (producer `Auditor`, unversioned).
- **Relabel (2026-10-19):** `tb21/sqlite-db-truncate` changed from `fail` to
  `unknown`. Its 5 survivors are now labelled `within_intentional_grader_slack`
  instead of `required_gap`, and their findings were removed. The test scores
  8 rows and passes with at least 7. The truncated db cuts off inside rows 00
  and 01, which the test omits on purpose. Rows 08 and 09 are 8-byte doubles,
  while rows 02–07 are 1-byte integers, so 7/8 tolerates exactly one
  mis-decoded row. The instruction ("as many of the rows as possible") does not
  clearly require all 8. Two reviewers (Auditor and an independent verifier)
  judged this threshold to be intentional slack. TB2.1 is now 3/18 confirmed
  weak (Wilson 95% 0.058–0.392), down from 4/18. See
  `research/experiments/tb21-test-repairs-20261019/notes/sqlite-db-truncate.md`.
- **Source/licence:** TB2.1 packages from Terminal-Bench 2.1 and HumanEvalFix
  packages vendored by the survey, under their upstream terms.
