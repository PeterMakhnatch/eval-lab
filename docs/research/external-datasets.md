# External trajectory datasets: catalog and ingestion specification

Role: DATA-STRATEGY. Date: 2026-08-15. Confidence labels: **[verified]** =
schema/content confirmed by direct inspection; **[reported]** = from dataset
cards/search metadata, unverified. Verify before first ingest.

## 1. Why external trajectories matter here

The lab's analytics (facts, cohorts, P4 queries) currently run only on
trajectories the lab itself generates — small-N, one machine, subscription
budget. Public corpora provide millions of steps of *someone else's* compute
for calibrating fact extractors, stress-testing the Parquet projection, and
building failure-taxonomy baselines across models the lab will never run
directly. They are Zone 01 material (see `docs/data-architecture.md`): useful,
auditable, never trusted as local evidence.

## 2. Catalog

### Tier A — ATIF-native (ingestable via existing `atif.py`)

| Dataset | Contents | Format | Status |
|---|---|---|---|
| [`SJCaldwell/proofjudge-eval-traces`](https://huggingface.co/datasets/SJCaldwell/proofjudge-eval-traces) | 2,706 proof-review traces; 11 model-specific JSONL shards | **ATIF v1.6** | [verified: one pinned 246-row shard fetched and projected] |
| [`benchflow/ClawsBench`](https://huggingface.co/datasets/benchflow/ClawsBench) | 7,834 productivity-agent traces across four harnesses | **ATIF v1.6** | [reported] |
| [`kendx/Harbor-Adapter`](https://huggingface.co/datasets/kendx/Harbor-Adapter) | Harbor trial directories as checksum-bearing tar archives plus a filterable manifest | Harbor job dirs with ATIF where emitted | [reported] |
| Harbor-Index baseline trials (Harbor Hub) | **1,476 trials** behind the Harbor-Index 1.0 leaderboard, frontier agent-model pairs | Harbor job dirs incl. ATIF | [reported — harbor-index.org publishes "82 tasks and all 1,476 trials"] |
| [`obaydata/mcp-agent-trajectory-benchmark`](https://huggingface.co/datasets/obaydata/mcp-agent-trajectory-benchmark) | 49 MCP agent trajectories (38 single-pass, 11 multi-conv), full tool traces | **ATIF v1.2** explicit | [reported] |
| `yoonholee/terminalbench-trajectories` (HF) | Terminal-Bench trial traces, row = one trial with step/tool/observation trace | ATIF-adjacent | [reported] |

Harbor's own docs state the SFT exporter consumes ATIF from Terminus-2,
OpenHands, Claude Code, and Gemini CLI adapters — so any Harbor-run corpus
published as job directories is Tier A by construction.

**Not a trajectory corpus:** `harborframework/terminal-bench-2.0` on HF is a
**read-only task mirror** (tasks + Git LFS assets, Apache-2.0), primary source
`github.com/harbor-framework/terminal-bench-2` [verified via README fetch].
Useful as a benchmark pin for Zone 01, not as trajectory data.

### Tier B — OpenHands/SWE-agent format (needs a converter)

| Dataset | Scale | Producer scaffold | Notes |
|---|---|---|---|
| `nvidia/SWE-Zero-openhands-trajectories` | **318k trajectories** | OpenHands | agentic SFT corpus [reported] |
| `nvidia/Open-SWE-Traces` | 200k+ | SWE-agent + OpenHands | [reported] |
| `nebius/SWE-agent-trajectories` | **80,036** | SWE-agent | targets SWE-bench-extra + SWE-bench dev [reported] |
| `nvidia/Nemotron-SWE-v1` | 59k | OpenHands | [reported] |
| `nvidia/SWE-Hero-openhands-trajectories` | 34k | OpenHands | [reported] |
| `nebius/SWE-rebench-openhands-trajectories` | multi-turn SWE | OpenHands v0.54.0, Qwen3-Coder-480B-A35B | scaffold+model pinned in card [reported] |
| `SWE-Gym/OpenHands-SFT-Trajectories` (+ Sampled) | — | OpenHands | early SWE-Gym corpora [reported] |

Model coverage across Tier B skews heavily toward open-weight producers (Qwen,
DeepSeek-tuned, Nemotron); Claude/Codex/Gemini traces appear mostly in Tier A
(Harbor-run) corpora. That asymmetry is itself analytically useful: Tier B
gives open-model failure baselines at scale, Tier A gives frontier-model
behavior at small N.

### Model and harness coverage

| Requested coverage | Public evidence surface | Boundary |
|---|---|---|
| Claude | ProofJudge has a Claude Haiku shard; ClawsBench and [`Contextbench/Tracebench`](https://huggingface.co/datasets/Contextbench/Tracebench) report Claude Sonnet traces | Versions/scaffolds differ; do not pool by vendor name |
| Codex | Harbor-Adapter exposes cells where `agent=codex`; ClawsBench names Codex among four harnesses | **Codex is a harness/scaffold here, not a model family**; group it with its model ID |
| Gemini | ProofJudge has two Gemini shards; ClawsBench reports Gemini models and Gemini CLI | ProofJudge's live sample below is GPT, not Gemini |
| Llama | [`McGill-NLP/agent-reward-bench`](https://huggingface.co/datasets/McGill-NLP/agent-reward-bench) includes Llama 3.3 WebArena traces | Non-ATIF, browser domain; converter required |
| DeepSeek | [`cx-cmu/agent_trajectories`](https://huggingface.co/datasets/cx-cmu/agent_trajectories) reports DeepSeek-R1/V3.2 across six benchmarks; Tracebench reports V3.2 | Message/manifest formats, not native ATIF |

This table establishes availability, not comparability. A valid comparison
still matches model revision, agent/scaffold, benchmark/task, tool surface,
timeouts, and verifier.

### Tier C — adjacent, evaluate before use

`AI45Research/ATBench` (safety-oriented trajectory benchmark). Safety
diagnosis focus; schema unknown.

## 3. Token/cost fields: what to expect

- ATIF (v1.0–1.7, per `src/evallab/atif.py` SUPPORTED_SCHEMA_VERSIONS):
  step-level metrics including token counts where the adapter recorded them;
  the lab's `StepFact`/`ToolCallFact` projection already extracts optional
  int/float metrics defensively (`_optional_int`/`_optional_float`).
- OpenHands SFT exports: typically message-list format with per-message
  content but **often no per-step token accounting** — token counts must be
  recomputed with a tokenizer if needed, and cost fields are usually absent.
  Treat token/cost as nullable everywhere; never impute silently.
- ProofJudge v1.6 stores prompt/completion totals in `final_metrics`; its
  selected sample has no step-level token or cost values. The live projection
  therefore yields trajectory totals while `steps.prompt_tokens` stays null.

## 4. Ingestion specification for `src/evallab/fetch.py`

`fetch.py` already enforces the right discipline for benchmark pins: immutable
`name@version` (never `@latest`; `parse_pin` refuses `latest/head/main/master`),
`material_digest` over the fetched tree, license detection, task counting, and
audit rows, all behind a `HarborBackend` Protocol seam. The extension for
trajectory corpora reuses every one of those pieces:

### 4.1 Source seam (implemented prototype)

```python
class PublicAtifSource:
    repo_id: str
    revision: str       # exactly 40 lowercase hex characters
    filename: str
    sha256: str         # digest of downloaded bytes
```

`fetch_public_atif` downloads one HTTPS file anonymously, verifies its expected
SHA-256 before parsing, and accepts an injected byte-downloader for tests.
**Pin rule extends unchanged:** revision is a 40-hex commit SHA; branch names
are refused exactly as `@latest` is today. There is no credential parameter or
environment lookup. A gated dataset is unsupported.

### 4.2 Prototype pipeline

```text
fetch_public_atif(PublicAtifSource, output_root=derived/parquet)
  → anonymous HTTPS download from an immutable commit URL
  → SHA-256 comparison before any parse or destination write
  → split JSONL in a temporary workspace, preserving each record's bytes
  → existing atif.py validation + export_trajectories projection
  → provenance.json: source URI, revision, digest, license, timestamp, Zone 01
  → atomic rename to derived/parquet/external/<item_id>/
  → repeat call audits the sidecar + Parquet and returns status=noop
```

This prototype intentionally accepts ATIF JSONL only. Whole-repository
snapshots and non-ATIF converters remain future, versioned acquisition paths;
they must preserve raw bytes outside `derived/` before supporting publications
that depend on the upstream corpus remaining available.

Invariants (from `docs/architecture.md` and CHECKS.md, restated as contract):

1. Raw downloads are immutable once digested; converters write new files.
2. External Parquet lands under `derived/parquet/external/`, physically
   separate from local-evidence Parquet — no query can accidentally union
   zones without naming both paths (see P4).
3. Every ingest emits an audit row (dataset, revision, digest, license, bytes,
   files, trajectories parsed/failed) — same shape as the existing bench audit.
4. Tests inject `TrajectorySourceBackend` fakes (deterministic-test rule: no
   network, no HF cache, no host state).

### 4.3 Verified first target

`PROOFJUDGE_ATIF_SAMPLE` pins
`SJCaldwell/proofjudge-eval-traces@aac1f0f4c96e8394da6315a04778e4b7f13ac900`,
file `data/traces_qwen3-32b.jsonl`, content SHA-256
`79b7d3e71d28af6dc1630cb135d697c035a4e74de5eb9226db6e1c0cd3ee17fb`.
The 5,940,391-byte file has 246 JSONL records and includes full agent tool
steps plus trajectory token totals. The acceptance run validated 246/246 rows
as ATIF v1.6 and projected 246 trajectories, 1,746 steps, 1,028 tool calls, and
1,028 observations. DuckDB summed 8,688,317 prompt and 952,971 completion
tokens, and the Zone 01 sidecar matched the pinned revision and source digest.
The smaller MCP corpus remains a useful follow-up for testing older ATIF v1.2.

## 5. What is deliberately out of scope

- No OpenHands→ATIF converter in this phase (spec only; it is a versioned
  Zone 03-adjacent transform with its own tests).
- No gated/token-requiring datasets, ever, under the current credential policy.
- No training-data export (Zone 04 concerns, P3).
