# Provable wrong-pass differentials (pT-SELF-1)

**Question.** On how many cross-key-clean DEV tasks does a wrong shortcut provably pass the
original grader and fail the repaired grader, with both grades retained from an executed run?

**Answer.** **44, up from 31.** `tools/verify_differentials.py` re-reads the retained bytes behind
every row of `ledger.json`: 44 verified, 0 rejected, 0 unverifiable (`report.json`).

No run, container, cloud call or model call produced these numbers. The 13 additions are
retained executed grades that no cross-key-checked count had collected.

## What counts

A task counts when all of these hold, each re-checked by the tool:

- the wrong candidate scored the original grader **1.0** and the same candidate scored the
  repaired grader **0.0**, at a JSON pointer in a retained file whose sha256 matches;
- both arms are recorded as executed, on a stated backend that is not `host`, with no
  infrastructure exception on either arm;
- the task is affirmatively `dev`/`train` in `research/answer-keys/splits/assignments.csv` with
  no non-dev row under any key (the production predicate in `seeker_worker.py`).

Two levels: `native_differential_with_controls` also retains reference `[1,1,1]` and nop
`[0,0,0]` (the tier-1 rule); `native_differential_single` is one measured pair, which the trust-tier
policy calls tier 2.

| Level | Tasks | Where they come from |
|---|---:|---|
| with controls | 30 | the DB's tier-1 MiMo patch-witness differentials |
| single pair | 14 | `fin-saccr-rwa` (TB4 replayed witness, tier 1) plus the 13 additions |
| **total** | **44** | 31 already tier 1 in the DB, **13 beyond it** |

## The 13 additions

| Family | Tasks | Evidence |
|---|---:|---|
| October 13 EnvCheck historical partial repairs | 6 | Docker mutation report (original 1.0) and repair check (repaired 0.0); one script digest binds both arms |
| HAR-168 retained patches regraded | 2 | Daytona original 1.0; Modal repaired 0.0. `000792` is a source-bound projection of the patch (digests differ), not an exact match |
| Muse seeker swarm wave 5 | 3 | per-arm `result.json`; one deterministic shared template, not model-authored; backend unstated |
| Breaker Bench DEV pairs, local | 2 | benchflow 0.2.0 exploit 1.0, 0.2.2 0.0, in Docker; repaired oracle 1.0 and nop 0.0; bound by exploit class name only |

Five of the 13 (`000838`, `002139`, `002391`, `002552`, `002680`) already sit in the store as tier-2
confirmed issues; this adds the executed proof and the cross-key check behind them. The other
eight are not in the store.

All six October 13 repairs are historical *partial* repairs. They prove the differential for the
pinned witness and package, not that the task is now fixed.

## What is not counted, and why

| Excluded | Count | Reason |
|---|---:|---|
| Breaker SWE pairs `pytest-5809`, `pylint-7277` | 2 | repaired oracle scores 0 (`task_broken`): the repaired task is broken |
| Benchflow 0.2.0 → 0.2.2 upstream summary rows | 25 | executed upstream, but no raw per-trial receipts are retained here |
| TS-2 / TS-3 differential flags in this lab's seeker cells | 17 / 49 tasks | rewards recorded, but no container grader receipt; the candidates ran in local scratch directories |
| `executed-outcomes-v1` builder rows | 15 | repaired zero was hardcoded from the subset control, not graded |
| TB4 witnesses with host-side port replays | 4 | repaired grade is a host run, not a container run |
| MiMo weak-tests v2 / v2.1 witnesses | 114 tasks | the zero in their controls is the nop control, not a repaired grade |

## Still open

28 cross-key-clean DEV tasks have no retained executed differential: 13 TB4 tasks that already
carry both grader digests in the store, and 15 MiMo v2.1 weak tasks that are not in the store. Closing
them needs new native grades, which is a spend decision: nothing here grants it.

The store itself is unchanged. It has no storage-level proof gate for tier 1 (`upsert_task_issue`
and `assign_trust_tiers` accept any valid tier), so importing these rows is a decision for the
cloud-DB lane, with `ledger.json` as the proof to import against.

## Reproduce

```bash
uv run python research/evidence/runs/pt-self1-verified-differentials-20261027/tools/verify_differentials.py \
  research/evidence/runs/pt-self1-verified-differentials-20261027/ledger.json \
  --assignments research/answer-keys/splits/assignments.csv \
  --database ~/.local/state/evallab-prod-live/cloud-trace-db.sqlite
```

The cited files live outside the repository (`R` = `eval-lab-results`, `P` = the live store); point
them elsewhere with `--root R=/path`. Exit status is 0 when everything verifies, 1 on any
rejection, 2 when a cited file is absent. An absent file is never counted.

`assignments.csv` SHA-256 at verification, recomputed by the tool:
`7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`.
