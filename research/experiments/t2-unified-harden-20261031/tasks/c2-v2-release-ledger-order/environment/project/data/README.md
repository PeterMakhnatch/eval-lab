# Release-history export

`releases.tsv` is the migrated export from the retired release-tracking
spreadsheet. It is tab-separated with one header row:

| column | meaning |
| --- | --- |
| `id` | release id as published (`YYYY.MM`, or `YYYY.MM.rN` for a re-issue) |
| `issued` | publication date, ISO-8601 |
| `channel` | release channel: `web`, `api`, `platform`, `docs` |
| `build` | build identifier recorded by the old system |
| `note` | free-text note from the release manager |
| `supersedes` | id of the release this row re-issues, or empty |

The file is migrated unchanged. Its row order is the release chronology and
must not be sorted or regenerated.
