---
source_type: internal
type: adversarial-review
program: trajectory-to-training
reviewer: wT:p4 (zai/glm-5.3) — M8 standing adversarial exact-head reviewer
date: 2026-09-04
base: charter 6df601b1 · spine 99d471d1
execution: none (read-only)
---

# Adversarial review: M4, M5, M6 heads (2026-09-04)

## M4 — feat/trajectory-training-eval-freeze @ bea52f44 — VERDICT: PASS (one recorded condition)

Artifact: `research/inbox/held-out-freeze-design-20260904.md` (241 lines, only change on head besides the attribution fix).

Charter conformance:
- Freeze-before-outcome chronology (F0→F2) correct; "no outcome, trial ID, reward, metric value, or result-derived label is a freeze input" — enforced by invariant list and refusal table. ✓
- Scientific freeze honestly refused: cluster-disjointness unproven by census; only local-control sentinels frozen, `submission_permitted=false`, `claim permission: false`. ✓ (matches charter firewall: sealed eval frozen before outcomes, cluster-disjoint)
- Reuse-not-duplicate: composes exact pairs into existing `SftSignalFreezeV1`; explicitly rejects a second manifest family; refusal codes extend `SftSignalRefusalCode`/`SftExclusionCode`, no Eval Runner vocabulary. ✓
- Plan-only: no code, no execution, no billable paths on the head. ✓
- Sentinel digests (`eeee…`, `ffff…`, `1111…` checkpoints) are structurally obvious non-scientific; doc marks the fixture "structurally claim-ineligible." ✓

Condition (non-blocking at design stage, blocking before any F2 freeze counts):
1. The canonical digests claimed in the doc (pair-contract `37db3a34…`, freeze `f44f2652…`, reproducibility witness `83903a33…`) were computed by an **uncommitted** digest calculator — the head adds only markdown. The calculator must land as committed code with a test regenerating these exact digests before any freeze artifact derived from this design is admissible. Until then the digest claims are prose, not evidence.

Non-blocking notes:
- Decision rule "minimum eligible pairs: 2" with exactly 2 control pairs voids the control on any single pair failure — acceptable for a composition proof; do not reuse the rule shape for the scientific set.
- `SftStoppingRuleV1`/`SftHardwareClassV1` remain F4-owned placeholders; correctly deferred to Researcher–Evals.

## M5 — research/tt-pilot-design @ 6d8da521 — VERDICT: PASS (clean at design stage)

Artifact: `research/inbox/conflicting-source-pilot-design-20260904.md` (314 lines).

Charter conformance:
- Training-only, fixed, executable family; `allowed_use=training_candidate_only`, permanently ineligible for evaluation registration — matches "training candidates never graduate into the evaluation pool." ✓
- Design-only: no candidates generated, no model calls, no Harbor, no registry writes, no trainer action. ✓
- Topology firewall correctly owned externally: generator receives only collision decision + inventory digest, never sealed contents. ✓
- Single-delta contract is honest about the subtle point: hidden expected truth changes as a *derived consequence* of the authority placement; the design demands a derived-difference proof instead of a naive byte-identical claim, and requires the materialized agent-visible diff (after authority-map sentinel) to be exactly `{authority-map.json}`. ✓
- `shared_seed` derivation excludes `candidate_id`, arm, and `authoritative_source_index` — arms cannot differ beyond the authority binding. ✓
- Action budget arithmetic consistent: `source_count + 2` = 1 discovery + source_count reads + 1 submission; verifier checks 4–5 force full discovery/read sequence, so budget cannot be gamed by skipping reads. ✓
- Controls are well-shaped: oracle 3/3 per arm (6/6 across twins), NOP, and a *plausible* wrong-source mutant (complete, internally consistent, real source) — rejection proves authority selection, not formatting. ✓
- Leak scan distinguishes legitimate runtime observations (`read_source` values, authority roles) from forbidden disclosure; verifier-only mount outside agent image. ✓
- Refusal matrix is fail-closed with typed codes and staging deletion on partial failure. ✓

Non-blocking observations:
- `topology_id` granularity (entity_count, source_count, conflict_axis, tool schema, budget) may collide across *independently authored* families sharing coarse structure — the firewall gate would over-refuse (conservative false positive), never leak. Acceptable; note for the future separate evaluation family.
- All digest-bearing receipts are downstream of an implementation that does not exist yet; this PASS certifies the design contract only.

## M6 — research/tt-s0-validation — VERDICT: NOTHING TO REVIEW

Branch head equals the charter merge `6df601b1` (no commits beyond base; not pushed). No artifact delivered. Re-review when wH:p1 posts a head.

## M3 (not mine unless vacated)

PR #370 @1dd17b19 adversarial leg is with wS:p5 per the M8 registration; per lease terms I take it only on wS:p5 pass or silence.

---

# Deep adversarial pass: M5 PR #372 @ 6d8da521 (M8 first grant, 2026-09-04)

Attack angles per grant. Verdict: **CLEAN** — PASS reaffirmed, three non-blocking observations.

## 1. Single-delta twin claim — holds, and is code-anchored
- Semantic level is enforced in code at the exact head: `src/evallab/curriculum_candidates.py:416-423` refuses any candidate pair whose field-difference set ≠ `{one_variable_delta}`; spec bounds validated at `:149-155` (`authoritative_source_index` in `[0, source_count)`); twin identity digest binds the full spec at `:501`.
- Materialized level (sentinel authority-map manifest comparison) is design-only, correctly declared as future implementation with its own refusal (`single_delta_failed`).
- Stronger than the doc claims: because `shared_seed` excludes `candidate_id`, arm, and `authoritative_source_index`, every agent-visible byte except `authority-map.json` is arm-identical **by construction** — no side channel about the authoritative index exists anywhere else in the environment (distractors included, same seed). The derived-consequence framing for hidden `truth.json` differences is correct, and the pair-integrity receipt demands the proof rather than asserting it.

## 2. Reset/hash-chain replay — sufficient under the declared threat model
- Event chain binds previous-event digest + pre/post state digests; reset root derives from candidate+runtime implementation digests; verifier check 1 pins runtime/verifier/scenario digests to the frozen plan. An agent cannot forge, reorder, or truncate without `malformed_event_chain` / `identity_mismatch`.
- Observation (non-blocking): an *adversarial runtime* could under-report events — but the runtime is frozen by digest in the plan and the threat model is agent misbehavior, not implementation compromise; the implementation lease should keep runtime code under the same review gate as the verifier.

## 3. Hidden exact-binding verifier leak surface — clean
- `truth.json` + `validation-plan.json` live only in the verifier tree, excluded from the agent image and tool outputs; leak-scan rules cover instruction/verifier/truth/credential disclosure and explicitly whitelist legitimate runtime observations (source values, authority roles) — specified, not hand-waved.
- The explicit authority in `discover_sources` is the *design*, not a leak: the pilot isolates correct source selection; the wrong-source mutant (complete, consistent, real source) proves the verifier checks authority, not formatting.

## 4. Control expectations — sound; one wording defect
- Oracle 6/6, NOP 2/2 (`missing_submission`), wrong-source 2/2 (`wrong_source`) are deterministic, receipt-bound, and the mutant is genuinely plausible. 3 fresh resets per arm prove determinism (the stated purpose), not robustness — correctly not claimed.
- **Observation (fix before implementation): "the lowest-address source whose authority role is reference" is ambiguous** — addresses are "opaque stable handles" with no defined ordering. Must read "first `reference` source in the `discover_sources()` catalog order." Wording only; refusals and receipts unaffected.

## 5. Receipt materialization timing — honest
- All receipts are post-execution and quarantine-preserving (`training_eligible=false` stays); atomic staging with deletion on any refusal. The firewall receipt depends on an externally supplied frozen-evaluation inventory digest that does not exist yet — declared as unresolved dependency #7, with the collision gate as the sole interface. No receipt certifies itself; identities must reopen through existing artifact authority.

## 6. Quarantine-first lifecycle vs M1 F5 — honest, one cross-reference owed
- The gap is acknowledged: risk #1 ("must not be mistaken for a candidate family"), risk #2 (descriptor authority intentionally weak; certification boundary owned by Architect/Tasks), lifecycle gates the training-only pool behind a separate certification decision. `training_eligible: Literal[False]` at `curriculum_candidates.py:257`/`:373` structurally prevents silent promotion — matches the M1 audit §3d note. Held-out/calibration parents refused upstream at `:596`/`:599`; split literals at `:214-215`.
- **Observation (non-blocking): the receipts table is effectively the design sketch for F5's `ReplayReceiptV1` and certified-pool state but never names F5.** F5's owner is also wH:pE, so this is a proposal, not a parallel taxonomy — but the implementation mission should state these receipts *are* the F5 carriers (or extend them), so two receipt vocabularies don't drift apart the way M4 was specifically praised for avoiding.

## Defect-or-clean summary
CLEAN at `6d8da521`. No charter violations, no leak paths, no firewall gaps, no dishonest gaps. Three implementation-stage observations: (a) lowest-address ordering wording; (b) F5 cross-reference owed; (c) keep runtime code under the implementation review gate.

---

# M8 re-review: M4 implementation @ 48b686f9 (PR #371, 2026-09-04)

Leased paths judged: `research/inbox/held-out-freeze-design-20260904.md` (+52), `src/evallab/trajectory_training_eval.py` (265), `tests/test_trajectory_training_eval.py` (263). M5 pilot file deletion in the raw PR diff is base-alignment to 99d471d1 — confirmed present on spine.

Verdict: **CLEAN** — and the M8 binding condition is **DISCHARGED**.

## Binding condition (digest witnesses)
`test_design_digest_witnesses_regenerate_exactly` rebuilds `37db3a34…` (pair-contract), `f44f2652…` (freeze), and `83903a33…` (output) from typed fixture identities — not pinned constants. Verified by execution in a detached worktree at the exact head: 5/5 tests pass.

## Probes per grant
- **F2 sentinel recipe a×64:** executed the committed builder with `sha256:a×64`; produced exactly the doc's `pair-6e53b6f2e7852f68d979` / `pair-7d3355173f33ba2f8f55`, pair-set `e2a7b89b…`, projection `c2682bc8…`. Doc claims are true against the code. *Note (minor): the test module asserts the three original witnesses but not these three new projection digests — pin them in `test_design_digest_witnesses_regenerate_exactly` or a sibling test so a future digest-affecting refactor cannot silently diverge from the doc.*
- **Exact membership / stale-copy / extra-outcome / missing-extra-task refusals:** `build_local_eval_pair_projection` requires `task_seeds` to exactly cover the task set (both under- and over-coverage refused, `trajectory_training_eval.py` builder + tests); seed tamper → `pair_id does not match` (pair_id derives from task+seed+recipe, `:100-116`); `pair_digest`/`pair_set_digest`/`projection_digest` all recomputed and enforced (`:82-89`, `:132-141`, `:163-176`); extra `outcomes` field refused by `extra="forbid"` frozen config.
- **Literal gates:** `submission_permitted`, `scientific_claim_permitted`, `outcomes_present` are `Literal[False]` — assignment of `True` is a ValidationError (tested). No boolean can upgrade permission.
- **Checkpoint chain:** roles must match arms, baseline/candidate must share model_revision+model_digest and differ in artifact digest (`:84-97`); equal-checkpoint refusal tested.
- **F3/F4 scientific blockers — typed-absent, not silent:** the module exposes exactly three names (`__all__` verified), all subordinate/local; no scientific constructor exists to call, `scope` is `Literal["local-control-only"]`, and the design's refusal table still assigns the future `ownership_domain_unavailable/mismatch` etc. to `SftSignalRefusalCode` when F3/F4 land. A scientific evaluation set passed to the local builder still yields a claim-ineligible, non-submittable projection — it cannot masquerade as scientific freeze. Structural absence is stronger than a runtime refusal here; acceptable for plan-only scope.

## Verification performed (this review, not author-reported)
- Detached worktree at `48b686f9`: `PYTHONPATH=src uv run pytest -q tests/test_trajectory_training_eval.py` → 5 passed.
- `uv run ruff check` on both leased code paths → clean.
- Independent execution of the builder confirming the doc's a×64 digests. Worktree removed after.

No charter violations. One minor test-hardening note above; nothing blocking.

---

# M8 review: M6 S0 validation @ dc02b1b4 (origin/research/tt-s0-validation, 2026-09-04)

Deliverables judged: `research/inbox/s0-validation-report-20260904.md` (103), `research/tt-fixtures/` (bundle + four-arm staging + `validate_s0.py`, 236). Verdict: **CLEAN — BLOCK correctly declared; PASSes verified.**

## Binding verification performed (independent execution, detached worktree at dc02b1b4)
- `validate_s0.py` run twice: byte-identical results, exit 0. `overall_status: blocked_at_g3_assistant_mask_binding`.
- All three pinned digests reproduce exactly: plan `ee2255b5…`, checkpoint `95d795b5…`, effective config `97edc3c7…`. `assistant_only_loss_in_plan: false`, `trl_imported_by_renderer: false`, `truncation: error`. The PASSes are genuinely deterministic and offline.

## Is the G3 BLOCK complete? — yes, and correctly scoped
- Assistant-only mask binding: `TrainerRenderingContractV1`/`TRLPlanPayloadV1` `assistant_only_loss` are `Literal[False]` (validator proves construction with `True` raises); the blocker is structural, not a missing test. Correct to refuse rather than render an unbound claim.
- Exact token proof: honestly impossible offline; tokenizer/template files are declared offline contract fixtures, not upstream bytes. Drift is impossible to fake inside the fixture — digests are asserted against bundle identity — but upstream equivalence is correctly NOT claimed.

## Missed refusal surfaces? — none found; three probed live at this head
- **Tool-identity loss**: orphaned `tool_call_id` refused by production `_valid_tool_linkage` (executed: ValidationError).
- **Truncation**: `truncated_terminal_span` refusal exists (`training_export.py:1156-1159`, verified) + plan-level `truncation="error"` means the trainer errors rather than silently drops. Token-level overflow remains inside the declared BLOCK — correct.
- **Label/labelprob leakage**: fixture `FORBIDDEN_FIELDS` covers label(s), log_probs/logprobs, reward(s)/verifier_reward, token_id(s)/input_ids, and mask variants, walked recursively over projected records and train payloads; bundle-level scan (`trainer_bundle.py:488-501`) is recursive and casefolded, covering validation/test splits via `TrainerBundleV1.model_validate_json`.
- **Template drift**: digest-bound to bundle identity; upstream drift is part of the token BLOCK, not silently passed.

## S1 staging honesty — yes
All four arms `not_materialized`/`not_rendered`; C/D recorded `unavailable_degenerate_support` (matching M3's zero-strictly-eligible-rows finding); A/B only `eligible_after_all_gates_pass`; `s1_model_target` unfrozen with the five freeze requirements named; dependency heads pinned (M1 `f3d6ee42`, M2 `b81edd5e`, M3 `47653b3e`). The staging records refusals instead of fabricating four manifests — exactly right.

## Notes (non-blocking)
1. Negative surfaces (orphan linkage, incomplete span) are enforced by production code but not exercised as in-fixture negative tests inside `validate_s0.py`; I probed both live and they fire. Adding two negative asserts would pin them against fixture drift.
2. The 48-byte fixture checkpoint is honestly labeled fixture-only; nothing treats it as weights.
