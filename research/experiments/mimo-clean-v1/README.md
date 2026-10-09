# mimo-clean-v1: canonical clean set for the Python code pool

Date: 2026-10-09. $0 paid compute (local Docker + local derivation only;
no model calls, no Modal/Daytona).

## What this is

One clean package per task the Python task ledger marks usable, each
carrying the canonical chain in its lineage:

```
ledger run package -> strip-future-history@1 -> purge-installed-copies@1 (scoped)
  -> purge-build-caches@1 -> mtime-normalize@1 -> separate-verifier@2 (+ reference solution)
```

- Builder: `src/evallab/mimo_clean.py`; CLI: `evallab mimo-clean build|verify-local`
- Design doc: `docs/mimo/clean-set.md`
- Manifest: `manifest.csv` in this directory (1,180 rows: 1,148 built, 32 skipped discards)

## Build result (2026-10-09, `evallab mimo-clean build --workers 8`)

| Status | Count | Detail |
|---|---|---|
| built | 1148 | every keep/fix ledger row |
| skipped | 32 | all `discard` verdicts, reason cites the ledger row |

Chain shapes over the 1,148 built:

| Chain | Tasks |
|---|---|
| `strip-future-history@1>purge-build-caches@1>mtime-normalize@1>separate-verifier@2` | 1146 |
| `strip-future-history@1>purge-installed-copies@1>purge-build-caches@1>mtime-normalize@1>separate-verifier@2` | 2 (`format-code-task-001269`, `format-code-task-002308`: purge carried in their repair run packages) |

- 192/1148 built tasks carry an oracle-pass reference fix
  (`solution/solve.sh` built from the HAR-191 patch; manifest `reference_fix`).
- Purge skips: 5 fail-closed with per-task reasons (002552 poetry py3.8,
  000792/002486 no project name, 002139 bitbake, 002391 PEP 668); the rest
  per the HAR-194 CONFIRMED_PURGE-only stance (see `docs/mimo/clean-set.md`).

## Reproducibility proof

The builder is deterministic and idempotent (content-addressed derives;
existing `(task, transform, parent)` records reused, never duplicated).
After deleting all 3,440 newly derived lineage records (~72 MB — the reason
only the manifest is committed), a from-scratch rebuild reproduced every
manifest `final_digest`:

- 12-task subset covering every chain shape (purge-carried, fail-closed,
  scope-skip, with/without reference fix, original/leak-closed/repair runs):
  **12/12 digests match**.
- Full fleet re-run: **1148/1148 digests match**, statuses identical
  (receipt: this README; repro manifests are scratch under `/tmp`, not committed).

Orphan packages (present without a record, e.g. after a store cleanup) are
moved aside, re-derived, reconciled by digest, and dropped on match — the
full re-run exercised exactly this path for ~1,136 tasks with zero
mismatches.

## Local acceptance (2026-10-09, `evallab mimo-clean verify-local`)

Per task on local Docker (`--network none` via each task's declared
policy), pinned `xiaomimimo/mimo-v2.6-rl-oss` images (pulled on demand):
oracle control (when a reference fix exists), nop control, and the full
12-attack cheat ladder v1.2.0 in-trial. Acceptance = **oracle 1, nop 0,
cheat clean**. Jobs:
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-09/mimo-clean-v1/`
(raw, out of git). 12 tasks spanning purge-carried / fail-closed /
scope-skip and with/without reference fix, including the six required
(002552, 001809, 002391, 000666, 001269, 002308):

| task | purge leg | oracle | nop | cheat (cracked/trials) | verdict |
|---|---|---|---|---|---|
| format-code-task-002552 | fail-closed (poetry py3.8) | 1 | 0 | 0/1 | PASS |
| format-code-task-001809 | scope-skip | 1 | 0 | 0/1 | PASS |
| format-code-task-002391 | fail-closed (PEP 668) | 1 | 0 | 0/1 | PASS |
| format-code-task-000666 | scope-skip | 1 | 0 | 0/1 | PASS |
| format-code-task-001269 | carried (CONFIRMED_PURGE) | n/a (no oracle-pass patch) | 0 | 0/1 | PASS |
| format-code-task-002308 | carried (CONFIRMED_PURGE) | n/a (no oracle-pass patch) | 0 | 0/1 | PASS |
| format-code-task-000085 | scope-skip | 1 | 0 | 0/1 | PASS |
| format-code-task-002402 | scope-skip | 1 | 0 | 0/1 | PASS |
| format-code-task-000792 | fail-closed (no project name) | n/a (no oracle-pass patch) | 0 | 0/1 | PASS |
| format-code-task-002139 | fail-closed (bitbake) | n/a (no oracle-pass patch) | 0 | 0/1 | PASS |
| format-code-task-002486 | fail-closed (no project name) | n/a (no oracle-pass patch) | 0 | 0/1 | PASS |
| format-code-task-001198 | scope-skip (ledger `fix` row) | n/a (no oracle-pass patch) | 0 | 0/1 | PASS |

**Acceptance: 12/12 pass.** Each cheat trial executed all 12 ladder attacks
(`agent/cheat/attempts.json`, status `executed` each); the clean `@2`
packages grade every one of them 0, including `skip_plant` and the tracked-
conftest/sitecustomize/reward-writer/source tamper attacks that crack the
un-cleaned packages per the cheat-tamper-ladder receipt.

Notes:

- 002552/001809 cells were derived from the first run and reused verbatim
  by the resumable second run (`verify-local` reuses completed job dirs,
  never clobbers).
- 002391's first nop trial died on Docker daemon network exhaustion
  (`all predefined address pools have been fully subnetted`, caused by 18
  stale zero-container networks from other lanes' finished runs); after
  pruning only those unreferenced networks, the `-attempt2` cell passed.
  The incomplete first attempt is preserved in the jobs dir.
- The cheat job counts one trial per task: the CheatAgent runs the whole
  12-attack ladder inside that trial (per-attack evidence in
  `agent/cheat/attempts.json`).

## Spend

$0.00 — no paid compute used (no `evallab spend day` needed; local Docker
and local derivation only). One shared-hygiene note: 18 stale
zero-container Docker networks from other lanes' finished runs were pruned
after they exhausted the daemon's subnet pool; no containers or lanes were
touched.
