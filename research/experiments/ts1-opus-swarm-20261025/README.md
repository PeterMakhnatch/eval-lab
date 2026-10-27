# TS-1: Opus, cheap swarm, and retrieved-evidence comparison

## Status: blocked before benchmark execution

No cohort task has been sent to a model or grader. There are no pass-rate
results and no basis for claiming that one arm outperforms another.
`meta/muse-wonder-1.0-contributor` refused the protocol-review preflight with
HTTP 429, reporting a subscription-window reset at **8:00 PM ET on October 25,
2026** (`2026-10-26T00:00:00Z`). No paid replacement or fallback was used.

[`manifest.json`](manifest.json) freezes the 20-task DEV selection, requested
arms, permissions, and analysis. [`preflight-receipts.json`](preflight-receipts.json)
records actual served identities, exact captured-response hashes, the provider
refusal, and two integrity smoke receipts. Protocol reviews are not benchmark
observations.

## Comparison contract

- Single Opus 5.5; Kimi + Muse + Sonnet proposals with an anonymized Sonnet
  selector; and Opus 5.5 with a frozen `CloudTraceRetriever` snapshot.
- Three attempts per task, with the same no-tools permission set, timeout,
  feedback renderer, and response format on Attempts 1, 2, and 3. There is no
  Attempt-1-only surface-probe restriction.
- A selector chooses an unchanged proposal ID, never a rewritten script.
  Raw response and selected script hashes remain distinct and both are retained.
- Disable route/usage-aware fallback and automatic memory recall. Require the
  actual served provider/model rather than crediting the requested route.
- Interleave arm order with a fixed seed. Freeze retrieval before outcomes;
  do not ingest the campaign's own traces during the comparison.
- Treat tasks, not attempts or proposer calls, as the paired analysis units.
  This is a complete-system comparison with unequal model-call budgets, not
  an equal-compute model ranking. Twenty selected DEV tasks support an
  exploratory paired description, not held-out generalization.
- Keep infrastructure refusals distinguishable from measured task failures;
  do not silently exclude unavailable routes or substitute a two-model swarm.

## Split and execution boundaries

Selection uses only the sealed DEV `inputs.jsonl` export recorded in the
manifest. It does not initialize mixed-split corpus or gym loaders. The chosen
Karotte ports use the port-grader contract, not the Harbor-package contract.
GPU/missing-port/oversized-verifier exclusions are recorded in the selection
rule; the remaining tasks have **not** yet been runtime-qualified on Modal.
The one-task pricing/runtime canary must precede expansion, within the remaining
applicable approval and shared spend cap. Local Docker is disabled.

The live evidence store's split-count metadata contained 133 DEV and 3,242
non-DEV rows at preflight. A whole-file snapshot was therefore rejected before
retrieval. The retriever now creates a SQL-filtered DEV-only projection before
copying task or trace content. A real-data smoke returned bounded verified
DEV evidence, exercised same-task exclusion, and preserved the exact queried
snapshot outside the repository. Exact-ID exclusion does not establish
cross-family independence; receipt status is not independent re-verification.

## Observed integrity proof

A captured real Opus response passed the new byte-custody check unchanged.
Replacing its script with a static template was refused. No script was executed
in that smoke. The retrieval smoke used no model calls or containers. Full raw
captures and the DEV database snapshot are private immutable evidence under
`~/Developer/eval-lab-results/2026-10-25/`; only small receipts are committed.
