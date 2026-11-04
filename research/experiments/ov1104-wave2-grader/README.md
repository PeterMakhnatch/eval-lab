# Wave 2: grader trust

## Question

Do the earlier certificates survive a provenance audit?
Can blind alternative solutions strengthen the certification evidence?
Do cheap benign negative controls identify weak DEV graders with existing local images?

## How to read

1. Read `PREREG.md` for the fixed scope and status rules.
2. Read `CERT-AUDIT.md` for the prior certification failures.
3. Read `audit/REPORT.md` and its aggregate for deterministic provenance checks.
4. Read `cert/PREREG.md` and the certification aggregate for new blind alternatives and gate evidence.
5. Read `smoke/PREREG.md` and the smoke aggregate for image coverage, probe outcomes, confidence intervals, and mutation overlap.

The certification audit is retrospective. New alternative generation and smoke scoring use separate pre-registrations.
A machine certificate is not sufficient if its alternatives see hidden tests or its evaluation set changes after scoring.
An original-grader pass is evidence, not proof of semantic correctness.

## Scope limits

This lane does not execute exit-status injection, interpreter-startup injection, or grader-tampering payloads.
The requested full three-probe X07 gate is therefore not measured.
Benign null and inert token-grid probes are a partial defensive census, not a replacement for that gate.
Missing images, unsupported candidate interfaces, and runtime errors remain missing results.
A certificate with an unexercised required control is not reported as a fresh full certification.

## Provenance and delivery

Prior source: merged PR #1865, `research/experiments/ov1104-grader/`.
Independent audit source: `/private/tmp/ov1104/verify/VERIFY.md`, grader section.
Runtime evidence lives outside this worktree. Aggregates retain receipt paths and hashes.
The root pre-registration SHA-256 is `101840bd36ee6458b38a4d549793ddd825fcadf962457e656ab773b4841243d7`.
Its initial commit is `f3b1d27ba`.
Only the certification slice may spend, within the total $2 Modal cap.
Reported resource estimates are not provider invoices.
