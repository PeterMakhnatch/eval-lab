# 002391 public behavioral check

## Question and frozen evidence

Do the existing `real-2` and `real-3` patches satisfy the 2 examples in the public task instruction?
This is a finite check of 2 known alternatives, not a population estimate.
No confidence interval applies to this complete, fixed set.

The sole behavioral source is `instruction.md` in the original DEV package:
`format-code-task-002391`, digest `sha256:d0a399b8633cdf47a6c3d00198b50c889ed5c282c016bb4b29bbdaf2f9d75fa1`.
Its instruction SHA256 is `070adfd6a3bad8de52967db202ed20fa542b9551e77e7bd36608621ba1bb0ef6`.
The check uses pristine application interfaces only to construct valid inputs.
It does not read hidden tests, reference patches, answer keys, or TEST data.
It adds no requirement about descriptions, dates, fix versions, or object identity.

## Inputs and procedure

Use `Auditor(service, options=AuditOptions())` and `audit(source)`.
A fake dependency source supplies 1 dependency.
A fake vulnerability service returns the specified records for that dependency.
The check uses Python standard-library utilities and the installed library API, without a test framework or network requests.

Example 1 contains `PYSEC-0` aliased to `CVE-2024-0001`, plus `GHSA-aaaa` aliased to `CVE-2024-0001` and itself.
Example 2 removes that GHSA self-alias and adds a third record: `CVE-2024-0001` aliased to `GHSA-aaaa`.
Run every provider order: 2 permutations for example 1 and 6 for example 2.
Create new records for every permutation, so mutations cannot affect later cases.
Each case requires 1 dependency result, 1 emitted vulnerability, primary id `PYSEC-0`, and the exact merged alias union `{CVE-2024-0001, GHSA-aaaa}`.
The primary id must not appear in its own alias set.

Run 3 sequential Modal cells through unmodified `cert/cells.py` and the original package:
1. Unpatched negative control.
2. `real-2`, candidate SHA256 `36732e9915745a857f0369c29feb83934f3f7aba927f852c02895aff820aa003`.
3. `real-3`, candidate SHA256 `53787d3060a3197f0cb6689d95916c462496ee11bf0e0894577221d21cce8624`.

Verify both candidate hashes against `audit/existing-alternatives.json` before any execution.
Apply patches only with `cells.source_patch_script`; permit only `pip_audit/_audit.py` source changes.
Do not modify any original package file, hidden test, or harness.
The public check executes in the agent script, before the unchanged native verifier.
The native verifier may execute under the parent's explicit direction; its reward is incidental and separate from public acceptance.
No hidden tests execute inside the public check.

Freeze this preregistration, the check, evaluator, instruction, audit metadata, and patch hashes before execution.
Keep raw receipts outside the repository. Preserve all 3 receipts and their SHA256 values.
No model calls or injection controls are permitted.
Use app `ov1104-wave2-grader-cert3` and the existing shared ledger.
The incremental estimated spend cap is $0.10; a reserve check enforces it while preserving the shared ledger's existing ceiling.
Authorization: `Peter 2026-11-03 23:35 ET: $30 overnight infra budget (ov1104), lane cap per PROGRAM.md`.
Stop execution by 5:45 AM ET.

## Acceptance and failure rules

`PUBLIC_2391_PASSED` appears only after all 8 cases pass.
Record that public marker independently from the native reward.
Record the readiness marker, explicit assertion-failure marker, source-patch marker, receipt path/hash, patch hash, and estimated cost.

The negative control must start the check, report a behavioral assertion failure, and lack the pass marker.
A missing marker caused only by import, execution, or infrastructure failure is not a successful control.
An unexpected control pass is a check defect, not evidence for acceptance.
If the negative control is invalid, stop and report the defect without claiming acceptance.
A candidate passes only when the receipt is executed, its source-patch marker exists, and its public pass marker exists.
Report 2/2, 1/2, or 0/2 only with a valid negative control and all candidate receipts available.
Do not tune the check after observing results.

These patches were already shown to the old synthesizer.
This experiment does not provide certification, new held-out alternatives, or any Gate 7 impact.
