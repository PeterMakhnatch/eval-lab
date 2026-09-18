# Platform Builder — repair Git-snapshot verifier completeness and streaming

## Exact source

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Branch/head: `feat/strict-historical-regeneration @ d349aaaaba98246e394e2d588cfd702ae8723e5e`
- Architect BLOCK: `/tmp/system-architect-historical-git-snapshot-review-d349aaaa.md`

Keep the Git selected-blob/v2 cutover intact. Do not apply real outputs, integrate/rebase, run model/control, or spawn subagents.

## Repair 1 — heterogeneous trial verifier

In `verify_historical_contract_set`, after resolving each output's own `disposition`, compute that disposition's own loop-local trial prefix and use it for selected-blob inventory filtering and `removeprefix`. Never leak the last disposition-loop prefix.

Add a success test with at least two promoted trials whose artifact paths and contents are deliberately different. Materialize the exact planned contracts and prove the shared verifier accepts the authentic heterogeneous set.

## Repair 2 — enumerate the complete generated-output namespace

Before output byte verification, descriptor-safely enumerate every live entry named exactly `historical-contract.json` below the destination runs root without following directory or file symlinks. Normalize paths and require the enumerated set to equal exactly `set(manifest.outputs[].path)`.

- Refuse a named occurrence that is symlinked or nonregular.
- Do not follow symlinked directories.
- Preserve the output no-follow/inode protections when opening expected files.
- Do not limit extras checking to selected/disposition locations.

Tests must refuse extras under:

1. a selected disposition that emits no contract;
2. an unselected/non-promoted trial;
3. an unrelated nested directory.

Also exercise symlink/nonregular named occurrences and a symlinked-directory trap without traversing outside the runs root. Preserve existing expected-output substitution/race tests.

## Repair 3 — truly stream `git cat-file`

Replace `_read_blob_batch() -> dict[str, bytes]` and `capture_output=True` with a streaming authenticated-blob reader backed by `subprocess.Popen` batch I/O.

- Request/read one OID response at a time (or use a rigorously bounded protocol); never buffer the concatenated selected tree.
- Parse and validate header OID/type/size before payload acceptance.
- Consume each payload in fixed bounded chunks while updating SHA-256 and length.
- Capture payload bytes only for the exact semantic documents the planner parses; do not retain inventory-only artifact bytes.
- Capture path: construct metadata/model row as each blob finishes and retain only semantic documents.
- Reopen path: validate and discard every payload.
- Cleanly close stdin, consume/validate process exit/stderr, and terminate/reap on exceptions without pipe deadlock or zombie process.
- Convert process/object corruption failures to the existing typed snapshot-unavailable error.

Do not replace the whole-tree dictionary with a whole-tree list/generator cache or `communicate()` buffer.

Add deterministic tests with multiple synthetic large non-semantic blobs plus small semantic documents proving:

- capture retains only exact semantic documents;
- reopen retains none;
- reader never performs an unbounded/full-payload read and the maximum payload read is the fixed chunk bound;
- digest/size/OID/type/trailing-record corruption and subprocess failure still refuse;
- repeated OIDs, if deduplicated for I/O, still produce correct per-path identity rows.

Prefer an injectable stream/process test double or bounded-read spy over flaky wall-clock/RSS assertions.

## Verification/handoff

Run the exact repaired adversaries, existing Git-snapshot/strict/CLI/R1 publication matrix, statics, and two pinned dry-runs at source revision `f414512a728d26d507eccae9792ef89e6414007c`. Digests/counts should remain exactly:

- snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`
- plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`
- manifest file SHA-256 `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`
- counts 170/152/130/128/2/40/18; ready/admissible 0/0.

Confirm zero real outputs. Commit cleanly and page `wH:p9` with exact head, diff, tests, statics, dry-run identities, and no-apply/no-model/no-subagent confirmation. Return for Architect re-review.
