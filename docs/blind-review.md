---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Repeatable Blind Trace-Review Workflow

The `evallab review` command family provides a repeatable, leak-proof workflow for
evaluating multi-arm agent experiments (e.g. stock baseline vs tuned adapter vs prompt optimization).
It operationalizes the HAR-128 G6 methodology into three deterministic commands.

## Overview

Trace review separates human and model judgments from experimental arm labels to eliminate bias:
1. **`prepare`**: Ingests finished Harbor job trials across arms, strips all identifying tokens,
   asserts prompt identity across arms, and produces blinded packs with a cryptographically sealed arm map.
2. **`freeze`**: Validates independent rater outputs against the canonical rater guide,
   computes SHA256 digests over all labels and metrics, and writes an immutable freeze receipt.
3. **`join`**: Validates the freeze manifest, unseals the arm map, and outputs comparative
   tables (`TABLES.md`), tool prediction scores (`scores.md`), and an explanatory report (`REPORT.md`).

```
┌─────────────────────────┐
│ Multi-Arm Harbor Trials │ (stock, tuned, gepa, etc.)
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│  evallab review prepare │ ──> packs/<id>/, SEALED_arm_map.json (0400),
└────────────┬────────────┘     metrics_blind.jsonl, rater_batches.json, leak_scan.json
             │
             │ (Blind raters label runs without knowing arm identity)
             ▼
┌─────────────────────────┐
│   evallab review freeze │ ──> MANIFEST.sha256, FROZEN_AT
└────────────┬────────────┘
             │
             │ (Sealed freeze verified before reading arm map)
             ▼
┌─────────────────────────┐
│    evallab review join  │ ──> TABLES.md, tables.json, REPORT.md, scores.md
└─────────────────────────┘
```

---

## Step 1: `evallab review prepare`

```bash
uv run evallab review prepare \
  --jobs-glob "runs/HAR-126-ovn-g5-*" \
  --arm-regex "-(?P<arm>stock|tuned|gepa)__" \
  --mask-text-file path/to/addendum.txt \
  --out derived/review
```

### Inputs
- `--job-dir` (repeatable) or `--jobs-glob`: Landed Harbor job directories.
- `--arm-regex`: Regular expression with a named capture group `(?P<arm>...)` matched against trial or job names.
- `--mask-text-file` (repeatable): Text file containing arm-specific prompt text to delete (e.g. an experimental prompt addendum).
- `--mask-regex` (repeatable): Regular expression pattern to strip from prompts and outputs.
- `--out`: Destination review directory.
- `--raters`: Number of independent raters (default: `2`).
- `--per-agent`: Batch size for rater subagents (default: `6`).
- `--seed`: Deterministic random seed for shuffling (default: random from `os.urandom`).

### Outputs
- `packs/<id>/`: Blinded evidence packs containing:
  - `trajectory.json`: Sanitized ATIF steps retaining only `step_id`, `source`, `message`, `reasoning_content`, `tool_calls`, and `observation`.
  - `result_summary.json`: Compact outcome summary (`reward`, `stop_reason`, `n_episodes`, token counts, `loop_break`).
  - `exception.txt`: Sanitized error traceback if present.
  - `verifier/`: Sanitized verifier diffs and logs.
- `SEALED_arm_map.json`: Read-only map (file mode `0400`) binding blind IDs to true arms and trial hashes.
- `metrics_blind.jsonl`: Deterministic per-trial execution metrics.
- `rater_batches.json`: Shuffled batches formatted for subagent dispatch.
- `PACK_FORMAT.md`: Pack schema documentation.
- `leak_scan.json`: Audit log of the token leak scan.

### Hard Acceptance Gates
- **Zero Information Leaks:** Fails hard if any arm name, model ID suffix (`:har\w+`), mask text, or trial/job name survives in any pack file. Handles JSON-escaped strings.
- **Prompt Identity:** Fails hard if the first prompt of the same task differs across arms after masking container hostname UUIDs and 12-hex IDs. Prints unified diffs of differing spans.

---

## Step 2: `evallab review freeze`

```bash
uv run evallab review freeze derived/review --labels derived/review/labels
```

### Inputs
- `dir`: Review directory from `prepare`.
- `--labels`: Directory containing rater subdirectories (`labels/rater_a/<id>.json`, `labels/rater_b/<id>.json`).
- `--guide`: Canonical rater guide path (default: `research/explorations/trace-lab/review/RATER_GUIDE.md`).

### Contract & Invariants
- Enforces presence of valid labels for every blinded pack across all raters.
- Validates required fields: `trial`, `stop_reason`, `first_failure`, `blame`, `loop_kind`, and `pass_copied`.
- Computes SHA256 checksums over all label files, metrics, and leak scans into `MANIFEST.sha256`.
- Writes UTC timestamp, guide checksum, and run counts to `FROZEN_AT`.
- Refuses to overwrite an existing freeze.

---

## Step 3: `evallab review join`

```bash
uv run evallab review join derived/review [--predictions derived/review/predictions]
```

### Inputs
- `dir`: Frozen review directory.
- `--baseline`: Reference arm for paired comparisons (default: first sorted arm or `stock`).
- `--predictions`: Directory containing tool/model `.jsonl` predictions for validation.

### Outputs
- `TABLES.md` & `tables.json`: Per-arm medians/IQRs, paired deltas against baseline, flip counts, and differing tasks. Excluded/infra cells are recorded as missing rather than zero.
- `scores.md` & `scores.json`: Out-of-sample tool vs rater agreement with 95% Wilson confidence intervals.
- `REPORT.md`: Comprehensive narrative synthesizing counts verdicts, blind rater genuine-pass judgments, differing task breakdowns, evidence quotes, and freeze provenance.

### Known Quirks & Withdrawn Metrics
Detectors flagged in `research/explorations/trace-lab/QUIRKS.md` as broken are tracked in the module constant `WITHDRAWN_METRICS`. When populated, `TABLES.md` displays a prominent audit warning banner.
