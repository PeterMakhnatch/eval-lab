# LOCA lean replacement

This source-only replacement supersedes PR #168 (`fix/loca-verifier-state-sharing` at
`b29e3d0057a4289d567acd6106c0da42fcea15c4`). The old `library/benchmarks/loca-bench/`
task trees, vendor tree, source configs, and repeated adapter/verifier copies are
inventory-only and are not copied here.

## Materialization

`python3 library/benchmarks/loca-lean-v1/materialize.py` creates exactly one canary at
`derived/harbor-tasks/loca/<source-digest>/`. The output directory is ignored and
must be source-digest addressed. No generated task tree belongs in Git. The canary is
8k/seed 42; no fixture is committed because deterministic generation needs no fixture.

`source.py` validates the MIT license and immutable upstream/sandbox commits. The
production materializer is cache-first and SHA-fetches every pinned license, config,
generator, and tool-schema file from HTTPS when absent. Entries are atomically
installed as `<pin>.<sha256>` and rehashed; mismatches and cache misses fail closed.
`--cache-dir CACHE` selects the cache. The pinned 8k config is checked for the
ABTestingS2LEnv seed-42 parameters before generation.

`state.py` dynamically loads the SHA-verified upstream `generate_ab_data.py`; it
uses the official 8k parameters, trims serialized state at exactly 8192*4 bytes,
and reproduces the old context byte and state digest, failing closed on drift.
Canonical sorted UTF-8 JSON and the upstream CSV CRLF serialization are used. The
preserved old database digest is recorded in the manifest; the lean metadata DB is
intentionally not claimed as an exact vendored database reproduction.
`templates.py` contains the sole oracle, NOP, and swap/drop mutant templates;
`verifier.py` checks the manifest digest, non-empty state, exact oracle record, and
conditional marker. `context_curve.py` validates rows through the repository's typed
`ContextOperationFact` model; model/scaffold tokens remain unknown.

CI runs `ci_contract.py` to reject tracked old/new task corpora, regenerate twice,
and exercise oracle, NOP, and mutants. `evidence.json` preserves old head, source
and license pins, all nine old CAS references, file/byte inventory, and superseded
paths.
