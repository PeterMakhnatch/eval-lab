# on-gold-v1b-20261102 (A4B-FILTERED-GOLD)

Exploratory re-design of the Gold v1 certification (A4, `on-gold-v1-20261102`, pre-registered result 0/32 certified).
Result: 26/32 tasks with an accepted reference-filtered test, 8/32 `gold_structural` or better, 1/32 `gold_heldout`;
pooled unseen-EVAL kill 12/45. A4's numbers remain the headline for A4's original protocol.

What was wrong in A4 (diagnosed from stored cells and an A3 control): contract tests ran with generic `python3` in
`/testbed` instead of the grader's own interpreter/workdir; four tasks live in `/workspace/repo` and never started
(`cd /testbed || exit 1`); the single "repair" re-used the cached model output; train mutants were never graded;
exceptions were mapped to a reference failure.

| file | purpose |
|---|---|
| `envspec.py` | derive workdir, interpreter prelude and Python version from the hidden test patch |
| `a4b_synth.py` | prompt for 12 independent functions, per-test verdict parsing, dropping failed functions, repair prompt |
| `run_a4b.py` | driver (stages `control`, `accept`, `full`), resume-safe, Modal cap `$1.50`, `timeout_seconds=300` |
| `classify_a4_failures.py` | classification of A4's 28 reference failures, before and after the environment fix |
| `attest.py` | served-vs-requested model attestation from nested `omp -p` session logs |
| `aggregate.py` | pre-registered outcomes and the shared manifest/certified file (`--publish`) |
| `test_a4b.py` | offline tests with synthetic fixtures (no Modal, no model) |
| `summary.json`, `a4b-manifest.json`, `attestation.json` | aggregates; per-task id, status, counts only |

Third-party task text, test text and per-cell rows stay under `~/Developer/eval-lab-results/2026-11-02/A4B/` (local).

Run (Modal only, never local Docker):

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync --extra laminar python research/experiments/on-gold-v1b-20261102/run_a4b.py accept
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync --extra laminar python research/experiments/on-gold-v1b-20261102/run_a4b.py full --rounds 2
python3 research/experiments/on-gold-v1b-20261102/aggregate.py --publish
```
