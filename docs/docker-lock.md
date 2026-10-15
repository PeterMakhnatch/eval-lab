---
status: living
audience:
  - builder
  - operator
---

# Docker FIFO lock (`evallab docker-lock`)

A host-wide, first-in-first-out slot lock for local Docker, shared by every
process, tab, and worktree on this Mac. It implements the FOCUS rule
"Docker: max 2 containers per tab; use the shared Docker FIFO lock once
Infra ships it": the per-tab container cap stays local to each caller, while
this lock serialises the heavy Docker work *between* tabs so several agents
do not saturate the daemon at once.

The lock is **advisory**: only callers that cooperate (run under
`docker-lock run`, or hold `evallab.docker_lock.docker_slot`) are
serialised. Anything that starts containers directly is unaffected.

## Usage

Run a command under a slot (the common case):

```bash
uv run --no-sync evallab docker-lock run my-label -- docker compose up
uv run --no-sync evallab docker-lock run --slots 2 my-label -- sh -c 'echo hi'
uv run --no-sync evallab docker-lock run --timeout 60 my-label -- make test
```

`run` takes `LABEL` then the command with its arguments (`CMD [ARGS...]`),
acquires a slot, runs the command as a child with inherited stdio, releases
the slot, and exits with the child's exit code. `SIGINT`/`SIGTERM` received
by the wrapper are forwarded to the child; the slot is still released. With
`--timeout SECONDS`, a caller that cannot get a slot in time exits `124`
without running the command, and its queue entry is removed. While waiting,
the wrapper prints its queue position and the current holder to stderr
(once per position change, not on every poll), and prints again when the
slot is acquired.

Inspect the queue (stale entries are pruned before printing):

```bash
uv run --no-sync evallab docker-lock status
uv run --no-sync evallab docker-lock status --json
uv run --no-sync evallab docker-lock status --name other-lock
```

In Python, use the context manager:

```python
from evallab.docker_lock import docker_slot

with docker_slot(label="my-job"):  # slots=1, name="docker" by default
    ...
```

## CheatBench drop-in

CheatBench's `run_batch.sh` already accepts a lock wrapper of the form
`<wrapper> <label> <command...>` via `CB_DOCKER_LOCK`. Point it at this
lock:

```bash
CB_DOCKER_LOCK='uv run --no-sync evallab docker-lock run' \
  research/experiments/cheatbench-port/run_batch.sh <specs-dir> "<actor>"
```

Readiness and every trial then run one at a time under the shared slot, so
other tabs using the same lock interleave fairly instead of piling onto
Docker simultaneously.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `--slots N` / `$EVALLAB_DOCKER_LOCK_SLOTS` | `1` | How many queue entries may hold a slot (run) at once, host-wide. |
| `--name NAME` | `docker` | Lock name; selects `<name>.queue.json` + `<name>.mutex` in the state dir. Separate names are independent queues. |
| `--timeout SECONDS` (`run` only) | none (wait forever) | Maximum wait for a slot; exits `124` on expiry. |
| `$EVALLAB_DOCKER_LOCK_DIR` | `~/.cache/evallab/docker-lock/` | State directory shared by all tabs/worktrees. Point it at a tmp dir to isolate tests. |

## Semantics

- **FIFO, host-wide.** Callers append a queue entry
  (`ticket`, `pid`, `host`, `label`, `cwd`, `enqueued_at`, `started_at`,
  `heartbeat_at`) to `<name>.queue.json` in the state dir. Only the first
  `slots` entries in queue order hold a slot; a later waiter never jumps an
  earlier one. Queue reads/writes happen under `flock` on the mutex file,
  and the queue file is replaced atomically.
- **Slots are a global queue, not a per-tab container cap.** With the
  default `--slots 1`, one heavy Docker job runs at a time across all tabs.
  Each tab still keeps its own "max 2 containers" discipline inside the
  command it runs under the slot; the lock does not count containers.
- **Stale pruning.** An entry is stale when its pid is not alive on this
  host, or its `heartbeat_at` is older than the TTL (120 s). Holders and
  waiters heartbeat their own entry about every 15 s from a daemon thread,
  so a crashed process frees its slot promptly and pid reuse cannot pin a
  slot forever. `status` prunes stale entries too.
- **Release is unconditional.** The entry is removed when the command
  finishes, is signalled, or the Python block exits — including on
  exceptions — and releasing twice is harmless.
