---
source_type: internal
---

# Lead sync reply — Analyst (wK:p7 / Fable 5.1 Analyst pane)

## 1. Current focus

Analyst lane: merged capability-deficit miner (Track B, PR #356 → `02b4c814`, integration `fbee62dc`; `src/evallab/interpretation/capability_deficits.py` + TRACE measures). In flight: first calibration-tier deficit census over the 130 action-memory + 6 tau archives, then a TRACE Cov/ER−/ER+/Delta priority table as Track C's input. No SFT-signal overlap.

## 2. Top blocker

**Waiting on Peter:** two yes/no decisions in my plan — confirm integration head `fbee62dc` (or the smoke-verified head) as the census base, and confirm running the miner over the historical corpus as **calibration-tier artifacts** (descriptive-only, tier-labeled; the merged miner enforces the boundary structurally). Both sent; neither answered yet.

**Waiting on another pane:** a clean τ trial reaching evaluation (Eval Runner, post-PR 351 credential + `configure_run` deletion). Clean τ yield is still 0/6; every governed reading downstream of me is empty until one exists.

## 3. The one unowned thing — a gap between lanes

**`reward_info` is dropped at projection.** Archived `verifier/result.json` carries `reward_basis`, `reward_breakdown`, `db_check`, `action_checks`, `tau2_evaluation`, and `info.note` (verified on all 6 tau archives in CAS). The projected `reward_facts` table carries only `(reward_name, reward_value)`. So every consumer — mine, lessons, any held-out gate — sees a bare scalar and cannot distinguish an environment non-evaluation (5/6 of the corpus) from a model failure.

Nobody owns it: Data's projection PR #350 explicitly excluded it; Platform's PR #351 writes it to `tau3_runtime_state.json` but not to a typed column; my miner *consumes* the fields but takes them as caller-supplied input.

**Contract, file/symbol level:**
- Extend the reward projection in `src/evallab/evidence/facts.py` (`rebuild_from_raw` → `reward_facts` schema at ~:731) or add one `reward_components` table keyed `(job_id, trial_id, component)`, with typed columns: `reward_basis` (list), `reward_breakdown` (map), `db_match` (bool|null — **null by construction for telecom**), `tau2_evaluation` (bool), `termination_note` (str|null), and a derived `evaluation_class ∈ {analyzable, opaque, non_evaluated}` keyed on `tau2_evaluation`, not on breakdown presence.
- Project `reward_basis` **even on non-evaluated trials** — it is task-sourced, not evaluator-sourced, which is exactly why domain attribution works on the trials the evaluator gave nothing for.

**Acceptance bar (behavioral, from the real archives already in CAS):**
- Projecting the 6 tau archives yields `evaluation_class` counts exactly **1 analyzable / 0 opaque / 5 non_evaluated**, with `reward_basis` populated on all 6 (`[DB]`×4, `[DB, COMMUNICATE]`×2).
- HSNXqqs projects `reward_breakdown={"DB":1.0}`, `db_match=true`; eRUiH5K projects `tau2_evaluation=false`, breakdown null, `termination_note` containing `too_many_errors`.
- A negative: a hand-built record with `reward=0`, breakdown absent, `tau2_evaluation` absent must land `opaque`, never `non_evaluated` — the inference the Librarian and I both had to correct.
- Byte-deterministic reprojection; no change to the scalar `reward_value` path.

That closes the gap between Runner → Data → Analyst in one table, and it is the precondition for any of my census to say something about τ at all.
