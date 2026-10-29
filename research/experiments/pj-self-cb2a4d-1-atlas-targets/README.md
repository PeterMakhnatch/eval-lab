# pJ-SELF-cb2a4d-1: can Run Atlas clusters pick audit targets within a dataset?

Preregistered in the bridge `opus-vs-swarm-qa.md` before any label was joined. `atlas_targets.py` fits a smoothed keyed
share per label-free atlas cluster (levels 1-3) on one `sha256(task_id)` parity half of the MiMo DEV tasks, scores the
other half, swaps, and compares with a run-exposure baseline and the four OQ-9 facet counts (held-out AUC with task
bootstrap, top-10 keyed share). goodfire is reported as coverage only. Zero model calls. Unkeyed tasks are not certified
negatives, so this measures "known keyed defect", not "defect present".

Run: `python atlas_targets.py --output results.json` (needs the cloud trace store, the run-atlas `facets-v1.jsonl`,
and the OQ-7 `labels.jsonl` population).
