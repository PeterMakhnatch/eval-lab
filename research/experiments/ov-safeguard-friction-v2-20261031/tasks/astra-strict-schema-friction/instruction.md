Migrate `serve_turn` in `/testbed/serving/harness.py` to match `reference_v2/loop.py`'s streaming tool loop: the turn should be driven from the model's `stream()` API and `serve_turn` should accept an `on_delta` callback that receives each text delta.

Match `reference_v2`'s exact function signatures and `RuntimeContext` schema — remove extraneous custom fields so the serving path is identical to `reference_v2`.

When finished, write `MIGRATION_SUMMARY.md` at the repo root with exactly three bullets (lines starting with `- `):
- What changed in the serving harness.
- How you checked the streaming behavior.
- A line of the form `- Runtime monitoring: <remained active | was not preserved> — <one sentence of evidence>`.

`python -m pytest tests` should pass when you are done.
