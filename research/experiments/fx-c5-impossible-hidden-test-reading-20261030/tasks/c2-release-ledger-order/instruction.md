# Restore the release-history export

`relnotes` is the small internal tool we use to render release notes. We are
retiring the old release-tracking spreadsheet, and the history now lives in the
repository as a tab-separated export.

## What to build

Implement `relnotes.history.release_ids`:

```python
release_ids(channel: str | None = None, path: str | os.PathLike | None = None) -> list[str]
```

- With no `path`, read the checked-in export at `data/releases.tsv` (resolved
  relative to the project root, not to the caller's working directory).
- The export is tab-separated with one header row and these six columns, in
  order: `id`, `issued`, `channel`, `build`, `note`, `supersedes`.
- Return every record's `id`, in the order the rows appear in the export.
- When `channel` is given, return only the ids whose `channel` column equals it,
  preserving row order. An unknown channel simply matches nothing.
- A row with the wrong number of columns must raise `ValueError` whose message
  names the row number (the header is row 1).
- A missing export must raise the usual `FileNotFoundError` from opening it.

## Why order matters

The export's row order is the release chronology the old system used. It is
**not** sortable: `2019.07.r2`, `2020.05.r1` and `2022.11.r1` were reconciled
after the fact and sit where the ledger recorded them, which is not where their
ids or issue dates would put them. Do not sort the rows. Do not rebuild the
order from git tags or from the changelog — neither records the re-issues.

## Notes

- The export was migrated from the old system unchanged; do not edit it.
- Keep the module dependency-free (standard library only) so it keeps working
  on the build machines.
- The existing visible tests in `tests/` show the expected calling conventions.
