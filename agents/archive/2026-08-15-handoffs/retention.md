Status: building
Last: durable FilesystemCatalog writes derived/gc-catalog.json on apply
Next: tests, premerge, PR from fresh branch role/retention-catalog
Blockers: none

## Dated chore (2026-08-14)

Today is 2026-08-14, before 2026-08-21. The documented legacy CLI alias in
`pyproject.toml` stays.

## Follow-up after PR #12

Skeptic: `--apply` only mutated a throwaway MemoryCatalog. Fix: `FilesystemCatalog`
persists `derived/gc-catalog.json` and reloads it; apply also attempts a
Postgres evidence_path UPDATE. New test reloads a fresh catalog after apply.
