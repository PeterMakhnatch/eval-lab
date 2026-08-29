---
type: study-report
topic: zai-opencode-mcp-pilot
author: pilot-executor
date: 2026-08-29
status: complete
epistemic: observed repeated-cell outcomes on six seed-42 cells; no capability, reliability, ranking or cost claims
collection: trajectory-analysis
reviewed: 2026-08-29
authorized_by: Peter (execution), Main (evidence promotion)
artifact: research/evidence/runs
---

# Z.ai GLM-5.3-Flash MCP Pilot — 2026-08-29

## Executive result

OpenCode, Harbor 0.21, Z.ai Coding Plan, ATIF v1.7 capture, and Eval Lab's three MCP benchmark categories executed end to end on the Darwin workstation.

The primary wave contained 18 completed trials across six repeated cells. Harbor recorded zero trial exceptions after host adaptation. Verifiers awarded reward 1.0 on 15/18 trials:

| Category / cell | Repetitions | Reward 1.0 | Observed mean reward | Prompt tokens | Completion tokens | Cached tokens | ATIF steps | Tool calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Function DAG easy | 3 | 2 | 0.667 | 94,859 | 922 | 62,592 | 14 | 14 |
| Action Memory clean 4k | 3 | 3 | 1.000 | 96,167 | 948 | 67,584 | 15 | 27 |
| Action Memory neutral padding 16k | 3 | 3 | 1.000 | 201,390 | 7,053 | 164,352 | 20 | 201 |
| Action Memory semantic distractor 16k | 3 | 2 | 0.667 | 275,590 | 7,144 | 216,064 | 23 | 202 |
| Recovery transient HTTP 5xx, persistence 1, fault arm | 3 | 2 | 0.667 | 142,365 | 1,464 | 115,200 | 20 | 16 |
| Recovery matched clean twin | 3 | 3 | 1.000 | 70,377 | 521 | 52,288 | 12 | 6 |
| **Total** | **18** | **15** | **0.833** | **880,748** | **18,052** | **678,080** | **104** | **466** |

These are observed outcomes from repeated attempts on six specific seed-42 cells, not estimates of general model capability, reliability, or relative rank.

## Exact execution lane

- Model: `zai-coding-plan/glm-5.3-flash`.
- Agent: Harbor's native OpenCode adapter, subclassed locally to link a read-only Z.ai-only auth secret into OpenCode's XDG auth store.
- OpenCode version recorded in every primary ATIF document: `1.18.25`.
- Harbor: `0.21`, with native OpenCode JSON-to-ATIF conversion.
- Trajectory schema: `ATIF-v1.7`.
- Repetitions: `--n-attempts 3`, `--n-concurrent 1`, `--max-retries 0` for each primary cell.
- Model access: the user's Z.ai Coding Plan credential. No per-run monetary cost field was recorded, so this report makes no cost claim.

A host-level OpenCode canary returned exactly `READY`. A Harbor Function DAG canary then received reward 1.0 before the primary wave.

## Cell construction

### Function DAG

Registered `syn-funcdag-easy` task, repeated three times.

### Action Memory

- Clean 4k cell, seed 42.
- Matched 16k dose pair, seed 42:
  - `neutral_padding`
  - `semantic_distractor`

The 16k pair used the final dose-ladder materializer and identical dose/seed with arm as the declared delta.

### Recovery

Matched seed-42 pair for `transient-http-5xx`, persistence 1:

- fault arm
- clean twin

The fault-arm verifier required causal recovery evidence; the clean twin required the same write/read outcome without injected fault.

## Observed failure modes

### Function DAG: malformed final artifact

One trial computed the target correctly but wrote a diagnostic scalar before the JSON document. The verifier rejected `/app/output/result.json` with:

`Invalid JSON format: Extra data: line 2 column 1 (char 2)`

The trajectory shows the shell command printing the numeric value and then the JSON payload into the same file.

### Action Memory semantic 16k: duplicate retrieval

One semantic-distractor trial made 66 context-chunk reads where the verifier expected 65. One chunk ID was retrieved twice. The verifier returned:

- `reason`: `incomplete_or_reordered_context_retrieval`
- `expected_reads`: 65
- `observed_reads`: 66

The other two semantic trials made the required retrieval sequence and passed. All three neutral 16k trials passed.

### Recovery fault arm: missing causal recovery mutation

One fault-arm trial eventually wrote and read the record, but the verifier marked `causal_mutation=false` and awarded 0.0. The failed sequence retried after read/fallback diagnosis without the designated recovery mutation. Both passing fault-arm trials used `refresh_auth` before the successful write/read sequence. All three clean twins passed.

## Evidence validation

`uv run evallab trajectories` validated all 18 primary documents:

- 18/18 ATIF documents: `valid`
- 18/18 completed trials: no Harbor exception
- 104 projected steps
- 466 projected tool calls

After the six primary jobs completed, a credential scan loaded the single secret field from the temporary Z.ai-only auth file and the 32-byte Recovery evidence key, and searched every regular file in all six primary job trees for four encodings of each value — raw bytes, lowercase hex, standard base64 and URL-safe base64. Eight needles total, zero matching artifact files. The scan never printed the secret.

A second scan of both Recovery job trees for the 32-byte evidence-envelope key returned `0` matching artifact files.

The temporary filtered auth file, Recovery evidence key, staged task copies and wheelhouse files were deleted after the runs. The user's normal OpenCode credential store was not modified or read beyond selecting the Z.ai provider entry for the temporary file.

## Host adaptations and validity boundary

The canonical task packages target CPython 3.12 manylinux x86_64 and fail-closed network isolation. The execution host was Apple Silicon Darwin with Docker Desktop, which cannot enforce Harbor's canonical `no-network`/allowlist modes.

The staged task copies therefore recorded these adaptations:

1. `network_mode` adapted to `public` for the agent and verifier phases where Darwin could not enforce the canonical mode.
2. Main, MCP sidecar, and Recovery verifier images forced to `linux/amd64` so the reviewed cp312/manylinux x86_64 wheel manifest could be used under emulation.
3. The main agent service received a public Docker network in addition to the internal MCP network so OpenCode could install and reach the Z.ai provider; MCP sidecars remained attached only to the internal network.

The exact 68-wheel reviewed FastMCP manifest was used, including `joserfc==1.7.4`; post-download provenance verification passed before materialization.

These adaptations preserve verifier behavior and artifact validation, but they do not establish enforced network isolation on Darwin. The auth secret was also readable inside the trusted task container during the OpenCode turn because the experimental adapter used a read-only secret mount rather than a credential-isolating proxy. The artifact scans found no disclosure, but the lane should remain limited to reviewed/trusted tasks until a proxy-based credential boundary and Linux isolation host are used.

## Durable evidence

The six primary jobs are promoted as durable, redacted bundles under
`research/evidence/runs/`. Each bundle carries `PROMOTION.json` recording every
source file's action, unredacted parent SHA-256 and promoted SHA-256:

- `research/evidence/runs/zai-flash-funcdag-easy-r3-20260829`
- `research/evidence/runs/zai-flash-action-clean4k-r3-amd64-egress`
- `research/evidence/runs/zai-flash-action-neutral16k-r3-amd64-egress`
- `research/evidence/runs/zai-flash-action-semantic16k-r3-amd64-egress`
- `research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier`
- `research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier`

Every promoted `agent/trajectory.json` is R1-redacted (system/user step text
replaced by a digest marker, `message_sha256` and `message_chars` kept) and
stays a valid ATIF-v1.7 document. OpenCode raw model I/O and runtime state —
`agent/opencode.txt` and the whole `agent/opencode/**` tree including the
SQLite `opencode.db`/`opencode.db-wal`/`opencode.db-shm` store, `log/opencode.log`,
`snapshot/**`, `repos/**`, `locks/**` and the XDG `auth.json` credential link —
is omitted entirely and digest-recorded under R2; none of it survives in the
bundles. Promotion enumerates symlinks explicitly and never dereferences them:
each `auth.json` is a symlink to the host credential store, so it is recorded
as an R2 omission with `entry_type: "symlink"` and the SHA-256/length of its
*link-target string* (the link itself, never the target's content). Any symlink
outside an R2 omission path is refused outright, and `--verify` rejects any
symlink found in a promoted bundle and re-checks every recorded link-target
digest. Verify at any time with:

```bash
uv run python scripts/promote_codex_bundle.py --verify
```

The original job directories in the experiment worktree (under `runs/zai-opencode/`)
documented setup diagnostics (nested mount, unsupported Darwin network
enforcement, x86 wheelhouse on arm64 images, missing agent egress, and verifier
platform mismatch). Those earlier jobs were excluded from model outcome counts
because they failed before a valid scored trial.

## Supported conclusions

- The Z.ai/OpenCode/Harbor path is operational for these three benchmark categories on the current host after explicit, recorded Darwin adaptations.
- All 18 primary trajectories are valid ATIF v1.7 and project through Eval Lab's trajectory reader.
- The observed repeated-cell outcomes were 15/18 reward 1.0, with exact verifier-specific failure evidence above.
- The paired pilot observations were 3/3 neutral versus 2/3 semantic at Action Memory 16k, and 3/3 clean versus 2/3 fault for Recovery. With one seed and three attempts per arm, these differences are descriptive only.

## Not supported

- General Z.ai GLM-5.3-Flash capability or reliability estimates.
- Model rankings or comparisons with other providers/models.
- Claims requiring enforced no-network execution.
- Cost or throughput forecasts.
- Causal claims from the small repeated-cell contrasts.
