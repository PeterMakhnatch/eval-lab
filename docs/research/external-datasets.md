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
| Harbor-Index baseline trials (Harbor Hub) | **1,476 trials** behind the Harbor-Index 1.0 leaderboard, frontier agent-model pairs | Harbor job dirs incl. ATIF | [reported — harbor-index.org publishes "82 tasks and all 1,476 trials"] |
| `obaydata/mcp-agent-trajectory-benchmark` (HF) | 49 MCP agent trajectories (38 single-pass, 11 multi-conv), full tool traces | **ATIF v1.2** explicit | [reported] |
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

## 4. Ingestion specification for `src/evallab/fetch.py`

`fetch.py` already enforces the right discipline for benchmark pins: immutable
`name@version` (never `@latest`; `parse_pin` refuses `latest/head/main/master`),
`material_digest` over the fetched tree, license detection, task counting, and
audit rows, all behind a `HarborBackend` Protocol seam. The extension for
trajectory corpora reuses every one of those pieces:

### 4.1 New source seam (additive)

```python
class TrajectorySourceBackend(Protocol):
    """Seam beside HarborBackend; injected, never reached for host state."""
    def resolve_revision(self, repo_id: str, revision: str) -> str: ...
    def snapshot(self, repo_id: str, revision: str, dest: Path,
                 allow_patterns: list[str] | None) -> None: ...
```

Default implementation wraps `huggingface_hub.snapshot_download` with
`revision=<commit-sha>`. **Pin rule extends unchanged:** a HF ref is
`org/name@<40-hex-sha>`; branch names are refused exactly as `@latest` is
today. Anonymous access only — public datasets need no token, and the
subscriptions-only rule forbids introducing `HF_TOKEN` handling; a gated
dataset is simply unsupported.

### 4.2 Pipeline (all existing machinery)

```text
fetch trajectories <org/name@sha>
  → snapshot into library/benchmarks/_trajectories/<name>@<sha12>/   (Zone 01)
  → material_digest(dest)  +  detect_license(dest)                   (existing fns)
  → sidecar: ProvenanceMetadata JSON (P3 schema) with
      source_uri, revision_sha, material_digest, license, fetched_at, zone="01-external"
  → classify per file: ATIF? → atif.py validation (SUPPORTED_SCHEMA_VERSIONS)
      valid   → TrialTrajectoryProjection → Parquet under derived/parquet/external/<name>@<sha12>/
      invalid → recorded in ProjectionFailure audit, file kept, never mutated
  → non-ATIF (Tier B): stored + provenance only; conversion is a SEPARATE,
      versioned converter (converter name+version recorded in provenance),
      never inline in fetch
```

Invariants (from `docs/architecture.md` and CHECKS.md, restated as contract):

1. Raw downloads are immutable once digested; converters write new files.
2. External Parquet lands under `derived/parquet/external/`, physically
   separate from local-evidence Parquet — no query can accidentally union
   zones without naming both paths (see P4).
3. Every ingest emits an audit row (dataset, revision, digest, license, bytes,
   files, trajectories parsed/failed) — same shape as the existing bench audit.
4. Tests inject `TrajectorySourceBackend` fakes (deterministic-test rule: no
   network, no HF cache, no host state).

### 4.3 First target

`obaydata/mcp-agent-trajectory-benchmark` (49 trajectories, ATIF v1.2): small
enough to verify by hand, ATIF-native so it exercises the real validation path,
and multi-conversation rows stress the projection. Success criterion: 49/49
validate-or-audited, Parquet queryable by the P4 queries, provenance sidecar
present. The Harbor-Index 1,476-trial corpus is the follow-up once its Hub
download path is confirmed.

## 5. What is deliberately out of scope

- No OpenHands→ATIF converter in this phase (spec only; it is a versioned
  Zone 03-adjacent transform with its own tests).
- No gated/token-requiring datasets, ever, under the current credential policy.
- No training-data export (Zone 04 concerns, P3).
