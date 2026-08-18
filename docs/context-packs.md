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
4. **Token budget & truncation priority**: Enforces a documented token budget
   (default 12,000 tokens per v2 §6) with deterministic, priority-ordered
   truncation that preserves mission briefs and task facets while shedding
   expendable documentation.
5. **Deterministic**: Compiling the same repository state always produces
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
| `--budget`, `--token-budget <tokens>` | Token budget ceiling (default `12000` per v2 §6; `0` for unlimited). |
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

## Token Budget & Truncation Priority (v2 §6)

To prevent context pollution and avoid silently exceeding LLM context windows,
the compiler enforces a **12,000 token budget** by default (configurable via
`--budget <tokens>` or Python API `token_budget`).

### Token Counting Method

Token counting uses a documented, tokenizer-free character approximation:

$$\text{Tokens} = \left\lceil \frac{\text{len}(\text{text})}{4} \right\rceil$$

- **Ratio**: 4 characters per token ($4:1$), matching standard English prose
  and code token density heuristics.
- **Formula**: `(len(text) + 3) // 4` with empty string returning `0`.
- **Zero Dependencies**: Computed deterministically without external
  tokenizer packages.

### Truncation Priority Hierarchy

When a compiled pack exceeds the token budget, sections are shed in a declared,
deterministic order from **most expendable to most essential**:

1. **Tier 1 (Inviolable — NEVER Shed)**:
   - **Mission Brief & Execution Guide**: Objective, standard workflows,
     handoff contracts, and task references. The brief is the core reason the
     pack exists.
   - **Header Marker & Truncation Notice**: Metadata and audit trail.
2. **Tier 2 (High Priority — Retained Before General Docs)**:
   - **Task Design Facets & CRAFT Patterns**: When `--task` is supplied, task
     facets and verifier patterns are preserved before general living docs.
   - Under extreme budget pressure where all docs are already shed:
     1. Reproducibility / Environment Pinning pattern is shed first.
     2. Anti-Cheat / Isolation pattern is shed second.
     3. Verifier pattern is shed third.
     4. Task Facets summary table is shed fourth.
3. **Tier 3 (Living Documentation — Shed First by Priority Tier)**:
   - **Category 0 (Lowest Priority / Most Expendable)**: Supplemental research
     notes (`docs/research/*.md`) and broad catalog maps (`docs/INDEX.md`,
     `docs/repo-map.md`).
   - **Category 1 (Medium-Low Priority)**: Cross-cutting architectural documents
     with 3 or 4 audiences (`len(audience) >= 3`, e.g. `docs/engineering.md`,
     `docs/architecture.md`, `docs/platform-architecture.md`).
   - **Category 2 (Medium-High Priority)**: Dual-audience technical
     specifications (`len(audience) == 2`, e.g. `docs/craft.md`,
     `docs/contracts.md`, `docs/authoring.md`).
   - **Category 3 (Highest Doc Priority / Retained Longest)**: Single-audience
     core mission workbenches and operating manuals (`len(audience) == 1`, e.g.
     `docs/task-workbench.md` for builder, `docs/analysis-loop.md` for analyst,
     `docs/operating-manual.md` for operator).

**Tie-Breaking**: Within the same category, documents are dropped by audience
count descending, then document size descending (shedding larger documents first
to minimize total documents lost), with alphabetical path as the deterministic
tie-breaker.

### Truncation Audit Notice

A truncated pack explicitly records its truncation in the markdown output and
the header comments so downstream agents and evaluators are aware of omitted
content:

```markdown
### ⚠️ Context Pack Truncation Notice

This context pack exceeded the configured token budget of 12,000 tokens (~48,000 chars)
and was truncated per v2 §6 priority policy (most-expendable content shed first;
mission brief and instructions protected).

- **Configured Token Budget**: 12,000 tokens (~48,000 chars)
- **Estimated Untruncated Size**: ~70,185 tokens (~280,740 chars)
- **Tokens Shed**: ~58,307 tokens across 23 dropped section(s)/doc(s)
- **Retained Living Docs**: 5
- **Dropped Items (in order shed)**:
  - `docs/INDEX.md` (~3,369 tokens shed)
  - `docs/repo-map.md` (~5,236 tokens shed)
  ...
```

Header comments include:
```markdown
<!-- truncated: true -->
<!-- token-budget: 12000 -->
<!-- tokens-shed: 58307 -->
```

When a pack is under budget, output is completely untruncated, no notice is
inserted, and the bytes are identical to unbudgeted compilation.

---

## Acceptance & Determinism

1. **Idempotence**: Consecutive builds over unchanged repository state emit
   identical content hashes.
2. **Zero Text Tampering**: Document bodies are included verbatim; front-matter
   is parsed and stripped from the assembled document body.
3. **Fail-Closed**: Unclassified or malformed docs without `status: living` are
   excluded from agent bundles.
