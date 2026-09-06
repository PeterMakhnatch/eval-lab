---
source_type: internal
---

# Track H composition ruling — acceptance contract (2026-09-03)

Ruling issued to wH:p1 for `/private/tmp/track-h-architect-review.md`; recorded here as the review acceptance contract. Option **A** is selected on all five composition questions.

## The five decisions

1. **A→D boundary.** D imports and read-only validates A's `TrainingDatasetManifestV1` (`training_export.py:505-608`). No parallel narrowed dataset/source/exporter bindings in D. (Track F §6: A owns the export envelope; D consumes.)
2. **Row rendering.** Declared rendering field names are validated inside `TrainingExampleRecord.payload` (A's envelope + typed payload, `training_export.py:440-473`), not required at the JSONL envelope top level. Representation, forbidden-field, and split/digest checks are retained.
3. **Flow.** `A → B → C → E → D → G`. B is independently re-anchored (via `verify_artifact`/`reverify_authority`, `artifact_authority.py:320-489`) before C consumes it. C hands E only twin/pair identities and stays quarantined (`curriculum_candidates.py:218-241`). C never seeds D. Glue never reconstructs B/C/E schemas or authority.
4. **Publication / idempotency.** Whole-bundle staging: A writes an unpublished subdirectory, D reads from it, and ONE atomic directory rename publishes the bundle (receipt contract §2: sibling stage, fsync files+dir, refuse existing destination of any kind, single no-replace rename, parent fsync, inventory relist). Same input digest rehydrates a byte-identical complete bundle; any mismatch refuses. No per-stage or partial publication.
5. **Queue / execution seam.** Strict typed B/C/E/D/result/G inputs on the ordinary `ExperimentSpec` queue path (`queue.py:780-824` submit, `:966-988` human approve; digest-bound `extra_instruction_path`, `schemas/__init__.py:755-768`). No Track-H queue protocol, no direct Harbor call, no execution, no untyped caller assertions.

## Binding condition (i): authority axes

Every H admission requires BOTH, and neither substitutes for the other:

- registry axis: `allowed_use == "training"` from the task registry;
- evidence axis: an affirmative admissibility/evidence class from the `TrialAdmissibilityV1`-verified authority (via `ArtifactAuthority` at `bytes-verified`).

The admissibility `"causal"` literal is the evidence-axis value; H must not read it as training authorization on its own, and must never infer either axis from paths, cohort membership, or mutable metadata. Absence or contradiction of either axis is a typed refusal (Track F §2; wK:p9 ruling).

## Binding condition (ii): TRL-SFT-only H input

At the H boundary, v1 admits only objective `sft` with backend `trl`. `verifier_reward_episode` and the `spade` backend (`trainer_bundle.py:32-35, 96-105`), plus verl/Agent Lightning, are typed REJECT/incompatibility (`SFT_SIGNAL_NOT_ESTABLISHED` for RL) — unconditional, not caller-settable, until a completed SFT result authority is itself a typed `bytes-verified` input (Track F kill gate 1; adoption table).

## Reviewer acceptance checks

1. D contains no second manifest/source binding type for A's data.
2. Rendering validation reads `record.payload`; forbidden-key/representation/split checks present.
3. B re-anchor call precedes C consumption; C output carries pair IDs only; nothing constructs B/C/E types in glue.
4. Exactly one rename publishes; existing destination refused; byte-identical rerun test present; mismatch refusal test present.
5. Only `ExperimentSpec` reaches the queue; no Harbor/subprocess/network imports.
6. Both authority axes checked with typed refusals; no `"causal"`-only admission.
7. `sft`/`trl` only; RL/SPADE/verl/Agent Lightning refused via a field the caller cannot flip.
