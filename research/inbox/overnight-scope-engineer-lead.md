---
source_type: internal
---

# Overnight scope — Engineer Lead (this side)

## Current owned scope (no overlap with A–E)
- Z.ai queue execution lane (wired, reconciled with spine, 382 green on integrate/spine-batch1)
- Spine Batch-1 integration branch (failover tip + pairing fix, conflicts resolved, verification done)
- Feature quarantine mechanics (27 dead columns, committed to base)
- T1.1 discrimination gate first run (213 verdicts, committed)
- approve-all-waiting CLI (committed); 17-cell screening canary specs (schema-valid, unsubmitted)
- Night-shift workers out: DropVerif (batch-2 worktree clearing), MeteredSpecs (metered k1 specs → proposed), RegDrafts (registry drafts), LineageDecl (lineage metadata)

## Claim: complementary slice — external trainer-result manifest + frozen held-out eval read path
Rationale: Track D covers the outbound trainer bundle; nothing in A–E covers the inbound leg (external trainer result manifest → frozen held-out Harbor evaluation). This is the tail of the pipeline diagram and is unowned. It is also the piece my side is uniquely positioned to define (queue + execution + provenance ownership).

Scope: backend-neutral result-manifest contract (model/checkpoint identity, dataset/split digests, training config digest, reported metrics with uncertainty, artifact digests), validator (digest binding, split-integrity, held-out non-contamination evidence), and a frozen held-out evaluation protocol (how a trained checkpoint re-enters Harbor eval without touching train data or the trainer). Fixture-based tests, no model/network/trainer invocation. Isolated worktree, named branch, PR unmerged per program rules.

Evidence due morning: branch, head, files/symbols, focused test output, negative controls, residual risk.
