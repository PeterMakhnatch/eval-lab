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
lab's only committed real-agent evidence. Selection rule: exactly the three jobs
`STATUS.md` names as the scored 2026-08-15 set, one job per canary task, at
`k=3` with `exception_info` `null` on every trial. The 2026-08-14 waves are
excluded because `PROGRAM.json` `EXP-S01-canary-codex-k3` places them outside the
capability denominator, and the `-r2` re-runs are excluded because they carry
`NonZeroAgentExitCodeError`.

| bundle | trials | reward | job `result.json` SHA-256 | promoted |
| --- | --- | --- | --- | --- |
| `canary-event-summary-codex-20260815` | 3 | 3/3 `1.0` | `d471db9c534aa7d5a12661b7832555778099d0d25604feb92ef837da3695863d` | 131,119 B |
| `canary-transaction-reconciliation-codex-20260815` | 3 | 3/3 `1.0` | `cf134cbb67126fdd1646141102fb03ba9cd7f207cfead5c897dfd00bb5b6a198` | 121,772 B |
| `canary-terminal-bench-html-js-filter-codex-20260815` | 3 | 0/3 `1.0` | `1b860cfe0e674675171a43727ffe776329f73f09c16a55982bbd9c025bf87b2c` | 413,759 B |

Those three digests are the ones already recorded in
`research/experiments/baselines/codex-canary-20260815.md`, so the promoted
bundles verify against the pre-existing digest record. Total added: 666,650 B
from 1,967,393 B of source.

### These bundles are redacted

`AGENTS.md` forbids committing unredacted model prompts, and the
`terminal-bench-html-js-filter` verifier keeps its attack-vector corpus outside
this repository on purpose. `research/evidence/promote_codex_bundle.py` is the
promotion mechanism; its module docstring states rules R1, R2 and R3 in full.

- **R1** — `agent/trajectory.json` becomes `agent/trajectory.redacted.json`.
  Every ATIF step whose `source` is `system` or `user` carried verbatim prompt
  text; its `message` is now `null` beside `message_sha256` and `message_chars`.
  `agent`-source messages, `tool_calls` and `observation` are agent output and
  environment response, not prompts, and are verbatim.
- **R2** — `agent/sessions/**` Codex rollout JSONL is omitted. It holds the
  untruncated request/response stream including `payload.encrypted_content`.
  Each omitted file's SHA-256 is recorded.
- **R3** — `verifier/*` oversize payloads become digest markers, because pytest
  echoes the whole rendered attack-vector batch on failure. Rewards, statuses,
  test names and timings are verbatim.

`agent/codex.txt`, the raw `codex exec --json` stream, is promoted verbatim: it
was checked and contains no vendor system prompt, no reasoning text and no
encrypted payload.

Every bundle carries `PROMOTION.json`, which records for each source file its
action, its unredacted parent SHA-256, and the SHA-256 of the promoted bytes.
Re-check the promoted side at any time, no runtime `runs/` needed:

```bash
uv run python research/evidence/promote_codex_bundle.py --verify
```

Promoted bundles are immutable per `agents/STRUCTURE.md`; re-promotion requires
an explicit `--force`.
