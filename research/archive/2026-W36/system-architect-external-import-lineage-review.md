# System Architect review — external-import lineage contract

Review source branch `feat/external-import-lineage` @ `f33fe0697f45ef0771f0c13622f5bb78cc3dd31a` (base `8e02fba2`) and its provisional re-rooted staging commit `eaf9984c06bd2983b582d3ba491b2bc8ae367290` on `analyst/synth-data-promotion-hardening` after F1–F3. Architecture decision: `/tmp/system-architect-external-import-certification-decision.md`; implementation brief: `research/inbox/platform-external-import-lineage-contract.md`. Review only; do not reset or rewrite the staged branch.

Validate these boundaries end to end:

- `ExternalImportLineageV1` is the single generic optional lineage contract on `CandidateSource` and `TaskRegistryRecord`; native packages remain compatible without ambiguous defaults.
- `ExternalImportTransformationRecordV1` is durable outside the task package and binds source identity/digest, transformation implementation/config/digests, output runtime identity/digest, reproducibility evidence, timestamps/version, and no-op/transformed state.
- Source digest is provenance only. Certification, registry reload/audit, and campaign identity bind final certified runtime bytes, with exact packet→envelope→registry equality and digest reopening.
- No-op imports require source/output byte identity. Transformed imports require different bytes plus transformation evidence; missing/malformed/tampered/unsafe-path/unsupported-version artifacts refuse.
- Workbench packet writer emits successor `m049-v2`; legacy `m049-v1` read support is explicit and cannot bypass v2 lineage requirements.
- Candidate source metadata is bound through promotion; optionality does not allow a declared external source to silently lose lineage.
- There is no fair-alternative waiver, duplicate answer-map authority, LoCoMo certification, registration, or model execution in this generic contract.
- Algorithms/path handling/canonical serialization match repository conventions and resist traversal, transient-file ambiguity, or digest-domain confusion.
- The provisional `eaf9984c` conflict resolution is semantically equivalent to the reviewed source implementation, uses the staged F3 `task_directory_digest` authority, and introduces no lost validation or accidental drift.

The commit formats all five touched files and has a large textual diff. Separate semantic review from formatting churn; identify any accidental behavior change or unnecessary restyling. Confirm all constructors/callers are migrated despite Python LSP being unavailable, and list rebase conflicts expected with digest F3 (`task_directory_digest`) and canonical `46794c16`. Run focused registry/workbench tests, adversarial probes, and touched-file Ruff only. Return APPROVE/BLOCK with exact evidence and integration instructions; no merge, main sync, or model call.
