---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Artifact Lineage Walker (E14)

The evaluation laboratory generates a multi-stage graph of artifacts — raw execution
evidence, Parquet projections, model-assisted trial analysis sidecars, statistical lessons,
evaluation cards, and newly authored benchmark tasks. The lineage subsystem resolves the
provenance of any generated artifact back to Zone 1 (immutable evidence), validating recorded
content digests at every hop.

Entry point: `evallab lineage <path|id> [--json] [--derived-root <dir>]`

## 1. The `inputs` Contract

Every derived artifact declares its direct upstream dependencies via an `inputs` list.
Each item in `inputs` specifies:

- `path` or `id`: Repository-relative file path or entity identifier.
- `digest`: Expected content digest in `sha256:<64-hex>` format.

### Markdown Front-Matter (Zone 4)

Knowledge documents (evaluation cards, findings, research syntheses) embed `inputs` in YAML front-matter:

```yaml
---
status: living
audience:
  - builder
  - analyst
inputs:
  - path: derived/analyses/01JXYZ.../analysis.json
    digest: sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945
---
```

### JSON / Parquet Sidecars (Zone 3)

Structured analysis sidecars and Parquet datasets carry `inputs` in their top-level JSON or companion metadata:

```json
{
  "analysis_id": "01JXYZ...",
  "inputs": [
    {
      "path": "runs/job_alpha/trial_001/result.json",
      "digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ]
}
```

## 2. Terminal Zones and Recursion Base Cases

Provenance zones define evidential authority:

- **Zone 1 (`z1` — Immutable Evidence):** Raw Harbor runs (`runs/<job>/...`, `research/evidence/runs/`), benchmark snapshots (`library/benchmarks/_trajectories/`), and raw artifacts. Zone 1 is the **terminal base case**: the lineage walker stops at Z1 and does not walk past it.
- **Zone 2 (`z2` — Catalog):** Relational PostgreSQL database entities and schema views.
- **Zone 3 (`z3` — Analytics):** Rebuildable Parquet analytics, analysis sidecars, and projections.
- **Zone 4 (`z4` — Knowledge):** Human and generated markdown documents, findings, and evaluation cards.
- **Zone 5 (`z5` — Coordination):** Board files, mission briefs, role contracts, and handoffs.

Every zone outside Z1 is derived and must name its inputs.

## 3. Unrecorded Derivations and Honesty

When an existing artifact on disk does not carry an explicit `inputs` field, the lineage walker reports it as `unrecorded` with a precise reason (such as `"no inputs field declared in front-matter"` or `"no inputs field in JSON artifact"`).

The lineage walker **never guesses edges** from filename similarity, timestamp proximity, or directory co-location. In an evaluation laboratory, an invented lineage edge is worse than a missing one, because an invented edge will be trusted in automated synthesis and calibration.

## 4. Digest Verification and Cycle Detection

- **Digest Verification:** Every hop validates the actual SHA-256 content digest of the referenced file against the recorded `digest`. If the file has been edited, tampered with, or corrupted, the hop is reported as `digest_mismatch`, failing resolution.
- **Cycle Detection:** If an artifact references itself or any of its ancestors in the current traversal path, traversal terminates immediately with status `cycle`, preventing infinite loops.
- **Bounded Depth:** Traversal depth is bounded (default 32) to prevent runaway recursion on pathological graphs.

## 5. Machine-Readable JSON Shape

Passing `--json` outputs a deterministic, byte-identical JSON tree:

```json
{
  "target": "docs/finding.md",
  "path": "docs/finding.md",
  "zone": "z4",
  "digest": "sha256:a1b2c3...",
  "status": "resolved",
  "resolved": true,
  "reason": null,
  "inputs": [
    {
      "target": "derived/analyses/01JXYZ/analysis.json",
      "path": "derived/analyses/01JXYZ/analysis.json",
      "zone": "z3",
      "digest": "sha256:d4e5f6...",
      "status": "resolved",
      "resolved": true,
      "reason": null,
      "inputs": [
        {
          "target": "runs/job_alpha/trial_001/result.json",
          "path": "runs/job_alpha/trial_001/result.json",
          "zone": "z1",
          "digest": "sha256:789abc...",
          "status": "terminal",
          "resolved": true,
          "reason": null,
          "inputs": []
        }
      ]
    }
  ]
}
```

## 6. Worked Example: Tracing a Finding to Evidence

Suppose an evaluation report cites an analysis sidecar derived from a Harbor execution run:

```bash
uv run evallab lineage docs/finding.md
```

Output:
```text
docs/finding.md [z4] (sha256:a1b2c3...) [resolved]
└── derived/analyses/01JXYZ/analysis.json [z3] (sha256:d4e5f6...) [resolved]
    └── runs/job_alpha/trial_001/result.json [z1] (sha256:789abc...) [terminal]
```

Exit code is `0` when the entire lineage resolves to valid Zone 1 evidence with matching digests. Any missing file, digest mismatch, unrecorded artifact, or cycle causes `evallab lineage` to exit non-zero (`1`), allowing automated scripts and CI pipelines to detect broken provenance chains.

## 7. Generator Input Declarations, Partition Conventions, and Determinism

All derived artifacts generated by repository tooling embed explicit `inputs` blocks in their front-matter or metadata payloads.

### 7.1 Generator Declarations

- **`docs/INDEX.md` (`evallab.docindex`)**: Inputs are all discovered markdown documentation files under `docs/` and `docs/research/` (excluding `INDEX.md` itself). Each entry carries the document's repository-relative path and its `sha256:` content digest.
- **`docs/repo-map.md` (`evallab.repomap`)**: Inputs are all Python modules under `src/evallab/` discovered and parsed by the AST walker (including `src/evallab/cli.py` for command attribution), each with its `sha256:` content digest.
- **`research/lessons.md` (`evallab.lessons`)**: Inputs are the DuckDB SQL aggregation view file (`sql/lessons.sql`), craft task manifests (`task.toml` files or `craft.parquet`), observation records (`research/observations/**/*.md`), analysis sidecars (`analysis.json`), and trial facts Parquet partitions (`trial_facts.parquet`).

### 7.2 Convention for Glob / Partitioned Inputs

Where an input source spans multiple partition files rather than a single file:

1. **Resolved Member Listing ($\le 100$ files):** When the resolved partition count is small ($\le 100$), every member file is recorded individually with its explicit repository-relative path and `sha256:` content digest, sorted deterministically by path.
2. **Composite Digest for Large Collections ($> 100$ files):** When partition counts exceed 100, the input is recorded using the canonical glob pattern (e.g. `derived/parquet/**/trial_facts.parquet`) paired with a composite SHA-256 digest computed over the sorted concatenation of all member file SHA-256 digests. Tooling never silently records an unexpanded glob string as though it were a single regular file.

### 7.3 Generation Convergence and Sibling Acyclicity

To prevent cyclic digest chasing between generated documentation siblings:

- `docs/repo-map.md` depends solely on Python source code in `src/evallab/` and never reads `docs/INDEX.md`.
- `docs/INDEX.md` depends on all documentation files, including `docs/repo-map.md`, but excludes itself (`docs/INDEX.md`).
- This dependency structure forms a strict Directed Acyclic Graph (DAG):
  $$\text{src/evallab/*.py} \longrightarrow \text{docs/repo-map.md} \longrightarrow \text{docs/INDEX.md}$$
- Executing generator passes in sequence (`repomap generate` followed by `docindex generate`) produces byte-stable artifacts that converge in a single pass without oscillation.
