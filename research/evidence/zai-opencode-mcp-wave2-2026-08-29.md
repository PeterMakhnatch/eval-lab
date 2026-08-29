---
type: study-report
topic: zai-opencode-mcp-wave2
author: Main
date: 2026-08-29
status: complete
epistemic: observed multi-seed and paired-lane outcomes; infrastructure failures excluded from model denominators; no capability, reliability, ranking, causal or cost claims
collection: trajectory-analysis
reviewed: 2026-08-29
authorized_by: Peter
artifact: research/evidence/runs
---

# Z.ai OpenCode MCP Wave 2 — 2026-08-29

## Executive result

Wave 2 expanded the first pilot across Function DAG difficulty, Action Memory 64k, two additional Recovery fault classes, an accessible full GLM-5.3 mini-lane, repeated seed-1337 Action trials, and one sequential-retrieval scaffold intervention.

- **27 scored trials**, **18 reward-1.0**, zero exceptions among scored trials.
- **29 valid ATIF v1.7 trajectories**: the 27 scored trials plus two default-timeout scaffold attempts whose partial trajectories were captured.
- **2 non-scored `AgentTimeoutError` attempts** at the default scaffold timeout.
- **1 observed provider-access error** for `glm-5.3-highspeed`; the subscription returned HTTP 429 before a model outcome. That job was cancelled and is excluded.
- Scored-trajectory totals: **18,724,686 prompt**, **110,022 completion**, **18,095,360 cached tokens**, **752 ATIF steps**, **2,915 tool calls**.

These are observed outcomes from specific tasks, seeds, lanes and scaffolds. They are not model capability, reliability or ranking estimates.

## Scored outcomes

| Lane | Family / cell | Arm or scaffold | Trials | Reward 1.0 |
|---|---|---|---:|---:|
| GLM-5.3-Flash | FuncDAG depth 5 | seeds 42/101/2024 | 3 | 3 |
| GLM-5.3-Flash | FuncDAG high name similarity | seeds 42/101/2024 | 3 | 2 |
| GLM-5.3-Flash | Action Memory 64k | neutral, unscaffolded | 4 | 1 |
| GLM-5.3-Flash | Action Memory 64k | semantic, unscaffolded | 4 | 1 |
| GLM-5.3-Flash | Recovery persistent-signature | clean/fault | 4 | 4 |
| GLM-5.3-Flash | Recovery silent-wrong-payload | clean/fault | 4 | 4 |
| GLM-5.3-Flash | Action Memory 64k seed1337 | sequential scaffold, timeout ×3 | 2 | 1 |
| GLM-5.3 | FuncDAG depth 5 | seed42 | 1 | 1 |
| GLM-5.3 | Action Memory 64k semantic | seed42 | 1 | 0 |
| GLM-5.3 | Recovery persistent-signature fault | seed42 | 1 | 1 |
| **Total** | | | **27** | **18** |

Lane subtotals are Flash **16/24** and full GLM-5.3 **2/3**. The lanes overlap on only three cells with one trial per lane; no comparison or ordering is supported.

## Function DAG

- Depth 5: Flash 3/3 and full GLM-5.3 1/1.
- High name similarity: Flash 2/3.
- The failed high-name-similarity trial propagated `23` where the verifier expected `-39`; `dag_conf=False`, `val_prop=0.4`.

The four observed depth-5 successes do not establish that depth is saturated. High name similarity is a current candidate difficulty axis because one of three trials failed, not because three trials establish a general effect.

## Recovery

All nine scored Recovery trials passed:

- Flash persistent-signature: clean 2/2, fault 2/2.
- Flash silent-wrong-payload: clean 2/2, fault 2/2.
- Full GLM-5.3 persistent-signature fault: 1/1.

Every Flash fault trial recorded `exact_injections=true` and `causal_mutation=true`; clean twins recorded zero injected faults. Persistent-signature trials used `refresh_auth`. Silent-wrong-payload trials used `fallback_query`; one also used `refresh_auth`. This weakens a simple one-repair-move or blind-identical-retry account for these two fault classes, but does not settle the three unrun fault classes or prove diagnosis.

## Action Memory 64k

### Outcome boundary

Unscaffolded outcomes:

- Flash seed42: neutral 1/1, semantic 1/1.
- Flash seed1337: neutral 0/3, semantic 0/3.
- Full GLM-5.3 seed42 semantic: 0/1.

Across the nine unscaffolded Action64 trials, reward was 1.0 on 2/9. Outcomes differed by seed and lane within the observed cells, but seed also changes content, opaque identifiers and ordering. No isolated seed, arm, model-tier or pure-capacity effect is established.

### Issued-handle audit

The machine-readable audit is `research/evidence/zai-wave2-action64-handle-audit.json`.

For all six Flash seed1337 failures:

- The listed set contained 257 handles.
- Issued counts were `[257, 257, 257, 257, 257, 259]`.
- Every trial omitted the same expected handle, `ctx_2110473c018845ab0cc32bf4`.
- Five substituted the near-identifier ending `32bf6`; one used `32bf3`.
- Every trial had exactly **256 unique valid-content handles**.
- Five produced one application-level `not_found`; the 259-call trial produced two, repeated the near-identifier, and duplicated a valid final handle.
- First order mismatch indexes varied; the six signatures were not identical.
- ATIF tool issuance order exactly matched benchmark-event order in every trial, ruling out server/event-capture reordering as the source of the discrepancy.

Flash seed42 neutral and semantic controls issued the exact 257 listed handles in order. Full GLM-5.3 seed42 issued all 257 plus a duplicate final handle and reordered calls.

The supported mechanism is agent-side batching, opaque-handle transcription and sequence maintenance. The evidence does not support a pure context-capacity claim or the statement that all failures retained complete valid coverage.

## Sequential-retrieval scaffold

The exact appended scaffold is preserved at `research/evidence/scaffolds/action-memory-sequential-retrieval-v1.md`.

Default timeout:

- Two attempts ended in `AgentTimeoutError` while still issuing one-by-one reads.
- They are harness-budget outcomes and are excluded from reward denominators.

With `agent_timeout_multiplier=3`:

- Neutral completed 257/257 reads and passed.
- Semantic stopped after 232/257 reads and failed.
- Neutral consumed 6,683,558 prompt tokens; semantic consumed 7,454,261.
- Combined scaffold prompt tokens were 14,137,819.
- The nine unscaffolded Action64 trials averaged 412,753 prompt tokens, range 227,610–539,198.

The scaffold therefore increased prompt use roughly 16–18× per scored trial, produced one pass and one incomplete failure, and required a longer budget. It is not a general fix. One-tool-call-per-model-turn repeatedly replays accumulated context and is an unsuitable broad-ladder scaffold.

## Next discriminating experiment

Before a broad dose ladder, run a small matched C1→C2 manipulation that holds seed, content, order, model, adapter and timeout constant while changing only handle representation/issuance:

1. Opaque IDs with existing list/get calls.
2. Indexed handles or numeric positions.
3. A range/batch retrieval tool that returns contiguous chunks in canonical order.

Required checks: listed-vs-issued set equality, valid-content coverage, unknown-handle count, duplicate count, order equality, manipulation identity/digest, opportunity denominator, unintended-delta refusal and token/latency cost. This tests the observed mechanism without conflating semantic arm, content or seed.

## Evidence and security

Promoted bundles:

- `zai-wave2-funcdag-depth5-s42`
- `zai-wave2-flash-matrix`
- `zai-wave2-glm53-funcdag-canary`
- `zai-wave2-glm53-paired-mini`
- `zai-wave2-action64k-s1337-repeats`
- `zai-wave2-action64k-s1337-sequential-scaffold`
- `zai-wave2-action64k-s1337-sequential-scaffold-t3`

Promotion verification covers 619 files across 16 total durable bundles with zero failures. All 16 bundles were re-promoted as schema v2 after the security review: root `job.log`, per-trial `trial.log`, `agent/codex.txt`, `agent/opencode.txt`, and OpenCode runtime/session trees are digest-recorded R2 omissions, so prompt text redacted from ATIF is not republished through a raw log. Raw, hex, standard-base64 and URL-safe-base64 scans for the filtered Z.ai credential and Recovery evidence key found zero matches in retained regular evidence files. The pinned adapter removed its auth link after normal and raised runs; a broken link left by cancelling the inaccessible Highspeed job was removed manually and that job was not promoted.

The temporary auth file, Recovery key, materialized tasks, trusted wheelhouse and run configs were deleted after settlement. Darwin public-network and linux/amd64-emulation limitations remain the same as wave 1.

## Prohibited claims

This wave does not support:

- General capability, reliability or model ranking.
- A semantic-distractor effect at 64k.
- A deterministic seed effect.
- A pure context-capacity mechanism.
- Scaffold effectiveness beyond the two observed timeout-3 trials.
- Claims requiring enforced no-network execution.
- Monetary cost or throughput forecasts.
