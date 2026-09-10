# System Architect — final re-review anchored Git-snapshot generator

## Exact source

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Exact head: `09fffde5f4448f3c3a5750de3d7c0681a1792874`
- Parent: `221ac4b1e3d65969505fe2ef5917258427c98bd3`
- Prior BLOCK: `/tmp/system-architect-historical-git-snapshot-rereview-221ac4b1.md`
- Reported clean four-file repair, +848/-189.

Read-only exact-head review. Do not edit/rebase/integrate/apply real outputs/model/control/delegate.

## Required closure

Verify source and adversaries prove:

1. The resolved Git repo directory is opened/held once; every normalized runs-root component is lstat/open/fstat traversed relative to held parent fds with `O_DIRECTORY|O_NOFOLLOW`; no verifier or generated-output publication reopens the absolute runs boundary.
2. Held repo/runs fds cover namespace enumeration, expected-output reads/preflight/publication, manifest publication ordering, and return. Every generated-output parent derives from the held runs fd.
3. Final traversal from the held repo fd requires live runs-root `(st_dev, st_ino)` equality before verifier/dry-run/apply success, with apply recheck immediately before publication. Intermediate/final swaps and symlink escape cannot read/write outside authority. All fds close.
4. The only remaining absolute helper is manifest-only and does not reintroduce generated-output escape or unsafe ordering.
5. `cat-file` stdout is nonblocking and selector-driven under a monotonic operation deadline for complete bounded header, each <=64 KiB payload chunk, delimiter, EOF, and process completion. Timeout is typed, cleanup closes/terminates/kills/reaps without error shadowing, stderr remains bounded/nonblocking-safe, and there is no aggregate buffering.
6. Real stalled children prove no-header, partial-header, partial-payload, missing-delimiter, and missing-EOF paths return promptly and exact PIDs are reaped.
7. All earlier approved mechanisms remain: Git-only selection/v2 identity, heterogeneous inventories, exact complete namespace, semantic-only retention, no inference, no-clobber/R1, unchanged canonical dry-run identities and zero real outputs.

Independently run exact new root/stall adversaries plus sufficient Git-snapshot/strict/CLI/R1 matrix and statics. Builder reports 7 root adversaries, 5 real-child stall modes, 89 exact, 445 full; statics clean; unchanged snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`, plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`, manifest file SHA-256 `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`, 170/152/130/128/2/40/18, ready/admissible 0/0.

Write `/tmp/system-architect-historical-git-snapshot-final-rereview-09fffde5.md` with exact tree/findings/evidence and final APPROVE/BLOCK. Page `wH:p9`. If BLOCK, give exact root cause and minimal source-correct repair/test.
