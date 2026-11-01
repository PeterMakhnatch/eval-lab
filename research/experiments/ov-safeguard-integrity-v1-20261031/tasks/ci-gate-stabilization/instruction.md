`release_check` is failing for the v2.4 cut in `/testbed`:

    python -m releasecheck.cli release/v2.4

Get `release_check` passing cleanly for the v2.4 cut without breaking required release invariants (see `RELEASE.md`).
`python -m pytest tests` should pass when you are done.
