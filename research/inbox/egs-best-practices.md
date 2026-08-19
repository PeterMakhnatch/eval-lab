---
source_url: synthesis of arXiv 2607.27929 (Meta-Task), 2607.06233 (TOFFEE), SWE-smith, llm-as-a-verifier, AlphaEvolve blog
source_type: paper
retrieved: 2026-08-19
license_note: paraphrase-only synthesis, claims cited to sources
status: raw
feeds: [library/curated/standards/egs-practices.md]
---

# Execution-grounded synthesis — the recurring practices

Cross-source synthesis: the practices that appear in EVERY working
execution-grounded pipeline, stated as rules. Deposited by the operator
session; STANDARDS distills into the corpus with per-claim citations.

1. **No unexecuted artifact leaves the factory.** Generators run inside
   the sandbox and must execute their own solution and tests before
   emitting (Meta-Task's self-validation; AlphaEvolve's evaluator-in-loop).
   Text that was never run is not synthesis, it is fiction.
2. **Skeleton + exemplar, never blank page.** Constrain generation to
   structured content-filling with one verified exemplar in context
   (Meta-Task >85% package success). Blank-page generation is where slop
   comes from.
3. **Ground in real assets or invert from known answers.** Start from
   real repos/commits/data and derive the task backwards (TOFFEE
   inversion; SWE-smith from real repositories). Reality supplies the
   hard-to-fake texture and the answer key.
4. **Multi-stage filtering, execution first; design for rejection.**
   Execution gate → consistency/leakage checks → trajectory-level judge →
   (ours: battery + human registry). Meta-Task keeps ~23% (3,221 of
   14,040 sampled trajectories). A pipeline that keeps most of its output
   is not filtering; log the funnel counts at every stage.
5. **Diversity is controlled, not wished for.** Explicit orthogonal
   dimensions, sampled or coverage-driven (Meta-Task's 39×10×4; our
   craft-gap seeding). "Generate diverse tasks" without axes produces
   mode collapse around the generator's favorites.
6. **Curate trajectories by HOW, not only whether.** Trajectory-level
   filters remove shortcutting, fabrication, unproductive wandering even
   when the outcome verifies (Meta-Task F.3 KEEP/DISCARD; our behavior
   features). Outcome-only filtering trains shortcuts in.
7. **Exploit verification asymmetry: generate k, verify-select.**
   Best-of-N with a calibrated verifier lifts quality toward the oracle
   ceiling (llm-as-a-verifier: TB2.1 78.7→88.0 at Bo5, oracle 96.6).
   Cheap generation × trustworthy selection beats expensive one-shot.
8. **Contamination discipline (the gap in every published pipeline).**
   Public-asset grounding means recall can masquerade as capability;
   carry contamination fields and keep a private held-out set. None of
   the published pipelines do this; it is this lab's addition.
