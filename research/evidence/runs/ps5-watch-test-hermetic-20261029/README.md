# Watcher test isolation

The fixture changed ATLAS_ROOT but the separately constructed DEFAULT_WATCH_CALLS remained bound to the real home.
The backfill test reparsed arguments without --calls, reading and appending to that watcher ledger; origin/main observed 180 tokens instead of 18.
A clean HOME passed on origin/main; the fixture now redirects the default watcher ledger into tmp_path.
The full watcher suite passed (28 tests); the targeted test passed in both real and clean HOME.
`measure.py` records three interleaved post-fix CPU timings per HOME; it does not benchmark a production change.
