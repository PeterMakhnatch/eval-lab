# ID-OQ-1: why the fixed Task QA gate misses keyed DEV defects

Population: Sonnet 5.5 "B" arm of OQ-6 v4 on the 44 keyed DEV tasks / 118 admitted keys (110 missed under the production
"new verified" gate). Single-turn audit, no tools, so "never opened" means not in the view or never cited.

Pipeline (no paid calls; labelling and judging by `gpt-6.1-terra`/`gpt-6.1-sol` on subscription):

1. `build_bundles.py` — per-task bundle: keys, all findings with confidence, citation verification, eligibility, retained judge row.
2. Two independent labelers (terra, then sol) assign one of eight causes per missed key (agreement 103/110, kappa 0.886).
3. `enrich_keys.py` — the dominant cause was boilerplate scorer-key text on 73 MiMo `block`/`line` keys (one fixed
   "Archived instruction-adjudicated wrong-patch witness" sentence), which a "same specific contradiction" judge can never
   match. The answer key's own adjudicated evidence and instruction line refs replace it, scorer side only.
4. `build_judge_prompts.py` + `rejudge_metrics.py` — judge every finding under original (O) and enriched (E) keys, report recall
   at confidence floors 0.5/0.4/0.3/0.2, split by `sha256(task_id)` parity.

Recall is key-backed task-defect matching by a model judge, not an executed grader differential. The cross-test (OQ-10)
is not read or rerun. Results and the full receipts: bridge `opus-vs-swarm-qa.md`, section ID-OQ-1.
