# Library crack sweep (2026-10-12)

Every task under `library/tasks` ran through `evallab interop matrix` on
**Harbor 0.24.0** and **Inspect** (inspect-ai 0.3.276 + inspect-harbor 1.0.0).
Each ran with the scripted oracle, nop and full cheat-ladder agents, both as
shipped and under the hardened setup (separate verifier + `agent-network-none@1`).
Everything ran on local Docker with no model calls, so the sweep cost $0.
Per-cell verdicts, rewards, reasons and versions are in
`research/evidence/library-crack-sweep-20261012/cells.json`.

## Crack table

Each platform cell reads oracle / nop / cheat. A sound task shows `pass / fail / clean`.

| Task | Setup | Harbor | Inspect |
|---|---|---|---|
| transaction-reconciliation | shipped | pass / fail / **cracked** | error / error / **cracked** |
| transaction-reconciliation | fixed (`task-separate-verifier@1`) | pass / fail / clean | pass / fail / cracked¹ |
| transaction-reconciliation | hardened (fixed + `agent-network-none@1`) | pass / fail / clean | pass / fail / cracked¹ |
| query-optimize | shipped | pass / fail / **cracked** | pass / fail / clean |
| query-optimize | fixed (`task-separate-verifier@1`) | pass / fail / clean | pass / fail / clean |
| query-optimize | hardened (fixed + `agent-network-none@1`) | pass / fail / clean | pass / fail / clean |
| event-summary | shipped | pass / fail / clean | pass / fail / clean |
| event-summary | hardened | pass / fail / clean | pass / fail / cracked¹ |
| release-branch-rescue | shipped (0.1 oracle) | pass / fail / clean | fail² / fail / clean |
| release-branch-rescue | hardened (0.1 oracle) | **fail³** / fail / clean | fail² / fail / clean |
| release-branch-rescue | shipped, oracle fixed | pass / fail / clean | not run |
| release-branch-rescue | hardened, oracle fixed | pass / fail / clean | not run |
| terminal-bench-html-js-filter | shipped | pass / fail / clean | fail² / fail / cracked¹ |
| terminal-bench-html-js-filter | hardened | pass / fail / clean | fail² / fail / cracked¹ |
| syn-funcdag-easy / medium / hard | shipped | pass / fail / clean | pass / fail / cracked¹ |
| syn-funcdag-easy / medium / hard | hardened | pass / fail / clean | pass / fail / cracked¹ (medium: error⁴) |
| tau3-retail-1 | shipped | error⁵ | error⁵ |

1. **Inspect grades separate-verifier tasks in the agent's container.**
   `inspect_harbor` 1.0.0 has no separate-verifier mode, so
   `environment_mode = "separate"` is ignored and the cheat ladder's tamper
   attacks reach the grader again. The task cannot fix this; it is a
   platform limitation. Use Harbor for these tasks whenever the reward feeds
   GEPA, SFT or RL. event-summary was clean on Inspect in one run and cracked
   in the other.
2. On Inspect the oracle fails for tasks whose grader needs the bundled
   verifier image (`tests/Dockerfile` setup that inspect_harbor does not
   build). This is the same limitation as note 1.
3. release-branch-rescue's oracle exited 141 (SIGPIPE): `grep -m1` closed the
   pipe early, git died writing the reflog, and `set -o pipefail` failed
   `solve.sh`. The failure is racy, so the shipped run passing was luck. It
   is fixed in place (`solution/solve.sh` now uses `awk` and reads the whole
   reflog). After the fix, both shipped and hardened controls are correct.
4. Infrastructure flake: the Inspect sandbox died (`service "default" is not
   running`). Shipped transaction-reconciliation's Inspect controls hit a
   similar flake while copying `reward.json`. Its cheat cell still ran and
   cracked.
5. tau3-retail-1 is not runnable model-free. Harbor 0.24 refuses the scripted
   `oracle`/`nop`/cheat agents because the task declares MCP servers they do
   not support, and Inspect cannot start its Compose topology. It is
   untested, not clean.

## Fixes

The shipped `transaction-reconciliation` and `query-optimize` packages stay
byte-identical. Their digests are pinned by `library/registry/*.json`,
`library/frozen/gym-v1/manifest.json` and `policy/canary-suite.yaml`. The
fixes are derived variants with tracked lineage records; rebuild either from
its parent with `evallab.task_variants.materialize(record, parent_dir)`.

| Task | Variant | Change | Why |
|---|---|---|---|
| transaction-reconciliation | `a90ff693ee10` (`task-separate-verifier@1`) | `environment_mode = "separate"`, `tests/Dockerfile`, artifact `/app/data/ledger.db`. The stdlib `tests/verify.py` replaces `test_outputs.py`, so there is no `apt`/`curl`/`uv` at grade time; the interpreter is called by absolute path and the DB is opened read-only | Shipped grading ran `test.sh` in the agent's container. `tamper_reward_writer` (detached loop holding `/logs/verifier/reward.*` at 1) cracked it with no fix |
| query-optimize | `99d7984aca98` (`task-separate-verifier@1`) | `environment_mode = "separate"` (fresh copy of the task image, no verifier image), artifact `/app/sol.sql` | Shipped grading ran in the agent's container. The full ladder cracked it; the executed attacks were `reward_plant`, `skip_plant`, `tamper_shadow_timeout` and `tamper_reward_writer`. A fresh copy receives only `sol.sql`, so none of them survive |
| release-branch-rescue | in place, `solution/solve.sh` | SIGPIPE-safe reflog lookup | Racy oracle failure (exit 141). The task is not registry-pinned |

Hardened variants (`agent-network-none@1`, lineage records under
`library/task-variants/`):

| Task | Hardened variant |
|---|---|
| transaction-reconciliation | `8075401a8c07` (on `a90ff693ee10`) |
| query-optimize | `f17c814a0f49` (on `99d7984aca98`) |
| event-summary | `fb256a608721` |
| release-branch-rescue | `790070f84a4e` (oracle fixed) |
| terminal-bench-html-js-filter | `30476ca110d5` |
| syn-funcdag-easy / medium / hard | `e4bd32dc2583` / `59922826130a` / `dd609936a783` |

## Reproduce

```bash
uv sync --frozen --extra laminar --group xplat-verifiers
uv run evallab interop matrix library/tasks/<task> --targets harbor inspect --agents oracle nop cheat --json
uv run evallab interop matrix derived/task-store/variants/<slug>/<digest12> --targets harbor inspect --agents oracle nop cheat --json
```

The scripted agents need no network, so adding `agent-network-none@1` changed
no verdict here. The sweep checks grading integrity, not whether egress was
actually blocked.
