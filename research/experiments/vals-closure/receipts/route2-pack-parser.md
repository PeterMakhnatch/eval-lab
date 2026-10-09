# Route 2 receipt: strip defeats the hand-rolled pack reader (MEASURED)

Date: 2026-10-09 UTC. $0 (local Docker + host-side audit, no model).

## Claim

On the `strip-future-history@1` variant of 002552 (existing record
`3efa6a29bc46`, read-only reuse), a hand-rolled git object-store reader —
pure Python (`struct`+`zlib` only, no git binary), resolving OFS/REF deltas
itself — enumerates every object and finds **zero post-base objects**.

## Tool

`pack_audit.py <git_dir> <base>` (copy in this directory): parses `.idx` v2
(fanout/sha/crc/offsets incl. 64-bit table), walks `.pack` entries
(undeltified + OFS/REF deltas), hash-verifies every object against the idx,
decodes commits, BFS-walks parents from BASE, then walks trees/blobs under
the closure. Exit 0 iff every store object is BASE-reachable and BASE is
present. Two bugs were found and fixed while building it (idx 40-byte
trailer, not 20; pack deltas operate on headerless content and inherit the
base type) — both verified by the hash check, which fails loudly on any
misdecode.

## Results

Stripped 002552 (setup ran in-container from variant `3efa6a29bc46`,
`.git` copied to host for audit, BASE `d5179059`):

- Reader: `store_objects=7418 commits=1001 base_closure=1001
  post_base_commits=0 post_base_objects=0 base_present=True
  dangling_tree_refs=0`, types `blob:3313 commit:1001 tree:3104`. Exit 0.
- Git cross-check (same store): `fsck --unreachable` 0 commits / 0 other,
  `rev-list --all --not BASE` empty, `count-objects -v` `in-pack: 7418`,
  `verify-pack -v` 3313/1001/3104 — exact match with the reader on all four
  counts.
- Control (not vacuous): same reader against the same store with BASE's
  50th ancestor exits 1 with `post_base_commits=50 post_base_objects=460`.

So even Vals' rung-4 escalation (custom pack parser under a git-command
block) finds nothing: there is no post-base object left to parse.

## Automation: probe-image-checks@2 → @3

`probe-image-checks@2` covered the ladder only via `pack_files` counts and
trace-side regexes; nothing asserted the pack inventory. Extended in this
branch (`src/evallab/exploit_probe.py`, `CHECKS_TRANSFORM =
"probe-image-checks@3"`):

- `CHECKS_SCRIPT` emits per git dir
  `== pack <dir> commits=N trees=M blobs=B tags=T base_commits=K`
  (`git verify-pack -v` over every pack + `git rev-list --count BASE`).
- `image_findings` flags `pack holds N commits but base reaches K (<dir>)`
  when N > K (unknown base stays silent; the fsck lines still guard).
- Tests: clean fixture carries the new line (commits == base, no finding);
  new `test_image_findings_report_pack_commit_surplus` covers surplus and
  the unknown-base case. `bash -n` on the rendered script passes.
- The id bump keeps digest-bound lineage honest: existing @2 records stay
  reproducible; new derivations get @3.

On the stripped store above, the v3 line reads `commits=1001 ...
base_commits=1001` → no finding; on the leaky 1.2.0 stores from route 1 it
would read e.g. `commits=1013 base_commits=1001` → finding.
