Status: review-wanted
Last: PR #446 green at head 740c52d6 (record bridge + consumer check + token accounting)
Next: merge review; then split-manifest intake for training_allowed=null tasks
Blockers: none

# HAR-65 — SFT record bridge and GLM consumer proof

## Scope

Faithful real-trajectory export for the SFT consumer (MISSION 2026-09-18,
Harness role). Record boundary, dispositions, dedupe, lineage gating,
privacy quarantine, and the tokenizer/mask consumer proof for
GLM-5.3-Flash. Not owned: trainer, template choice, serving (HAR-64/66).

## What exists now

- `src/evallab/sft_records.py` — pinned contract `evallab.sft_records/1`;
  model-neutral messages (`observation` role with `presented_as`), structured
  tool calls with `arguments_encoding`, causal decision examples, per-trial
  dispositions with reasons, session-identity dedupe preferring unredacted
  copies, registry + TB3/TB4 lineage rejects, redaction/secret quarantines,
  deterministic manifest + `contract.json`.
- `scripts/sft_consumer_check.py` — renders records through the real
  GLM-5.3-Flash tokenizer/chat template (HF snapshot `eb9eb208`, offline) and
  audits the assistant-only mask; `--account` emits per-record token counts.
- `tests/test_sft_records.py` — 14 behavioral tests incl. explicitly labeled
  mini protocol fixtures (both wire formats). Not model evidence.

## Measured results (real corpus, untouched originals)

- Roots: `research/evidence/runs` + `zai-opencode-experiments` runtime
  worktree. 380 trials -> 155 accepted (1618 decision examples, all GLM
  opencode), 9 quarantined (R1 redaction), 61 rejected, 155 session
  duplicates.
- Consumer: 40/40 mask audits pass; supervised span starts at the
  template-emitted `</think>`; EOS outside span.
- Token fit: median 3010 / p95 40330 / max 45362 rendered tokens vs 1M
  window — whole-trajectory SFT feasible; no forced truncation.
- Artifacts: `runs/sft-records/real-20260918/` (gitignored; manifest,
  receipts, token accounting, full-suite logs).

## Known limits / next

- `training_allowed: null` (synthetic tasks not in registry) need the
  Experiments split manifest; exporter keeps `split.assignment: unassigned`.
- `presented_as` stays `unknown` for opencode ATIF (recorded limit); raw
  `mini-swe-agent.trajectory.json` settles it — proven by fixture, no
  retained mini trial exists yet.
- Subagent trajectory refs unhandled (no such trials in corpus).
- Upstream `export_traces` fidelity losses documented in-module; data-lane
  HF script unchanged.

## Return path

Linear HAR-65 receipt; HAR-64 handoff comments posted. No peer pages, no
trainer work, no shared-file edits.
