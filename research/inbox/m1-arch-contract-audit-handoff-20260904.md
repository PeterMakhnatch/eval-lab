---
source_type: internal
---

# M1 ARCH-CONTRACT-AUDIT — Wave 0 deliverable (wS:p9/Fable, 2026-09-04)

- **Branch:** `research/tt-arch-contract-audit` @ `7090c2e4` (parent `6df601b1`, spine as granted)
- **Deliverable:** `research/inbox/architect-contract-audit-20260904.md` on that branch (14.1KB; state diagram, exact type/field map, coverage table, boundary gaps, second-convention risk table)
- **Verification:** read-only; every PRESENT/MISSING call re-verified in source at `6df601b1` (module:line cited in the doc); no code change; no second manifest family

## Headlines for review

1. **Chain exists as encoded.** Charter's `QuarantinedCurriculumCandidate` is really `SyntheticTaskCandidate(status="quarantined")` (`curriculum_candidates.py:252-255`) — charter should use the real name.
2. **Firewalls typed.** Dual-axis training admission (`registry_allowed_use="training"` + affirmative evidence class, `training_export.py:389, :399-405`), `NonContaminationEvidence` cluster/split refusals (`training_result.py:304-318`), `FrozenHeldOutEvaluationPlan.submission_permitted=False`, `ready_for_rl=Literal[False]`.
3. **Six proven-missing fields**, each one field/small record on EXISTING types — no new manifest family:
   - **F1** producer_model / producer_harness / source_dataset_revision / source_license on the source binding trio (blocks provenance strata balancing);
   - **F2** `SelectionRecipeV1` (arm A–D, block keys, supervised-token budget) referenced by the manifest — carries the G1 comparability refusals;
   - **F3** `ownership_domain` extending `TrainingSplit` (training-discovery / curation-development / sealed-test);
   - **F4** freeze completeness: `stopping_rule`, preregistered exclusions beyond capture-incomplete, `hardware_class` for S1;
   - **F5** certified training-only pool state + `ReplayReceiptV1` for the environment pilot;
   - **F6** `discovery_evidence_epoch` on `CapabilityDeficitArtifact` for the closed-loop stale-deficit refusal.
4. **Six second-convention risks** tabulated with rulings (dual source-binding types, cluster key vs digest, backend identity pair, three arm enums, capture taxonomies) — none may grow.
5. Coverage table maps every charter field PRESENT/PARTIAL/MISSING with `module:line` at `6df601b1`.

Feeds M2: F1 is the census field skeleton. Merged audit-file PR goes to integration per your merge sequencing; no smoke needed (docs-only file on a leased branch).
