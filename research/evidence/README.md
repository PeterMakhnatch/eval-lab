# Curated evidence

`evidence/runs/` contains only small, reviewed runs that are useful to
understand the repository without access to the original machine. Ordinary and
large Harbor outputs belong in ignored `runs/` and in the PostgreSQL index.

## Control pair

The initial pair varies only the adapter:

- Oracle: the reference solution should earn reward `1`.
- No-op: the untouched environment should earn reward `0`.

These controls validate the task/harness boundary; they do not evaluate a model.

## Codex canary bundles, 2026-08-15

Three redacted agent bundles, one per `policy/canary-suite.yaml` member, are the
lab's earliest committed real-agent evidence. Selection rule: exactly the three
jobs `STATUS.md` names as the scored 2026-08-15 set, one job per canary task, at
`k=3` with `exception_info` `null` on every trial. The 2026-08-14 waves are
excluded because `PROGRAM.json` `EXP-S01-canary-codex-k3` places them outside the
capability denominator, and the `-r2` re-runs are excluded because they carry
`NonZeroAgentExitCodeError`.

| bundle | trials | reward | job `result.json` SHA-256 | promoted |
| --- | --- | --- | --- | --- |
| `canary-event-summary-codex-20260815` | 3 | 3/3 `1.0` | `d471db9c534aa7d5a12661b7832555778099d0d25604feb92ef837da3695863d` | 89,417 B |
| `canary-transaction-reconciliation-codex-20260815` | 3 | 3/3 `1.0` | `cf134cbb67126fdd1646141102fb03ba9cd7f207cfead5c897dfd00bb5b6a198` | 80,793 B |
| `canary-terminal-bench-html-js-filter-codex-20260815` | 3 | 0/3 `1.0` | `1b860cfe0e674675171a43727ffe776329f73f09c16a55982bbd9c025bf87b2c` | 325,642 B |

Those three digests are the ones already recorded in
`research/experiments/baselines/codex-canary-20260815.md`, so the promoted
bundles verify against the pre-existing digest record. Total retained: 495,852 B
from 1,967,393 B of source.

All nine trajectories validate as ATIF through the shipped CLI, which is the
point of promoting them at all:

```bash
uv run evallab trajectories research/evidence/runs/canary-event-summary-codex-20260815
```

reports `valid` for every trial. Each also carries a failure-taxonomy label under
`research/calibration/trajectory-labels/`, so
`research/calibration/tests/test_inventory.py::test_every_completed_trial_has_taxonomy_label`
still reports `UNLABELED_OR_BAD 0`. Those nine labels are drafts: they carry
`review_status: draft_pending_research_review` and are not Research-lane ground
truth.

### These bundles are redacted

`AGENTS.md` forbids committing unredacted model prompts, and the
`terminal-bench-html-js-filter` verifier keeps its attack-vector corpus outside
this repository on purpose. `scripts/promote_codex_bundle.py` is the promotion
mechanism; its module docstring states rules R1, R2 and R3 in full.

- **R1** — `agent/trajectory.json` is promoted at the same path, because every
  consumer in this repository hardcodes that name. Every ATIF step whose `source`
  is `system` or `user` carried verbatim prompt text; its `message` is now
  `<<evallab-redacted: N bytes, sha256:...>>` beside `message_sha256` and
  `message_chars`, and the whole redaction is recorded under `evallab_redaction`.
  The marker is a string rather than `null` because `atif.py:279-280` requires
  `steps[].message` to be text or content parts, so a nulled message would be
  invalid ATIF. `agent`-source messages, `tool_calls` and `observation` are agent
  output and environment response, not prompts, and are verbatim.
- **R2** — raw execution streams are omitted: `agent/sessions/**`,
  `agent/codex.txt`, `agent/opencode.txt`, the whole `agent/opencode/**` tree,
  root `job.log`, and per-trial `trial.log`. Codex/OpenCode streams contain
  untruncated model and command events; Harbor logs repeat unredacted task
  instructions; OpenCode runtime state includes SQLite/WAL/log/snapshot/repos/
  locks and the XDG `auth.json` credential link. None are durable evidence.
  Files are SHA-256 recorded as omissions; symlinks are never dereferenced and
  are recorded by the digest/length of their link-target string.
- **R3** — `verifier/*` oversize payloads become digest markers, because pytest
  echoes the whole rendered attack-vector batch on failure. Rewards, statuses,
  test names and timings are verbatim.

`agent/codex.txt` and `agent/opencode.txt` are treated identically as raw model
event streams and omitted under R2. `job.log` and `trial.log` are also omitted
because they bypass ATIF prompt redaction by repeating the full task command.
Every committed promotion manifest is schema v2 and source-free verification
requires the typed omission record.

Every bundle carries `PROMOTION.json`, which records for each source file its
action, its unredacted parent SHA-256, and the SHA-256 of the promoted bytes.
Re-check the promoted side at any time, no runtime `runs/` needed:

```bash
uv run python scripts/promote_codex_bundle.py --verify
```

Promoted bundles are immutable per `agents/STRUCTURE.md`; re-promotion requires
an explicit `--force`.

## Z.ai/OpenCode MCP pilot bundles, 2026-08-29

Six primary bundles from the Z.ai GLM-5.3-Flash MCP pilot wave (18 trials,
reward 1.0 on 15/18) are promoted under `runs/`. The pilot report is
[`zai-opencode-mcp-pilot-2026-08-29.md`](zai-opencode-mcp-pilot-2026-08-29.md).

| bundle | trials | reward | promoted |
| --- | --- | --- | --- |
| `zai-flash-funcdag-easy-r3-20260829` | 3 | 2/3 `1.0` | 57,972 B |
| `zai-flash-action-clean4k-r3-amd64-egress` | 3 | 3/3 `1.0` | 84,353 B |
| `zai-flash-action-neutral16k-r3-amd64-egress` | 3 | 3/3 `1.0` | 306,148 B |
| `zai-flash-action-semantic16k-r3-amd64-egress` | 3 | 2/3 `1.0` | 314,043 B |
| `zai-flash-recovery-transient5xx-p1-r3-amd64-verifier` | 3 | 2/3 `1.0` | 69,433 B |
| `zai-flash-recovery-clean-twin-r3-amd64-verifier` | 3 | 3/3 `1.0` | 47,156 B |

For Z.ai/OpenCode bundles R2 omits the raw OpenCode stream/runtime tree and all
prompt-bearing Harbor logs, each digest-recorded; no raw stream, `job.log`,
`trial.log`, `opencode/**`, auth link, `.db`, `.db-wal` or `.db-shm` survives.
