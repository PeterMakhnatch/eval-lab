# tb2-to-tb21-fixes

Rebuild: `uv run evallab failures ingest tb2-to-tb21-fixes`.

- Source: the **26 modified PR #53 tasks** in `research/answer-keys/tb2-to-tb21-v1/items.jsonl` (SHA-256 `76c4e615f3d19ad3ec198d7011de3a60e75c2a70061e4d94d632a749c2ff5d21`), joined to `research/experiments/breaker-rediscovery-v1/tb2-diff.json` (SHA-256 `989eae62e76dff1aa4e3169087d16d8adbc0814870a8926af7f51d03025b2cc5`). The 26 format-only controls are excluded.
- Before: **harbor-framework/terminal-bench-2@69671fbaac6d67a7ef0dfec016cc38a64ef7a77c**, retaining each old task-package digest.
- After: **harbor-framework/terminal-bench-2-1@7131e4375048a0e408a8fb404b5f499d726b695b**, retaining the after-package digest and repository in evidence notes, and the full after revision in both `fix_revision` and `fixed_in_revision`.
- Count/tier: **26 V2_upstream_fixed findings**. `upstream_fix` means a pinned maintainer version change, not an executed differential or proof that a current unrelated package is fixed. Resource or oracle changes do not by themselves establish a reward-hack mechanism.
- Mapping is deliberately conservative: explicit formerly unstated signatures/paths and the read-only database restriction → TS05; the corrected SSH user/auth flow → TS06; explicit acceptance of alternate valid build/model layouts and extra compiled artifacts → TS07; removal of accessible setup patch files → TS03. **17 changes remain `unclassified`**, including resource bumps and dependency/oracle adjustments. No blanket weak-test label is copied from the old key's coarse category.
- Locator: the original TB2 child key item ID. Split is copied unchanged: **26 TEST**. These findings never feed DEV knowledge merely because an older tuning exercise saw a task.
- License: the before-pin Git tree has no root LICENSE entry, so no blanket license is assigned (`null`). The after-pin TB2.1 root license is Apache-2.0; task-specific licenses are not reinterpreted. Only factual version/diff metadata is redistributed; no new execution or paid compute was performed.
