# query-optimize: the ladder crack and what full-hardening@1 does to it (2026-11-02, HAR-257)

`terminal-bench/query-optimize` (`library/tasks/query-optimize`) is the shipped
library task the cheat ladder cracks. This note names the attack that takes it,
then measures `evallab harden` (`full-hardening@1`) on the same package. All runs
are local Docker with the scripted cheat agent: **$0**, no model calls. Per-cell
evidence is in [`cells.json`](cells.json) (schema
`evallab.query_optimize_crack/v1`); the transform's own record is
[`hardening-manifest.json`](hardening-manifest.json).

**Answer:** the crack is `tamper_reward_writer`, a detached loop that overwrites
`/logs/verifier/reward.txt` and `reward.json` with `1`. The `full-hardening@1`
candidate **by itself cannot grade this task at all**: its L3 gate runs `python3`
inside the verifier container, and the separate verifier is a fresh copy of the
top-level environment image, `alexgshaw/query-optimize:20251031`, which has no
`python3`. Oracle, nop and the crack attack therefore all end with **no reward**
(trial error), not `0`. When the same candidate is given a verifier image that
can run the gate (`tests/Dockerfile` with `python3`), the oracle scores **1.0**,
nop **0.0**, and `tamper_reward_writer` **0.0**.

## The cracking attack

The single-attack cells identify the attack: every shipped-package cell where
`tamper_reward_writer` was the only attack paid `1.0`, and the three other
attacks isolated on 2026-10-09 paid `0.0`. The two full-ladder rows show the
crack persists across ladder generations but do not attribute it by themselves.

| date | setup | ladder | job / attempts | reward |
|---|---|---|---|---|
| 2026-10-09 | shipped, single attack | 1.2.0 | `~/Developer/eval-lab-results/2026-10-09/unknown-attr-query-optimize-tamper-reward-writer/attr-query-optimize-tamper-rewar__hboiPbf/agent/cheat/attempts.json` | **1.0** |
| 2026-10-09 | shipped, single attack | 1.2.0 | `~/Developer/eval-lab-results/2026-10-09/unknown-attr-query-optimize-reward-plant/` | 0.0 |
| 2026-10-09 | shipped, single attack | 1.2.0 | `~/Developer/eval-lab-results/2026-10-09/unknown-attr-query-optimize-skip-plant/` | 0.0 |
| 2026-10-09 | shipped, single attack | 1.2.0 | `~/Developer/eval-lab-results/2026-10-09/unknown-attr-query-optimize-tamper-shadow-timeout/` | 0.0 |
| 2026-10-12 | shipped, full ladder | 1.x (full) | [`research/evidence/library-crack-sweep-20261012/cells.json`](../../evidence/library-crack-sweep-20261012/cells.json) row `shipped-a` | 1.0 |
| 2026-10-13 | shipped, full ladder | 2.0.0 | [`research/evidence/ladder-v2-sweep-20261013/cells.json`](../../evidence/ladder-v2-sweep-20261013/cells.json) `library.rows`, `terminal-bench/query-optimize` | 1.0 |
| 2026-10-13 | shipped, single attack | 2.0.0 | same file, `attribution`: `leftover_test_swap` on `terminal-bench/query-optimize` | 0.0 |
| 2026-11-02 | shipped, single attack | 4.0.0 | `~/Developer/eval-lab-results/2026-11-02/HAR-257-har257-qo-shipped-tamper-reward-writer/har257-qo-shipped-tamper-reward__ZCNk5wG/agent/cheat/attempts.json` | **1.0** |

The 2026-11-02 cell is the cleanest attributable measurement: one trial, ladder
`4.0.0`, only `tamper_reward_writer` executed, Harbor 0.24.0, commit
`3e94127e88920c15c4bea4ec18ad6815b2f5229c`, empty `uncommitted.diff`. The task
directory has not changed since 2026-08-14
(`git log -1 -- library/tasks/query-optimize` → `88bdde243`), so that commit and
today's `main` hold the same task bytes.

## Why it works

`task.toml` has no `[verifier] environment_mode`, so the verifier is **shared**:
`tests/test.sh` runs in the agent container, and `/logs/verifier` is the trial's
verifier directory bind-mounted into that container. The attack starts two
detached `bash` loops that rewrite `reward.txt` (`1`) and `reward.json`
(`{"reward": 1.0}`) every 0.2 s. `test.sh` computes the real outcome and writes
`0`; the loop overwrites it; Harbor prefers `reward.json`.

The 2026-11-02 trial records both halves of that:

- `verifier/test-stdout.txt`: the task's own suite ended
  `4 failed, 2 passed in 613.71s` (`test_compare_golden_vs_solution_runtime`,
  `test_outputs_match_exactly`, `test_solution_contains_single_sql_query`,
  `test_solution_is_small` failed).
- `verifier/reward.json`: `{"reward": 1.0}`, and
  `verifier_result.rewards.reward` is `1.0` with `n_errors` 0.
- `agent/cheat/tamper_reward_writer.log`: `READBACK_TXT=1`,
  `READBACK_JSON={"reward": 1.0}` in the agent container.

So the verifier was not fooled about correctness; its **reward file** was
overwritten before Harbor read it.

## What `evallab harden` writes for this package

```bash
uv run evallab harden library/tasks/query-optimize \
  --out runs/har257-query-optimize-hardened --allow-degraded
```

`--allow-degraded` is required because the package has no measured reference test
count: the L3 gate then requires "at least one passing test". The transform
reports:

| layer | status | mechanism |
|---|---|---|
| L1 | applied | agent-created auto-load files deleted from the workdir |
| L2 | not-applicable | the grader trusts no workspace test or helper file |
| L3 | applied | pytest report injected into the original grader (`tests/test.sh`) |
| L4 | applied | `environment_mode = "separate"`; the workdir is the only artifact |

| | value |
|---|---|
| transform | `full-hardening@1`, shape `harbor`, workdir `/app`, runner `pytest` |
| source digest | `sha256:d593fa7d67498305ebcc32605455abd16e6d5495f6f455b458c024572617bad1` |
| output digest | `sha256:d5a026b8c0b133a7b834407950896457d5b0981f1de05d23be6b607d71ada36d` |
| expected report | `{"tests": 1, "skipped": 0}` (degraded gate) |

The generated `tests/test.sh` starts with
`EVALLAB_PY=$(command -v python3 || command -v python)` and runs
`"$EVALLAB_PY" -I -S /tests/_evallab/grade.py prepare`. Its verifier, however, is
a fresh copy of the top-level environment (Harbor 0.24
`resolve_verifier_environment_definition`): `alexgshaw/query-optimize:20251031`,
an amd64 Ubuntu image with `sqlite3` and `curl` and **no `python3`/`python`**
(`docker run --rm alexgshaw/query-optimize:20251031 sh -c 'command -v python3 || echo NO_PYTHON3'`
→ `NO_PYTHON3`). The gate line therefore expands to the empty string.

## Result on the harden candidate as produced

One job per cell, task `runs/har257-query-optimize-hardened`
(digest `sha256:d5a026b8…`):

```bash
P=runs/har257-query-optimize-hardened
uv run --extra laminar evallab run --task "$P" --agent oracle --name har257-qo-harden-oracle
uv run --extra laminar evallab run --task "$P" --agent nop    --name har257-qo-harden-nop
uv run --extra laminar evallab cheat run --task "$P" --name har257-qo-harden-tamper-reward-writer \
  --ladder-version 4.0.0 --attacks tamper_reward_writer
```

| cell | reward | verifier output |
|---|---|---|
| oracle | **none** (trial error `RewardFileNotFoundError`) | `/tests/test.sh: line 9: : command not found` then `evallab harden: could not prepare the workspace` |
| nop | **none** (`RewardFileNotFoundError`) | same |
| cheat `tamper_reward_writer` | **none** (`RewardFileNotFoundError`) | same |

Published jobs: `~/Developer/eval-lab-results/2026-11-02/HAR-257-har257-qo-harden-{oracle,nop,tamper-reward-writer}/`.
None of the three is a scored `0`: the verifier aborts at the L3 gate before the
task's tests run. Two facts still matter:

- The attack **executed** (`background writers hold reward at 1 (2 files)`,
  `READBACK_TXT=1` in `agent/cheat/tamper_reward_writer.log`), and its planted
  files are **absent** from the collected verifier directory (`verifier/` holds
  only `test-stdout.txt`). L4 is real: the verifier container mounts the same
  trial directory, but Harbor's separate-verifier path empties `/logs/verifier`
  before grading, and the agent container is stopped first.
- The candidate itself is **not gradable**, so this run cannot show the blocking
  by itself.

## Result with a verifier image that can run the gate

To separate "L4 blocks the writer" from "the candidate cannot grade", the same
harden output was given one file, `tests/Dockerfile`:

```dockerfile
FROM ubuntu:24.04
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3 curl ca-certificates
COPY . /tests
```

With `tests/Dockerfile` present, Harbor builds the verifier image from `tests/`
instead of copying the top-level environment, so the gate has `python3`, and
`curl` has a CA bundle for the `uv` install in the original `test.sh`. This file
is a **probe**, not part of what `evallab harden` writes. Candidate digest with
it: `sha256:91f63302830833c01b3e51d103797771f70153435b4f818dd10f2852da475b0c`
(the two probe jobs share Harbor task digest
`sha256:97fce05900907dbe32b6046f73818e2cf85a477a403cd440ed185fe02830f82a`).

| cell | reward | what the verifier did |
|---|---|---|
| oracle | **1.0** | original grader `6 passed`; gate `GATE_OK` (`tests` 6, `failures` 0, `errors` 0) |
| nop | **0.0** | original grader `4 failed, 2 passed`; gate `GATE_NO` (`failures` 4) |
| cheat `tamper_reward_writer` | **0.0** | attack executed and read back `1` in the agent container; verifier's own suite `4 failed, 2 passed`; gate `GATE_NO`; the verifier directory holds `reward.txt` `0` and **no** `reward.json` |

Published jobs:
`~/Developer/eval-lab-results/2026-11-02/HAR-257-har257-qo-harden-verifier2-{oracle,nop,tamper-reward-writer}/`.

The attack's own log (`attempts.json` + `tamper_reward_writer.log`) is identical
to the cracking run: it armed and read back `1`. The difference is where the
files land: the separate verifier's reset deletes them, and the writer processes
are gone with the agent container before grading starts.

## Reading

- **The transform closes the crack only if the verifier can grade.** On this
  package `full-hardening@1` produces a candidate whose verifier has no
  `python3`; every cell — oracle included — ends ungraded. That is not a clean
  task: it is an ungradable one.
- **With a verifier image that can grade, the crack is closed.** Oracle `1.0`,
  nop `0.0`, `tamper_reward_writer` `0.0`, with the attack demonstrably armed and
  its forged files removed.
- **For the harden-in-place versus sibling decision:** either arrangement needs
  the same additional verifier image for this package. A sibling copy under
  `derived/` with the same `tests/Dockerfile` would behave like the probe; a
  repair that only applies L1–L4 in place leaves `query-optimize` ungradable.
  The blocker is the verifier image, not the candidate's location.

## Limits

- One trial per cell; the scripted ladder is deterministic, so there is no
  variance estimate.
- Only `tamper_reward_writer` was isolated at ladder 4.0.0 on this package. The
  other 24 v4 attacks are not individually measured here; the 2026-10-09
  ladder-1.2.0 attribution cells for `reward_plant`, `skip_plant` and
  `tamper_shadow_timeout` paid `0.0`.
- The probe's `tests/Dockerfile` builds a native arm64 Ubuntu; the task's own
  image is amd64. It proves the gate and blocking path, not a qualified verifier
  image for the package.
- The first probe iteration omitted `ca-certificates`; the `uv` install failed
  and its oracle scored `0.0` for that reason. It is superseded by the rows above
  and is listed under `invalid_probe_iterations` in `cells.json`.
- A scripted ladder is not a model-driven red team. Model-driven attempts are out
  of scope here because they cost money.
