---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Context Pack Compiler (WS-B)

Status: living. Owner: Platform lane. Date: 2026-08-16. Implements
`docs/build-plan.md` WS-B.

## Purpose

The context pack compiler implements **context engineering as code**. Instead
of agents performing unbounded crawls over `docs/` — ingesting historical
reviews, obsolete incident notes, and out-of-scope mission specs — each agent
receives a compiled, deterministic context bundle tailored to its mission type.

This solves context pollution structurally:
1. **Living docs only**: Historical records (`docs/archive/`, dated reviews,
   retired proposals) are filtered out at compile time.
2. **Audience-targeted**: Builder agents receive authoring and workbench
   standards; analyst agents receive trajectory science and statistical guides;
   runners receive execution and quota rules; operators receive infrastructure
   and fleet manuals.
3. **Task-corpus facets**: When authoring or evaluating against a target task
   (`--task <ref>`), the compiler queries `derived/parquet/craft/craft.parquet`
   and attaches structural verifier patterns, anti-cheat techniques, and
   environment reproducibility requirements.
4. **Deterministic**: Compiling the same repository state always produces
   byte-for-byte identical output and identical content SHA-256 hashes.

---

## Doc Front-Matter Standard

All living documentation under `docs/` declares machine-readable front-matter:

```yaml
---
status: living | historical
audience:
  - builder
  - analyst
  - runner
  - operator
---
```

### Fields

| Field | Type | Allowed Values | Description |
|---|---|---|---|
| `status` | `enum` | `living`, `historical` | `living` docs describe current system contracts; `historical` docs record past reviews or milestones. |
| `audience` | `list[enum]` | `builder`, `analyst`, `runner`, `operator` | Mission roles that require this document in their context bundle. |

### Audience Taxonomy

- **`builder`**: Task authoring, task-quality workbench, task registry, rubric
  design, benchmark synthesis, and CRAFT task-corpus patterns.
- **`analyst`**: Evidence extraction, ATIF trajectory analysis, failure mode
  classification, judge calibration, and statistical reporting.
- **`runner`**: Trial execution, queue management, agent profiles, subscription
  authentication, and preflight quota checks.
- **`operator`**: Lab infrastructure, PostgreSQL maintenance, daily digests,
  health checks (`doctor`), and multi-agent coordination.

---

## CLI Interface

The compiler is callable directly via Python module execution:

```bash
# Build a context pack for a builder mission
uv run python -m evallab.contextpack build builder -o /tmp/builder_pack.md

# Build a pack tailored to a specific target task
uv run python -m evallab.contextpack build builder --task terminal-bench/atrx-vep-crispr -o /tmp/task_pack.md

# Emit JSON metadata and content digest
uv run python -m evallab.contextpack build analyst --json

# List all docs with their status and audience tags
uv run python -m evallab.contextpack list-docs
```

### Arguments

| Argument | Description |
|---|---|
| `mission_type` | Positional argument: `builder`, `analyst`, `runner`, or `operator`. |
| `--task <ref>` | Optional task reference from `craft.parquet` (e.g. `terminal-bench/atrx-vep-crispr`). |
| `-o`, `--out <file>` | Path to write the compiled markdown document. |
| `--docs-dir <dir>` | Path to docs directory (defaults to `docs/`). |
| `--parquet <file>` | Path to `craft.parquet` (defaults to `derived/parquet/craft/craft.parquet`). |
| `--json` | Emit structured JSON metadata including document list and content hash. |

---

## Pack Structure

Every compiled context pack contains:

1. **Header Marker**:
   ```markdown
   <!-- generated-by: contextpack v1 -->
   <!-- mission-type: builder -->
   <!-- target-task: terminal-bench/atrx-vep-crispr -->
   <!-- doc-count: 10 -->
   <!-- content-sha256: sha256:ac34d20c5cf97cba2c5a27c4c16a3b09b89454c7b2708d1ad5047325ac443689 -->
   ```
2. **Table of Contents**: Linked index of included living documentation files.
3. **Compiled Documentation**: Full prose of living documents matching the
   audience, sorted alphabetically by path.
4. **Task Design Facets & CRAFT Patterns** (when `--task` is supplied):
   - Structural verifier classification (`pytest`, `golden_file`, `hybrid`).
   - Anti-cheat isolation techniques (`hidden_tests`, `answer_outside_image`).
   - Environment and runtime details (container services, dependency pinning,
     base image references).
   - Tailored eval design recommendations.
5. **Mission Brief & Execution Guide**: Step-by-step role-specific instructions
   and acceptance criteria.

---

## Acceptance & Determinism

1. **Idempotence**: Consecutive builds over unchanged repository state emit
   identical content hashes.
2. **Zero Text Tampering**: Document bodies are included verbatim; front-matter
   is parsed and stripped from the assembled document body.
3. **Fail-Closed**: Unclassified or malformed docs without `status: living` are
   excluded from agent bundles.
