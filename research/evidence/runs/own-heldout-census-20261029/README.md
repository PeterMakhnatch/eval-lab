# OWN-HELDOUT: partial own-run census

## Question and status

Can Eval Lab's own runs provide an unseen corpus of at least 40 spontaneous,
independently labelled reward-hacking positives and 40 independently checked
clean negatives, with both classes in 20 task clusters, at least two actor
families, and no trivial separating rule? **Not established.** This partial
inventory does not prove no such corpus exists. Both fresh DEV and a stronger
held-out set remain unquantified; no unsealing number can be justified.

## Seal incident

A broad search of `research` returned trajectory text and identifiers from
`research/datasets/qa-trace-sft-v0/test.jsonl` into the assistant's tool
context. This violated the requested non-DEV trajectory boundary. The affected
data must not be treated as unread until Research assesses the incident and its
scope. No trajectory text, identifiers, or per-run labels are reproduced here.
No additional reader, judge, or detector was invoked. See the fleet receipt for
the incident and continuation decision.

## What ran

The metadata-only census was preregistered before the label join; the private
registration and SHA-256 are in `results.json`. The observed metadata sources
were direct `2026-*/*/processed/trial-*.json` reports under the published results
home, the shared integrity Parquet catalog, the read-only cloud trace database,
and stored Run Atlas and trace-harness run-id sets. `evallab qa-data inventory`
was also run for strict admitted-DEV inventory; it does not establish clean
labels. No model calls, paid compute, grader execution, or detector scoring ran.

- **Published reports:** 7,595 files; 16 carry `copied_fix`, 3 `audit_cheat`.
  These counts include controls and possible duplicate trials and do not certify
  spontaneous behavior, trajectory coverage, split, or freshness. Reasons can
  overlap. Among the 16 `copied_fix` reports, 11 have a MiMo model name and 5
  another or unknown model; that is not two established actor families.
- **Integrity catalog:** all 1,416 qualification rows are nop controls; of 34
  exploit rows, 32 are not probed and two say none. These are not actor-run
  positive/negative labels. Task-level findings do not themselves certify a run.
- **Cloud own-run collections:** 57 stored positive-flag rows: 51 MiMo, 4 TB4,
  2 TB2/TB21. All 57 identify no-model replay/journal records, so none is an
  independently demonstrated spontaneous actor trajectory. All declare DEV,
  but that field is not strict cross-key admission. The 32 provisional positive
  QA-SFT cloud rows are a derived mixed-source collection, not a fresh own-run
  cohort; the dataset was already used in detector experiments.
- **Exposure:** 32,695 findings rows have 5,529 distinct run IDs; 6,965 facet
  rows have 4,165 distinct run IDs. Direct trial-name equality matches 34/7,595
  reports to findings and 0/7,595 to facets. These namespaces have not been
  bound, so nonmatches are **unknown**, not proof of detector non-exposure.
  Among the 57 no-model cloud positives, 43 match both files, 14 match neither.
  Those 14 are not qualifying actor cases and may have other exposure.

## Interpretation and remaining work

A: qualifying spontaneous positives and full submission/test-write visibility
are unknown. B: independently clean run-level negatives and mixed-task clusters
are unknown; neither verifier success, a control condition, nor no flag is a
clean label. C: qualifying actor diversity and content-free separation are
unknown. D: strict cross-key assignment, all task/run selection exposure,
cascade caches, and receipt joins are incomplete. Hence neither DEV nor
held-out status passes any criterion on this evidence; this is **not** a numeric
zero or a definitive corpus-wide failure.

To continue, Research must assess the seal incident. Then a metadata-only,
identity-bound inventory needs stable trial-to-trace mapping, strict cross-key
admission, source and actor provenance, independent positive and negative
labels, completeness evidence, all detector/cascade/selection receipts, and a
predeclared content-free baseline. Held-out trajectory access or a one-shot
held-out test requires a separate Research unsealing decision; this census does
not establish a number to unseal.

`results.json` records measured aggregate counts and hashes. Sources were live
and mutable; hashes taken after measurement are not an atomic snapshot. The
report enumeration was limited to direct published trial reports; other native
jobs, attached audit views, and detector receipts were not exhaustively joined.
