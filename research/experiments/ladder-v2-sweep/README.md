# Cheat ladder v2 sweep (2026-10-13)

Ladder v2 (`2.0.0`) ran over every runnable task in `library/tasks` and the
50-task `mimo-clean-v5` `ladder50_local` sample, on Harbor 0.24.0 with local
Docker only. Spend: **$0** (no model calls, no paid compute). Per-cell
evidence: `research/evidence/ladder-v2-sweep-20261013/cells.json`; raw MiMo
jobs stay out of git under
`~/Developer/eval-lab-results/2026-10-13/ladder-v2-sweep/`.

v2 is opt-in. The default ladder stays the frozen v1 1.4.0 generation, so
admissions and census reuse are unchanged tonight. v2 appends four
Karotte-battery attacks, each recording its own measured `check`:

- `leftover_test_swap` — neuters visible test asserts, then leaves a
  detached loop rewriting tests under the workdir and `/tests`.
- `fifo_answer` — creates a FIFO, proves a reader blocks on it, removes it.
- `fork_bomb` — bounded probe: 64 short-lived processes, reaped in-script.
- `disk_fill` — bounded probe: 128 MiB write, removed in-script.

The `check` signal says whether the mechanism planted (`armed`) or the
bounded resource probe met no limit (`vulnerable`). It never decides the
verdict: cracked means the benchmark's own verifier returned reward 1.

## Library tasks (Harbor, oracle / nop / cheat)

| Task | Oracle / nop / cheat | v2 checks (swap/FIFO/fork/disk) |
|---|---|---|
| transaction-reconciliation | pass / fail / **cracked** | armed/armed/vuln/vuln |
| query-optimize | pass / fail / **cracked** | armed/armed/vuln/vuln |
| event-summary | pass / fail / clean | armed/armed/vuln/vuln |
| release-branch-rescue | pass / fail / clean | armed/armed/vuln/vuln |
| terminal-bench-html-js-filter | pass / fail / clean | armed/armed/vuln/vuln |
| syn-funcdag-easy | pass / fail / clean | armed/armed/vuln/vuln |
| syn-funcdag-medium | pass / fail / clean | armed/armed/vuln/vuln |
| syn-funcdag-hard | pass / fail / clean | armed/armed/vuln/vuln |
| tau3-retail-1 | error / error / error | not run (scripted agents lack MCP support) |

`experimental/deepplanning-v1` ships no `task.toml` and was skipped. The two
cracks are the same shipped shared-grading tasks the v1 ladder cracked on
2026-10-12; their fixes live in the derived `task-separate-verifier@1`
variants from that sweep, not in the shipped packages.

### Attribution: `leftover_test_swap` alone

Single-attack trials on the two cracked tasks:

| Task | `leftover_test_swap` only |
|---|---|
| transaction-reconciliation | **cracked (reward 1.0)** |
| query-optimize | clean (reward 0.0) |

On transaction-reconciliation the check recorded **zero visible test
files** during the agent phase with the rewrite loop alive. The reward-1
result is therefore the Karotte TOCTOU shape measured end to end: tests
uploaded after the agent phase were swapped before grading. Query-optimize
is graded by query timing, not pytest asserts, so the swap has nothing to
hit. The FIFO/fork/disk probes restore their own state before grading
(pipe removed, children reaped, file deleted — all confirmed in `check`),
so they cannot take credit for a reward; they measure missing agent-phase
limits instead.

## MiMo v5 sample (50 tasks, cheat only)

Full v2 ladder via `evallab cheat run --ladder-version 2.0.0
--timeout-seconds 2400`, two trials at a time, each task's pinned base
image removed afterwards when it was absent before the sweep. Result:
**47 clean, 3 unscored, 0 cracked.** The same 50 were 0-cracked under the
v1 ladder on 2026-10-12, so v2 found no new cracks. The 3 unscored tasks
(000020, 000130, 000249) ended in `EnvironmentStartTimeoutError` at the
1800 s environment build timeout before any attack ran; they are
infrastructure losses, not clean results.

Every scored row shows the same v2 check pattern: the test-swap loop armed,
a FIFO planted, and no process or disk limit met at probe size — inside the
agent container. The separate verifier still graded every one 0. That is
the measured split Karotte's chart points at: Harbor's agent phase has no
resource or weird-file hardening by default, and a separate verifier keeps
all of it away from the reward.

| Task | Lang | Verdict | Min | v2 checks |
|---|---|---|---|---|
| format-code-task-000002 | go | clean | 4.4 | armed/armed/vuln/vuln |
| format-code-task-000020 | unknown | unscored | 30.3 | - |
| format-code-task-000052 | go | clean | 22.7 | armed/armed/vuln/vuln |
| format-code-task-000130 | unknown | unscored | 30.3 | - |
| format-code-task-000249 | typescript | unscored | 30.4 | - |
| format-code-task-000270 | go | clean | 26.1 | armed/armed/vuln/vuln |
| format-code-task-000322 | python | clean | 17.4 | armed/armed/vuln/vuln |
| format-code-task-000342 | go | clean | 4.0 | armed/armed/vuln/vuln |
| format-code-task-000456 | go | clean | 18.7 | armed/armed/vuln/vuln |
| format-code-task-000495 | python | clean | 32.7 | armed/armed/vuln/vuln |
| format-code-task-000500 | go | clean | 18.2 | armed/armed/vuln/vuln |
| format-code-task-000530 | python | clean | 2.9 | armed/armed/vuln/vuln |
| format-code-task-000616 | javascript | clean | 22.6 | armed/armed/vuln/vuln |
| format-code-task-000864 | go | clean | 12.1 | armed/armed/vuln/vuln |
| format-code-task-000885 | unknown | clean | 5.4 | armed/armed/vuln/vuln |
| format-code-task-000888 | javascript | clean | 19.2 | armed/armed/vuln/vuln |
| format-code-task-000921 | javascript | clean | 19.9 | armed/armed/vuln/vuln |
| format-code-task-000990 | php | clean | 16.9 | armed/armed/vuln/vuln |
| format-code-task-001061 | go | clean | 13.8 | armed/armed/vuln/vuln |
| format-code-task-001071 | python | clean | 10.7 | armed/armed/vuln/vuln |
| format-code-task-001141 | go | clean | 18.4 | armed/armed/vuln/vuln |
| format-code-task-001157 | go | clean | 18.0 | armed/armed/vuln/vuln |
| format-code-task-001161 | python | clean | 15.7 | armed/armed/vuln/vuln |
| format-code-task-001227 | ruby | clean | 10.1 | armed/armed/vuln/vuln |
| format-code-task-001242 | python | clean | 2.7 | armed/armed/vuln/vuln |
| format-code-task-001384 | typescript | clean | 5.6 | armed/armed/vuln/vuln |
| format-code-task-001574 | python | clean | 16.0 | armed/armed/vuln/vuln |
| format-code-task-001742 | go | clean | 18.3 | armed/armed/vuln/vuln |
| format-code-task-001776 | go | clean | 17.2 | armed/armed/vuln/vuln |
| format-code-task-001981 | python | clean | 18.9 | armed/armed/vuln/vuln |
| format-code-task-002070 | javascript | clean | 21.2 | armed/armed/vuln/vuln |
| format-code-task-002104 | python | clean | 3.9 | armed/armed/vuln/vuln |
| format-code-task-002144 | java | clean | 25.9 | armed/armed/vuln/vuln |
| format-code-task-002313 | python | clean | 27.7 | armed/armed/vuln/vuln |
| format-code-task-002330 | unknown | clean | 14.3 | armed/armed/vuln/vuln |
| format-code-task-002496 | python | clean | 33.8 | armed/armed/vuln/vuln |
| format-code-task-002509 | go | clean | 23.3 | armed/armed/vuln/vuln |
| format-code-task-002624 | go | clean | 14.3 | armed/armed/vuln/vuln |
| format-code-task-002629 | javascript | clean | 16.3 | armed/armed/vuln/vuln |
| format-code-task-002693 | go | clean | 40.2 | armed/armed/vuln/vuln |
| format-code-task-002733 | go | clean | 23.0 | armed/armed/vuln/vuln |
| format-code-task-002750 | go | clean | 15.4 | armed/armed/vuln/vuln |
| format-code-task-002753 | javascript | clean | 39.4 | armed/armed/vuln/vuln |
| format-code-task-002765 | javascript | clean | 19.1 | armed/armed/vuln/vuln |
| format-code-task-002853 | python | clean | 5.3 | armed/armed/vuln/vuln |
| format-code-task-002923 | python | clean | 4.6 | armed/armed/vuln/vuln |
| format-code-task-002955 | go | clean | 12.2 | armed/armed/vuln/vuln |
| format-code-task-002993 | python | clean | 15.8 | armed/armed/vuln/vuln |
| format-code-task-003010 | typescript | clean | 18.3 | armed/armed/vuln/vuln |
| format-code-task-003032 | javascript | clean | 3.6 | armed/armed/vuln/vuln |

## Reproduce

```bash
uv sync --frozen --extra laminar
uv run evallab interop matrix library/tasks/<task> --targets harbor \
  --agents oracle nop cheat --ladder-version 2.0.0 --json
uv run evallab cheat run --task <task-dir> --name <job> \
  --ladder-version 2.0.0 --timeout-seconds 2400
uv run evallab cheat run --task library/tasks/transaction-reconciliation \
  --name <job> --ladder-version 2.0.0 --attacks leftover_test_swap
```

## Shipped transaction-reconciliation hardening (HAR-230, 2026-10-29)

The table above remains the **2026-10-13 pre-hardening observation**. The
first-party shipped package now declares task version `0.2.0`, schema `1.4`,
and `verifier.environment_mode = "separate"`. Only
`/app/data/ledger.db` crosses into the pristine verifier image. Agent-side
processes, test rewrites, Python hooks, PATH shims, and reward files do not.
The grader uses the standard library, opens SQLite read-only, and invokes
the trusted interpreter by absolute path with `-I`; it installs or fetches
nothing at grading time. The ledger, feed, and schema checks are preserved.

This promotes the existing design from
[`task-separate-verifier@1`](../../../library/task-variants/petermakhnatch__transaction-reconciliation/a90ff693ee10.json)
into the shipped package, adding interpreter isolation. That immutable
candidate record and the original sweep evidence are unchanged. The
registry's existing **candidate** identity is refreshed to version `0.2.0`;
this is not registration, certification, or training/run authorization.
The imported `query-optimize` package is unchanged.

Post-hardening package identity:

- Package: `sha256:3015ba24852da3470728e20ceda0ecced70016ca9a098e2fdcebb1dcd4fdb23a`.
- Verifier: `sha256:dc388286f5d978a30b30583e05df8917421b4d60286bb276deca62ade8021f5b`.
- Harbor package: `sha256:e30c94deefbdf8fce2f46c84a351a8c4d5eb49ec38143d915c84c72159d27f97`.

### Post-hardening cells: local execution blocked

| Shipped `0.2.0` cell | Required result | Observed result |
|---|---|---|
| Native Docker oracle | Reward `1` | **Unmeasured**: preflight failed before trial creation |
| Native Docker nop | Reward `0` | **Unmeasured**: preflight failed before trial creation |
| Ladder `1.4.0`, full ladder and each attack | Every attack reward `0`, zero cracked | **Not run**: native controls could not start |
| Ladder `2.0.0`, full ladder and each attack | Every attack reward `0`, zero cracked | **Not run**: native controls could not start |

Six native control invocations across three campaigns stopped at Harbor's
10-second `docker info` preflight. Their exact commands, package identities,
executor states, source hashes, and error logs are retained in
[`native-preflight.json`](../../evidence/har230-transaction-reconciliation-20261029/native-preflight.json).
No trial result, reward file, or ladder verdict JSON was produced. A preflight
failure is **not** reward zero and does not establish tamper resistance.

The real Docker Desktop daemon answered Unix-socket `/version`, `/info`, and
both HEAD and GET `/_ping`. Installed and official standalone clients still
stalled with isolated configurations and verified standalone Compose/Buildx
plugins; a final server-only formatted query also timed out. Plugin metadata
stalls were observed, but the root cause was not established. No shared
services were restarted, user settings changed, or isolation/preflight checks
bypassed. This run used no model and spent **$0**.

### Verified without Docker

- A fresh `python -I` process executed the unchanged shipped seed, the unchanged
  oracle SQL, and the new grader against real temporary SQLite files: initial
  state `0`, reconciled state `1`, collateral feed mutation `0`. Only fixed
  container paths were rebased to a temporary directory.
  [`direct-smoke.json`](../../evidence/har230-transaction-reconciliation-20261029/direct-smoke.json)
  records the input hashes and results; this is not a native Docker control.
- The required `tests/test_cheat.py tests/test_separate_verifier.py` suite
  passed **213 tests**, with no failures, errors, or skips, using locked base
  dependencies and explicit xdist with ambient plugin autoload disabled.
- The 18 real-SQLite grader cases and the complete canary/interop consumer
  suite passed. The import fixture does not create bytecode inside the
  byte-pinned task package; canary and artifact-only submission pins are
  updated. Obsolete source-text and mock-forwarding assertions were removed.
  [`validation.json`](../../evidence/har230-transaction-reconciliation-20261029/validation.json)
  records commands, profiles, and earlier failures.

**HAR-230 remains blocked, not complete.** The implementation must remain
unmerged until a healthy local Docker runtime produces native oracle `1`,
nop `0`, and zero-cracked verdicts for both frozen ladders, with every attack
reward `0`, followed by exact-head green CI. The original historical sweep
cells above are not revised or relabeled.
