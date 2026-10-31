# relnotes

Internal release-note tooling.

`relnotes.history.release_ids()` reads the migrated release-history export at
`data/releases.tsv` and returns the recorded release ids in ledger order.

The migration is a straight copy of the old tracking spreadsheet: the ledger's
row order is the historical release chronology. Three builds were reconciled
after the fact (`2019.07.r2`, `2020.05.r1`, `2022.11.r1`), so the rows are
deliberately not sorted by id or by issue date. Sorting the export loses that
information and upstream git tags do not carry it either.

Run the visible checks with:

```bash
python -m pytest -q tests
```

See `data/README.md` for the column layout.
