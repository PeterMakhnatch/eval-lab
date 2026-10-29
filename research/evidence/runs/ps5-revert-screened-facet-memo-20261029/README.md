# Screened facet memo removal (2026-10-29)

Question: does removing the memo change served results or one-shot CPU? The old
checkout is `812454c48fc567fb83972d5c469f20aa95ecc791`; new is the reverted
source in this change. Both read the same 4,207,423,488-byte read-only SQLite
backup `/private/tmp/ps2/store.sqlite`. No model calls or non-DEV answer keys.

`cli_identity.py` compares serialized replies after removing `meta` and records
only counts and exit codes; it includes the withheld run's trace-card and an
actual successful one-run export. The historical memo evidence directory's
`measure.py` compares 40 screened-read shapes and 40 retrieval queries using
SHA-256 digests, and `cli_time.py` measures fresh-process CPU with seven
interleaved pairs, alternating which checkout runs first. All use the new
checkout's interpreter and the specified checkout's source and registry.
`results.json` records aggregate outcomes and CPU samples; no served content.
CPU timings can include interference from other local work.

All 19 CLI replies matched (18 successful and one identical refusal); the
40-shape and 40-query aggregate digests matched with no retrieval errors.
CPU medians old → new, seconds: traces 0.254843 → 0.255539; task
0.320230 → 0.334038; clusters 0.268909 → 0.271204; filtered runs
0.318233 → 0.329802. The differences are small relative to observed sample
spread; these measurements do not establish zero regression.
