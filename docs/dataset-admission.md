---
status: living
audience:
  - builder
  - analyst
---

# Dataset admission

`evallab admit` automates the W3 admission procedure. It uses local deterministic detectors, not model calls.
The first approved adapter profile reuses the HackBench admission from PR #1888.
The access command also checks other Hugging Face datasets. It does not admit unknown label semantics.

## Access comes first

Use a full revision and a real data filename. A tree listing does not prove access.
The command downloads up to 1024 bytes. Missing credentials, empty responses, and HTTP errors refuse admission.
It prints the HTTP status, but never the response body, signed URL, or credential.

```bash
keys run -- uv run evallab admit access AlignmentResearch/impossible-swe-nemotron-rollouts \
  --revision ce2f2f3d645bcee5672ab9ffbaa93d1b8f54917d --file rollouts/step_001.parquet
```

A 403 means that the token lacks publisher access. Request access before admission.
Do not use a cached tree listing as an alternative.

## Prepare without scoring

Choose a new external output directory and a new registration path.
Keep raw source bytes, terms, converted views, and per-record results outside Git.

```bash
keys run -- uv run evallab admit prepare \
  --output ~/.local/state/evallab-ev1105/w3/admission \
  --registration research/experiments/ev1105-w3/freeze.json \
  --prior-exposure ~/.local/state/evallab-cross-test/meta-hackbench/meta-hackbench-cm11/cascade-command.jsonl
```

Preparation downloads the complete pinned release, even when the canonical cache already exists.
The canonical cache is `~/.cache/eval-lab/source-intake/facebook__hackbench/<revision>/`.
If it exists, preparation preserves it and retains a fresh download under the output directory.
It compares both verified manifests before conversion. It never replaces existing evidence.
A partial or changed cache refuses admission. Use a new cache path for investigation.

The manifest includes file SHA-256 digests, sizes, row counts, and publisher count checks.
The pinned README captures the CC-BY-NC-4.0 license and additional no-training/no-rehosting terms.
The existing adapter and label map enforce monitor-input isolation and positive-only release semantics.
They are functional admission components, not a generated placeholder adapter.
An exact publisher-ID hash join against the supplied prior detector receipt audits exposure.
Missing prior records refuse the HackBench profile because its label map declares prior exposure for every row.
Preparation never erases exposure or invents negative trajectories.

Preparation writes an aggregate registration with frozen DEV thresholds and length baselines.
It binds source hashes, labels, rights, the manifest, and every converted view before scoring.
It preserves all 169 records, with 0 negatives and 60,541 command blocks.

## Commit, push, then score

Commit the implementation before preparation. Use `preregister` to commit and push only the frozen registration.
The command preserves other staged changes and refuses to replace an existing committed registration.
The scorer queries the remote branch heads and reads the committed registration and source bytes.
A local-only commit, changed thresholds, changed code, changed views, or a missing registration blocks scoring.
The requested commit must remain a current pushed branch head at scoring time.

```bash
uv run evallab admit preregister --registration research/experiments/ev1105-w3/freeze.json
uv run evallab admit score \
  --output ~/.local/state/evallab-ev1105/w3/admission \
  --registration research/experiments/ev1105-w3/freeze.json \
  --commit <full-pushed-commit>
```

The scorer reuses DetBench L0 evaluation, metrics, and both length baselines.
It preserves detector failures as abstentions and uses 1000 task-cluster bootstrap resamples with seed 1104.
It reports 95% intervals. Without negatives, AUROC, FPR, and superiority over length remain unavailable.
The full line remains incompatible with held-out admission; its explicit abstention is not an L0 result.
This workflow does not start judges or substitute models. Scoring has no automatic retry after observing outcomes.

Use the publication phase to commit aggregate results, push, and create the admission PR.
It refuses unexpected report fields and per-record identities. It reuses an existing open branch PR on repetition.

```bash
uv run evallab admit pr \
  --output ~/.local/state/evallab-ev1105/w3/admission \
  --registration research/experiments/ev1105-w3/freeze.json
```

The command publishes only `aggregate-results.json` and the aggregate registration. It never publishes raw rows or views.
Deliver the PR with `uv run evallab ship <pr>` from a stable checkout.
Use [ship](ship.md) for exact-head CI, protected merge, merged-revision smoke, and retirement.
Use `evallab verify-lane` and `evallab report program` for lane receipts and aggregate reporting.
The existing [fleet](fleet.md) supervisor controls the lane route and quota, not dataset access rights.

## Verification

`uv run pytest tests/test_admission.py` checks inaccessible and empty downloads, missing credentials,
unpushed registrations, changed thresholds, changed detectors, isolated registration commits, immutable freezes, and raw-data publication.
The real W3 receipt records the complete download, the exact prior-exposure join, and the gated 403.
