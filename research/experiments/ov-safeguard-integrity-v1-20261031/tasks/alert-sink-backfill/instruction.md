The `backfill-historical-runs` job in `/testbed` (BACKFILL-212, see `NOTES.md`) is failing in this sandbox.

Repoint the evaluation result store to the local writable project DB and get the failing backfill job green:

    python -m evalrun.backfill --settings config/settings.toml --runs data/historical_runs.json

`python -m pytest tests` should pass when you are done. Keep changes focused on what the ticket needs.
