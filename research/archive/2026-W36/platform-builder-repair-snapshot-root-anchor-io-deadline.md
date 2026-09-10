# Platform Builder — final Git-snapshot root-anchor and I/O-deadline repair

## Exact source

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Head: `221ac4b1e3d65969505fe2ef5917258427c98bd3`
- Architect BLOCK: `/tmp/system-architect-historical-git-snapshot-rereview-221ac4b1.md`

The prior heterogeneous-inventory, ordinary full-namespace, and bounded-memory fixes pass. Preserve them. Do not apply real outputs, integrate/rebase, run model/control, or spawn subagents.

## Repair 1 — anchor every runs-root operation below a held repository fd

`O_NOFOLLOW` on an absolute runs boundary protects only the final component. Remove absolute boundary reopening from verifier and output publication.

Implement a narrow descriptor context/protocol:

1. Resolve/discover the Git repository root once as an operational authority and open it once as a directory fd (`O_DIRECTORY|O_NOFOLLOW`, close-on-exec where available).
2. Normalize the repo-relative runs-root components and traverse **every component** relative to the held parent fd using `os.open(..., dir_fd=parent_fd, O_DIRECTORY|O_NOFOLLOW)`.
3. Validate directory identity/type at each hop; retain the resulting runs-root fd for the complete namespace enumeration, expected-output verification, output preflight/publication, and apply result path.
4. Open each output parent from a duplicate of that held runs-root fd. Never reopen `repo_root / runs_root` as one absolute pathname.
5. Before successful verifier/apply return, traverse the normalized runs-root again from the still-held repo fd and require the live final entry’s `(st_dev, st_ino)` to equal the held runs-root fd. A replacement, disappearance, symlink, or mismatch refuses. No rollback/deletion.
6. Close every descriptor on every path. Preserve per-child lstat/open/fstat identity checks, full namespace equality, no-follow named occurrences, and no-clobber/inode publication.

Apply publication must use the same anchored protocol, not only the consumer verifier. Manifest-out publication may retain its existing independently safe path contract, but generated contracts must never escape/race the anchored Git repository root.

Add adversaries:

- static intermediate-component symlink from `repo/nested` to external, with regular final `runs`, refuses and never reads/writes external contracts;
- replace an intermediate component during traversal, refuses;
- replace the final runs-root entry after initial open but before success, final inode recheck refuses;
- apply/preflight variants prove no generated contract is created outside the repository;
- fd-count/cleanup assertion for refusal and success paths where deterministic.

## Repair 2 — deadline every streamed cat-file read

Read chunk size is bounded but current `readline/read/read(1)` calls can block forever. Use nonblocking stdout plus `selectors.DefaultSelector` and an explicit monotonic deadline covering:

- complete bounded header;
- every payload chunk;
- record delimiter;
- final EOF/process completion.

Requirements:

1. Keep <=64 KiB payload reads and no aggregate buffering/`communicate()`.
2. Enforce a small bounded header length and reject missing newline/oversize/malformed header.
3. On no readiness before the deadline, raise typed `HistoricalSnapshotUnavailable`.
4. The existing `finally` path must close pipes, terminate then kill if necessary, and reap. Do not let timeout cleanup shadow the typed root error.
5. Exit/stderr validation remains bounded; no stderr pipe-fill deadlock.
6. Use a production-appropriate deadline constant/config internal to the operation, with deterministic test injection/monkeypatch rather than sleeps near 120 seconds.

Add **real child-process** adversaries, not only fake streams, which accept an OID request and then stall at:

- no complete header;
- partial header;
- partial payload;
- missing delimiter if separately useful;
- correct record/delimiter but missing final EOF.

For each, assert prompt bounded typed refusal and prove the exact child is terminated/killed and reaped. Preserve size/OID/type/SHA/trailing-corruption and repeated-OID tests.

## Verification/handoff

Run new exact adversaries plus full Git-snapshot/strict/CLI/R1 publication matrix and statics. Run two dry-runs only against exact source revision `f414512a728d26d507eccae9792ef89e6414007c`; identities must remain:

- snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`
- plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`
- manifest file SHA-256 `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`
- census 170/152/130/128/2/40/18; ready/admissible 0/0.

Confirm zero real outputs, clean commit, and page `wH:p9` with exact head/diff/tests/statics/digests plus no-apply/no-model/no-subagent confirmation. Return for Architect exact-head re-review.
