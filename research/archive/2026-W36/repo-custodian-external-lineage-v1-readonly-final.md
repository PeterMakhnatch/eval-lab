# Repo Custodian: make historical registered m049-v1 strictly read-only

## Exact target

- Continue as sole writer in `/private/tmp/eval-lab-external-lineage-b1-b4-repair` on `fix/external-lineage-b1-b4-repair`.
- Current head: `817b7068fd444711c05d6d7de21257e8d6782d2c`; base: `721afd5245c92b5b7761a64dc704a9a258ee6279`.
- Narrow BLOCK report: `/tmp/system-architect-external-lineage-narrow-rereview-817b7068.md`.
- Preserve every closed B1-B4 condition and canonical timestamp rule. Do not touch primary checkout, registry data, LoCoMo activation, canary authorization, or model execution.

## Single residual repair

In `register_task`, inspect the stored validated record before applying any caller-supplied certification replacement or constructing any update. If it is already `registered` with `certification.workbench_version == "m049-v1"`, mechanically confine it to read/reopen/audit compatibility:

- verify the existing stored packet/control authority;
- return the exact stored record unchanged only for an idempotent same-actor reopen with no replacement certification;
- refuse a different actor and refuse any supplied replacement certification (including an m049-v2 packet) with a typed certification/state error;
- never rewrite `approved_by`, `approved_at`, `certification`, state, or any other stored field for historical v1;
- leave the registry JSON bytes unchanged on every refused request.

Add direct production-API and parsed-CLI negatives using a valid already-registered historical m049-v1 record with valid controls. A different actor must fail, exact bytes must remain unchanged, and the original actor's read-only reopen/audit must still verify and return unchanged. Add API coverage for attempted replacement certification so the pre-mutation gate cannot be bypassed.

Run the two focused suites, the new narrow tests, touched Ruff/format, and diff-check. Commit one focused follow-up and page exact head/test evidence. Do not integrate.
