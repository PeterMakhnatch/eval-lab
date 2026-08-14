Status: review-wanted
Last: live hello-world@1.0 fetch+sample; audit 5/5 pass; premerge green
Next: PR FETCH: …; merge only when gh pr checks is fully green
Blockers: none

FETCH 2026-08-14. Worktree `.worktrees/fetch`, branch `role/fetch`.

## Commands (ran in this worktree)

`uv run evallab fetch --help` — flags `--list`, `--audit`, `--verify-sample N`, positional `name@version`.

`uv run evallab fetch --list` — Hub pins including `hello-world@1.0 tasks=1`, `aime@1.0`; `ds-1000@head` filtered; adapter lanes listed; header says `@latest` is refused. Transcript: scratch `fetch-list.txt`.

`uv run evallab fetch hello-world@1.0 --verify-sample 1`
```
fetched: materialized hello-world@1.0 (1 tasks, 1.7 KB)
manifest: library/benchmarks/hello-world/MANIFEST.md
```
Oracle **1.0** / nop **0.0** on task `hello-world`. Harbor sync digest `sha256:cff230d09ea952d092daf99796d0c52ec5bfb92d86f13021af902ce7b6b36720`. Existing INGEST dirs not touched.

`uv run evallab fetch hello-world@1.0` (second call)
```
noop: already pinned hello-world@1.0; digest match; no-op
```

`uv run evallab fetch --audit`
```
pass  aime: pin aime@1.0 recorded commit 414014c2…; no recorded tree digest (INGEST handmade); on-disk sha256:845637e5…
pass  gpqa-diamond: pin gpqa-diamond@1.0 recorded commit 1983ac5c…; handmade; on-disk sha256:628800b4…
pass  hello-world: pin hello-world@1.0 digest sha256:cff230d09ea952d092daf99796d0c52ec5bfb92d86f13021af902ce7b6b36720
pass  humanevalfix: pin humanevalfix@1.0 recorded commit ab02ff13…; handmade; on-disk sha256:5926e73e…
pass  terminal-bench-sample: pin terminal-bench-sample@2.0 recorded commit 7e917f35…; handmade; on-disk sha256:5130a7b7…
5 benches, 0 fail
```

`scripts/premerge.sh`
```
All checks passed!
62 passed in 1.03s
Found 33 diagnostics
premerge green: Python 3.12; ty 33 <= 33
```

Tests: `tests/test_fetch.py` injects FakeHarbor (no real harbor/Docker/network). Covers unpinned/`@latest`, MANIFEST sections, no-op, digest drift, `--verify-sample` `-n`≤2.

Scratch copies: `fetch-list.txt`, `live-fetch.txt`, `live-MANIFEST.md`, `audit.txt`, `premerge.txt`.
