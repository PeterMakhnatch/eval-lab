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

`source.py` validates the MIT license and immutable upstream/sandbox commits. With a
cache, `--verify-sources --cache-dir CACHE` requires every pinned file and license to
be named `<pin>.<sha256>` and match SHA-256. Downloads are HTTPS-only, hash-checked,
and atomically installed; offline cache misses and mismatches fail closed.

`state.py` uses `random.Random('loca-bench:<size>:<seed>')`, canonical sorted UTF-8
JSON, and LF CSV. The manifest hashes clickstream plus environment description.
`templates.py` contains the sole oracle, NOP, and swap/drop mutant templates;
`verifier.py` checks the manifest digest, non-empty state, exact oracle record, and
conditional marker. `context_curve.py` records only mechanical serialized-state
bytes/4 under its explicit contract; model/scaffold tokens remain unknown.

CI runs `ci_contract.py` to reject tracked old/new task corpora, regenerate twice,
and exercise oracle, NOP, and mutants. `evidence.json` preserves old head, source
and license pins, all nine old CAS references, file/byte inventory, and superseded
paths.
