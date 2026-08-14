Status: review-wanted
Last: Rebased onto origin/main and validated all three peer-reviewed inspection reports on the exact head.
Next: Open the INSPECTOR PR, require all current-head checks green, then merge only after final root review.
Blockers: none

# INSPECTOR handoff

Working only in `.worktrees/inspector` on `role/inspector`. No billable or control
runs were needed; all source runs remained read-only.

Evidence checkpoint:

- Transaction r2 is three `harness_failure` trials: each Codex CLI invocation
  reached only 401 authentication retries, produced no agent/tool ATIF step and
  no tokens, then the verifier correctly rejected the untouched seed database.
  Current task content differs from the August 6 original only by two import-order
  edits and the executable bit on `tests/test.sh`. The `tests/verify.py` version is
  on unmerged sibling commit `f7aa4c5`, not in the task used by r2.
- Judge weakness is concentrated: three overlapping negated criteria account for
  47 of 73 disagreements. The calibration task also omitted `/app/evidence`,
  contrary to the corpus contract and original judge configuration, making the
  worst criterion (`invents_evidence`) underdetermined.
- The DISCOVERIES finding is a narrowly supported control observation with
  warranted caveats. Its durable audit trail is not acceptable yet: 2/4 journal
  links do not resolve in the tracked checkout, and six of eight catalog rows
  self-cite the aggregate `evidence.json` rather than raw run evidence. The eight
  rows are one representative per job selected from 24 underlying trials, with
  no disclosed denominator or trial IDs; the cap omitted an existing HTML oracle
  and caused the synthesis to recommend collecting one.

Draft reports: `research/inspections/transaction-reconciliation.md`,
`research/inspections/judge-floor.md`, and
`research/inspections/discoveries-first-pass.md`.

Verification after rebasing onto `origin/main`:

- `make premerge`: pass; Ruff clean, 73 pytest tests passed, ty stayed at the
  33-diagnostic ratchet.
- Markdown evidence links: 114 occurrences, zero missing relative or local
  filesystem targets.
- Judge arithmetic, 24-trial hidden denominator, and local brief-07
  `f7aa4c5:tasks/transaction-reconciliation/tests/verify.py` object rechecked.
- `git diff --check origin/main...HEAD`: pass; diff confined to INSPECTOR-owned
  report and handoff paths.
