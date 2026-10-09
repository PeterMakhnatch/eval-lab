# Vals closure on the MiMo Python code domain (overnight slice)

Owner: ValsClosure. $0 only (local Docker `--network none`, registry/static
reads, file-local derivation). No model calls, no paid compute, no Exa.

Background: Vals [reward-hacking audit](https://www.vals.ai/blogs/mimo-reward-hacking)
(67% unreachable-fix census + ladder: unreachable → mtimes → pack parser →
caches → newer-release download → prompt wording). Python fleet = the
1,146 usable rows of `research/experiments/python-task-ledger/ledger.csv`
(non-Python is CodeHarden's; general/webdev is JudgeVariants').

## Per-route status

| Route | Status | Evidence |
|---|---|---|
| 1. Unreachable git objects (FineEnvs 1.2.0) | CLOSED (measured; no fix needed beyond ours) | `receipts/route1-fineenvs-120.md` |
| 2. Pack-parser escalation | CLOSED (check + fix) | `receipts/route2-pack-parser.md`, `probe-image-checks@3` in `src/evallab/exploit_probe.py` |
| 3. Cache search | CLOSED (check + fix) | `receipts/route3-cache-census.md`, `receipts/cache-census.csv`, `purge-build-caches@1` |
| 4. mtime fleet | CLOSED (10 validated / rest candidate) | `receipts/route4-mtime-fleet.md`, `derive_mtime_fleet.py` |
| 5. Prompt wording | STAGED (spec only, no model spend) | `prompt-ban-spec.md` |

Tasks covered by validation: 002552, 002402, 001269 (full strip→cache→mtime
chains: oracle 1 / nop 0 / caches 0 / mtime 1, except 001269 has no oracle);
002938, 000666, 002308, 000324, 002139, 000905, 001809 (strip→mtime: nop 0 /
mtime 1). Route 1 re-verified 002552, 002402, 001269 under the published
1.2.0 setup. Census: 11 images static + post-setup dynamics on 002552.

## Code changes (this branch; PR leaves open for morning integration)

- `src/evallab/purge_build_caches.py` (new): `purge-build-caches@1`;
  `tests/test_purge_build_caches.py` (new, 6 tests).
- `src/evallab/exploit_probe.py`: `probe-image-checks@2` → `@3`
  (verify-pack inventory + base comparison + findings rule);
  `tests/test_exploit_probe.py`: fixture + surplus test.
- `src/evallab/hardening.py`: registry entry for `purge-build-caches@1`
  (owned by this slice per CodeHarden).
- Lineage: 3 `purge-build-caches@1` records (validated), 3 chain-top
  `mtime-normalize@1` records (validated), ~1,135 fleet `mtime-normalize@1`
  records (candidate), 1 strip-chain mtime for 002938 (validated), 002361
  record (candidate; CodeHarden dropped theirs on PR #785).

## Residuals / open (for the parent)

- 1.2.0 closes on-ref + registry downloads only; unreachable-only images get
  no action (route 1). `strip-future-history@1` stays required.
- 002391/002486 ship `build/lib` + egg-info (census) — new
  installed-copy suspects for `purge-installed-copies@1`, not claimed here.
- 1,142 mtime candidates need grading-with-purge-guard evidence before
  training use; purge-inapplicable chains (e.g. 002938-purge) stay candidate.
- Prompt A/B needs paid approval (spec §cost: core 36 trials ≈$5.76
  expected/~$9 worst; full 108 over the HAR-202 $15 envelope).
- Non-Python caches (Go module, npm, cargo, m2) belong to CodeHarden's fleet.
