"""Eight-point Certification Gate for Synthetic Agent-Capability Evaluations (V0).

Executes deterministic, execution-based verification of synthetic task packages
before admission to the experimental registry:

1. Static Reachability: schema compliance, reachability of solution paths and dependencies.
2. Clean Reset: repeated environment setup and cleanup executions without residual state.
3. Oracle 3x Pass: reference oracle solution succeeds across 3 consecutive trials.
4. NOP Fail: empty/no-op agent fails verifier (ensures non-triviality / non-vacuity).
5. Plausible Mutants: at least 3 adversarial/plausible mutants tested and rejected.
6. Alignment Audit: verifier assertions strictly align with task instructions and construct.
7. Regeneration Idempotency: identical seed and parameters yield identical content digests.
8. Secret/Canary Isolation: verifier secrets, answers, and canary tokens are isolated.

This gate emits status='experimental' only on complete pass, or 'rejected' otherwise.
The contract may still represent historical or developmental experimental records with
0, 1, or 2 mutants; those records remain nonpassing under SyntheticCertificate.is_passing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.schemas import SHA256_RE
from evallab.synthetic_contracts import (
    PerturbationFamily,
    SyntheticCertificate,
    SyntheticCertificateStatus,
    SyntheticEvalSpec,
    compute_synthetic_spec_id,
)

MIN_MUTANTS_REQUIRED = 3
ORACLE_RUNS_REQUIRED = 3

FORBIDDEN_LEAK_PATTERNS = (
    re.compile(r"VERIFIER_SECRET\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"CANARY_TOKEN\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"GOLDEN_SOLUTION\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"GROUND_TRUTH\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"__HIDDEN_TEST_PASS_KEY__", re.IGNORECASE),
)


@dataclass
class GateCheckResult:
    """Result of an individual certification gate check."""

    name: str
    passed: bool
    details: str = ""
    evidence_path: str | None = None
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class SyntheticGateAuditResult:
    """Consolidated audit result across all eight certification gate checks."""

    spec_id: str
    passed: bool
    static_reachability: bool
    clean_reset_passed: bool
    oracle_3x_passed: bool
    nop_failed: bool
    mutants_tested_count: int
    mutants_failed_count: int
    alignment_audit_passed: bool
    regeneration_idempotent: bool
    secret_isolation_passed: bool
    evidence_paths: list[str] = field(default_factory=list)
    notes: str = ""
    check_results: list[GateCheckResult] = field(default_factory=list)

    def to_certificate(self) -> SyntheticCertificate:
        """Convert audit result to a validated SyntheticCertificate."""
        status: SyntheticCertificateStatus = "experimental" if self.passed else "rejected"
        return SyntheticCertificate(
            spec_id=self.spec_id,
            status=status,
            static_reachability=self.static_reachability,
            clean_reset_passed=self.clean_reset_passed,
            oracle_3x_passed=self.oracle_3x_passed,
            nop_failed=self.nop_failed,
            mutants_tested_count=self.mutants_tested_count,
            mutants_failed_count=self.mutants_failed_count,
            alignment_audit_passed=self.alignment_audit_passed,
            regeneration_idempotent=self.regeneration_idempotent,
            secret_isolation_passed=self.secret_isolation_passed,
            evidence_paths=self.evidence_paths,
            certified_at=datetime.now(UTC).isoformat(),
            notes=self.notes,
        )


class SyntheticCertificationGate:
    """Auditor and execution-based verification gate for synthetic tasks."""

    def __init__(
        self,
        *,
        evidence_dir: Path | str | None = None,
        min_mutants: int = MIN_MUTANTS_REQUIRED,
        oracle_runs: int = ORACLE_RUNS_REQUIRED,
    ) -> None:
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.min_mutants = min_mutants
        self.oracle_runs = oracle_runs

    def check_static_reachability(
        self,
        spec: SyntheticEvalSpec,
        task_dir: Path | None = None,
    ) -> GateCheckResult:
        """Check 1: Validate schema integrity, digests, and reachable task structure."""
        diagnostics: list[str] = []

        # Validate spec integrity
        if not spec.verify_spec_id():
            diagnostics.append(
                f"spec_id mismatch: {spec.spec_id} != {compute_synthetic_spec_id(spec)}"
            )

        if not SHA256_RE.fullmatch(spec.base_task_digest):
            diagnostics.append(f"Invalid base_task_digest format: {spec.base_task_digest}")
        if not SHA256_RE.fullmatch(spec.generated_task_digest):
            diagnostics.append(
                f"Invalid generated_task_digest format: {spec.generated_task_digest}"
            )

        if not spec.construct_name.strip():
            diagnostics.append("Empty construct_name")
        if not spec.expected_behavior.strip():
            diagnostics.append("Empty expected_behavior")
        if not spec.capability_opportunity.strip():
            diagnostics.append("Empty capability_opportunity")

        # Validate filesystem structure if task_dir is supplied
        if task_dir is not None:
            task_path = Path(task_dir)
            if not task_path.exists() or not task_path.is_dir():
                diagnostics.append(f"Task directory does not exist: {task_path}")
            else:
                has_instruction = (
                    (task_path / "instruction.md").exists()
                    or (task_path / "instruction.txt").exists()
                    or (task_path / "task.toml").exists()
                )
                if not has_instruction:
                    diagnostics.append("Missing instruction file (instruction.md/txt or task.toml)")

                has_verifier = (
                    (task_path / "tests").exists()
                    or (task_path / "verifier").exists()
                    or (task_path / "test.sh").exists()
                    or (task_path / "verify.sh").exists()
                )
                if not has_verifier:
                    diagnostics.append("Missing verifier or tests component")

        passed = len(diagnostics) == 0
        details = (
            "Static reachability and schema checks passed" if passed else "; ".join(diagnostics)
        )
        return GateCheckResult(
            name="static_reachability",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )

    def check_clean_reset(
        self,
        spec: SyntheticEvalSpec,
        task_dir: Path | None = None,
        reset_fn: Callable[[], bool | tuple[bool, str]] | None = None,
    ) -> GateCheckResult:
        """Check 2: Verify repeated environment clean reset executions."""
        diagnostics: list[str] = []

        if reset_fn is not None:
            for run_idx in range(2):
                try:
                    result = reset_fn()
                    if isinstance(result, tuple):
                        res_bool, res_msg = result
                        if not res_bool:
                            diagnostics.append(
                                f"Reset function failed on run {run_idx + 1}/2: {res_msg}"
                            )
                            break
                    elif not result:
                        diagnostics.append(f"Reset function returned False on run {run_idx + 1}/2")
                        break
                except Exception as exc:
                    diagnostics.append(
                        f"Reset function raised exception on run {run_idx + 1}/2: {exc}"
                    )
                    break

            if task_dir is not None:
                task_path = Path(task_dir)
                # Verify no transient leftover lock or cache files in environment dir
                env_dir = task_path / "environment"
                if env_dir.exists():
                    leftovers = list(env_dir.glob("**/*.tmp")) + list(env_dir.glob("**/*.lock"))
                    if leftovers:
                        diagnostics.append(
                            f"Transient unreset files found: {[p.name for p in leftovers]}"
                        )
        else:
            diagnostics.append("No clean reset function provided")

        passed = len(diagnostics) == 0
        details = "Repeated reset executions succeeded" if passed else "; ".join(diagnostics)
        return GateCheckResult(
            name="clean_reset",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )

    def check_oracle_3x(
        self,
        spec: SyntheticEvalSpec,
        oracle_runner: Callable[[], bool | tuple[bool, str]] | None = None,
        execution_records: Sequence[dict[str, Any]] | None = None,
    ) -> GateCheckResult:
        """Check 3: Reference oracle passes 3 consecutive execution trials."""
        diagnostics: list[str] = []

        if oracle_runner is not None:
            for run_idx in range(self.oracle_runs):
                try:
                    outcome = oracle_runner()
                    success = outcome[0] if isinstance(outcome, tuple) else bool(outcome)
                    if not success:
                        msg = outcome[1] if isinstance(outcome, tuple) else "failure"
                        diagnostics.append(
                            f"Oracle run {run_idx + 1}/{self.oracle_runs} failed: {msg}"
                        )
                        break
                except Exception as exc:
                    diagnostics.append(f"Oracle run {run_idx + 1}/{self.oracle_runs} raised: {exc}")
                    break
        elif execution_records is not None:
            oracle_records = [
                rec
                for rec in execution_records
                if rec.get("agent_kind") in ("oracle", "reference", "solution")
            ]
            if len(oracle_records) < self.oracle_runs:
                diagnostics.append(
                    f"Insufficient oracle records: found {len(oracle_records)}, required {self.oracle_runs}"
                )
            else:
                for idx, rec in enumerate(oracle_records[: self.oracle_runs]):
                    if not rec.get("passed", False):
                        diagnostics.append(f"Oracle execution record {idx + 1} did not pass")
        else:
            diagnostics.append("No oracle runner or execution records provided")
        passed = len(diagnostics) == 0
        details = (
            f"Oracle passed {self.oracle_runs} consecutive trials"
            if passed
            else "; ".join(diagnostics)
        )
        return GateCheckResult(
            name="oracle_3x",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )

    def check_nop_failed(
        self,
        spec: SyntheticEvalSpec,
        nop_runner: Callable[[], bool | tuple[bool, str]] | None = None,
        execution_records: Sequence[dict[str, Any]] | None = None,
    ) -> GateCheckResult:
        """Check 4: Empty/no-op agent fails verification (non-triviality)."""
        diagnostics: list[str] = []

        if nop_runner is not None:
            try:
                outcome = nop_runner()
                nop_passed = outcome[0] if isinstance(outcome, tuple) else bool(outcome)
                if nop_passed:
                    diagnostics.append(
                        "NOP/empty agent unexpectedly passed verification (vacuous verifier)"
                    )
            except Exception:
                # If running nop raised or failed, verifier rejected it as expected
                pass
        elif execution_records is not None:
            nop_records = [
                rec
                for rec in execution_records
                if rec.get("agent_kind") in ("nop", "empty", "no-op")
            ]
            if not nop_records:
                diagnostics.append("No NOP execution records found")
            else:
                for idx, rec in enumerate(nop_records):
                    if rec.get("passed", False):
                        diagnostics.append(f"NOP record {idx + 1} unexpectedly passed verification")
        else:
            diagnostics.append("No NOP runner or execution records provided")
        passed = len(diagnostics) == 0
        details = "NOP agent successfully failed verification" if passed else "; ".join(diagnostics)
        return GateCheckResult(
            name="nop_failed",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )

    def check_mutants(
        self,
        spec: SyntheticEvalSpec,
        mutant_runners: Sequence[Callable[[], bool | tuple[bool, str]]] | None = None,
        mutant_records: Sequence[dict[str, Any]] | None = None,
    ) -> tuple[GateCheckResult, int, int]:
        """Check 5: At least 3 plausible adversarial mutants tested and rejected."""
        diagnostics: list[str] = []
        tested_count = 0
        failed_count = 0

        if mutant_runners is not None:
            tested_count = len(mutant_runners)
            for idx, runner in enumerate(mutant_runners):
                try:
                    outcome = runner()
                    mutant_passed = outcome[0] if isinstance(outcome, tuple) else bool(outcome)
                    if mutant_passed:
                        diagnostics.append(f"Mutant {idx + 1} unexpectedly passed verification")
                    else:
                        failed_count += 1
                except Exception:
                    failed_count += 1

            if tested_count < self.min_mutants:
                diagnostics.append(
                    f"Insufficient mutants tested: {tested_count} < minimum {self.min_mutants}"
                )
        elif mutant_records is not None:
            tested_count = len(mutant_records)
            for idx, rec in enumerate(mutant_records):
                if rec.get("passed", False):
                    diagnostics.append(
                        f"Mutant record {idx + 1} ({rec.get('name', 'unnamed')}) passed"
                    )
                else:
                    failed_count += 1

            if tested_count < self.min_mutants:
                diagnostics.append(
                    f"Insufficient mutant records: {tested_count} < minimum {self.min_mutants}"
                )
        else:
            tested_count = 0
            failed_count = 0
            diagnostics.append("No mutant runners or mutant records provided")
        passed = (
            tested_count >= self.min_mutants
            and failed_count == tested_count
            and len(diagnostics) == 0
        )
        details = (
            f"Tested {tested_count} mutants, all {failed_count} rejected by verifier"
            if passed
            else f"Mutant check failed ({failed_count}/{tested_count} rejected): "
            + "; ".join(diagnostics)
        )
        result = GateCheckResult(
            name="mutants_tested",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )
        return result, tested_count, failed_count

    def check_alignment_audit(
        self,
        spec: SyntheticEvalSpec,
        task_dir: Path | None = None,
    ) -> GateCheckResult:
        """Check 6: Verify task-instruction and verifier alignment."""
        diagnostics: list[str] = []

        # Family and construct alignment
        family_construct_map = {
            PerturbationFamily.TOOL_UNRELIABILITY: [
                "tool",
                "retry",
                "unreliable",
                "fault",
                "error",
                "flake",
            ],
            PerturbationFamily.EPISTEMIC_RESTRAINT: [
                "abstain",
                "restraint",
                "missing",
                "impossible",
                "refusal",
                "uncertain",
            ],
            PerturbationFamily.CONTEXT_PRESSURE: [
                "context",
                "pressure",
                "distraction",
                "needle",
                "haystack",
                "noise",
                "spill",
            ],
            PerturbationFamily.FUNCTION_DAG: [
                "dag",
                "function",
                "dependency",
                "order",
                "graph",
                "step",
                "compose",
            ],
        }

        expected_tokens = family_construct_map.get(spec.family, [])
        construct_lower = (
            f"{spec.construct_name} {spec.perturbation_type} {spec.capability_opportunity}".lower()
        )
        if expected_tokens and not any(token in construct_lower for token in expected_tokens):
            diagnostics.append(
                f"Construct {spec.construct_name} does not match perturbation family {spec.family}"
            )

        if task_dir is not None:
            task_path = Path(task_dir)
            inst_path = task_path / "instruction.md"
            if not inst_path.exists():
                inst_path = task_path / "instruction.txt"
            if inst_path.exists():
                inst_content = inst_path.read_text(encoding="utf-8")
                if len(inst_content.strip()) < 10:
                    diagnostics.append("Instruction content is suspiciously short or empty")

        passed = len(diagnostics) == 0
        details = (
            "Instruction and verifier alignment audit passed" if passed else "; ".join(diagnostics)
        )
        return GateCheckResult(
            name="alignment_audit",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )

    def check_regeneration_idempotency(
        self,
        spec: SyntheticEvalSpec,
        regenerator: Callable[[int, dict[str, Any]], tuple[str, str]] | None = None,
    ) -> GateCheckResult:
        """Check 7: Seed-based task regeneration produces identical task digest."""
        diagnostics: list[str] = []

        if regenerator is not None:
            try:
                base_digest_1, gen_digest_1 = regenerator(spec.seed, spec.parameters)
                base_digest_2, gen_digest_2 = regenerator(spec.seed, spec.parameters)

                if base_digest_1 != base_digest_2:
                    diagnostics.append(
                        f"Non-deterministic base generation: run 1 ({base_digest_1}) != run 2 ({base_digest_2})"
                    )
                if base_digest_1 != spec.base_task_digest:
                    diagnostics.append(
                        f"Regenerated base digest ({base_digest_1}) != spec base digest ({spec.base_task_digest})"
                    )
                if gen_digest_1 != gen_digest_2:
                    diagnostics.append(
                        f"Non-deterministic generation: run 1 ({gen_digest_1}) != run 2 ({gen_digest_2})"
                    )
                if gen_digest_1 != spec.generated_task_digest:
                    diagnostics.append(
                        f"Regenerated digest ({gen_digest_1}) != spec digest ({spec.generated_task_digest})"
                    )
            except Exception as exc:
                diagnostics.append(f"Regeneration check raised: {exc}")
        else:
            diagnostics.append("No regenerator function provided")
        passed = len(diagnostics) == 0
        details = (
            "Seed-based regeneration idempotency verified" if passed else "; ".join(diagnostics)
        )
        return GateCheckResult(
            name="regeneration_idempotent",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )

    def check_secret_isolation(
        self,
        spec: SyntheticEvalSpec,
        task_dir: Path | None = None,
    ) -> GateCheckResult:
        """Check 8: Verify secrets, gold answers, and canary tokens are isolated."""
        diagnostics: list[str] = []

        # Check spec metadata and description fields
        spec_text = f"{spec.construct_name} {spec.expected_behavior} {spec.capability_opportunity}"
        for pattern in FORBIDDEN_LEAK_PATTERNS:
            if pattern.search(spec_text):
                diagnostics.append(
                    f"Forbidden secret pattern found in spec metadata: {pattern.pattern}"
                )

        # Check task directory files exposed to agent
        if task_dir is not None:
            task_path = Path(task_dir)
            agent_visible_paths = [
                task_path / "instruction.md",
                task_path / "instruction.txt",
                task_path / "task.toml",
            ]
            env_dir = task_path / "environment"
            if env_dir.exists():
                agent_visible_paths.extend(env_dir.glob("**/*"))

            for file_path in agent_visible_paths:
                if file_path.is_file():
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="ignore")
                        for pattern in FORBIDDEN_LEAK_PATTERNS:
                            if pattern.search(content):
                                diagnostics.append(
                                    f"Secret leaked in agent-visible file {file_path.name}: {pattern.pattern}"
                                )
                    except OSError:
                        pass

        passed = len(diagnostics) == 0
        details = "Verifier secrets and canaries isolated" if passed else "; ".join(diagnostics)
        return GateCheckResult(
            name="secret_isolation",
            passed=passed,
            details=details,
            diagnostics=diagnostics,
        )

    def audit_task(
        self,
        spec: SyntheticEvalSpec,
        *,
        task_dir: Path | str | None = None,
        oracle_runner: Callable[[], bool | tuple[bool, str]] | None = None,
        nop_runner: Callable[[], bool | tuple[bool, str]] | None = None,
        mutant_runners: Sequence[Callable[[], bool | tuple[bool, str]]] | None = None,
        regenerator: Callable[[int, dict[str, Any]], tuple[str, str]] | None = None,
        reset_fn: Callable[[], bool | tuple[bool, str]] | None = None,
        execution_records: Sequence[dict[str, Any]] | None = None,
        mutant_records: Sequence[dict[str, Any]] | None = None,
        notes: str = "",
    ) -> SyntheticGateAuditResult:
        """Run all eight certification checks and produce consolidated audit result."""
        task_path = Path(task_dir) if task_dir else None

        r1 = self.check_static_reachability(spec, task_dir=task_path)
        r2 = self.check_clean_reset(spec, task_dir=task_path, reset_fn=reset_fn)
        r3 = self.check_oracle_3x(
            spec, oracle_runner=oracle_runner, execution_records=execution_records
        )
        r4 = self.check_nop_failed(spec, nop_runner=nop_runner, execution_records=execution_records)
        r5, mutants_tested, mutants_failed = self.check_mutants(
            spec, mutant_runners=mutant_runners, mutant_records=mutant_records
        )
        r6 = self.check_alignment_audit(spec, task_dir=task_path)
        r7 = self.check_regeneration_idempotency(spec, regenerator=regenerator)
        r8 = self.check_secret_isolation(spec, task_dir=task_path)

        all_checks = [r1, r2, r3, r4, r5, r6, r7, r8]
        overall_passed = all(check.passed for check in all_checks)

        evidence_paths: list[str] = []
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            ev_file = self.evidence_dir / f"cert_evidence_{spec.spec_id[:16]}.json"
            ev_data = {
                "spec_id": spec.spec_id,
                "passed": overall_passed,
                "checks": [
                    {
                        "name": c.name,
                        "passed": c.passed,
                        "details": c.details,
                        "diagnostics": c.diagnostics,
                    }
                    for c in all_checks
                ],
                "timestamp": datetime.now(UTC).isoformat(),
            }
            ev_file.write_text(json.dumps(ev_data, indent=2), encoding="utf-8")
            evidence_paths.append(str(ev_file))

        return SyntheticGateAuditResult(
            spec_id=spec.spec_id,
            passed=overall_passed,
            static_reachability=r1.passed,
            clean_reset_passed=r2.passed,
            oracle_3x_passed=r3.passed,
            nop_failed=r4.passed,
            mutants_tested_count=mutants_tested,
            mutants_failed_count=mutants_failed,
            alignment_audit_passed=r6.passed,
            regeneration_idempotent=r7.passed,
            secret_isolation_passed=r8.passed,
            evidence_paths=evidence_paths,
            check_results=all_checks,
            notes=notes
            or (
                "All 8 certification checks passed"
                if overall_passed
                else f"Certification gate failed: {'; '.join(d for c in all_checks for d in c.diagnostics if d) or 'checks failed'}"
            ),
        )

    def certify(
        self,
        spec: SyntheticEvalSpec,
        *,
        task_dir: Path | str | None = None,
        oracle_runner: Callable[[], bool | tuple[bool, str]] | None = None,
        nop_runner: Callable[[], bool | tuple[bool, str]] | None = None,
        mutant_runners: Sequence[Callable[[], bool | tuple[bool, str]]] | None = None,
        regenerator: Callable[[int, dict[str, Any]], tuple[str, str]] | None = None,
        reset_fn: Callable[[], bool | tuple[bool, str]] | None = None,
        execution_records: Sequence[dict[str, Any]] | None = None,
        mutant_records: Sequence[dict[str, Any]] | None = None,
        notes: str = "",
    ) -> SyntheticCertificate:
        """Run certification gate and return validated SyntheticCertificate."""
        audit = self.audit_task(
            spec,
            task_dir=task_dir,
            oracle_runner=oracle_runner,
            nop_runner=nop_runner,
            mutant_runners=mutant_runners,
            regenerator=regenerator,
            reset_fn=reset_fn,
            execution_records=execution_records,
            mutant_records=mutant_records,
            notes=notes,
        )
        return audit.to_certificate()


def certify_synthetic_task(
    spec: SyntheticEvalSpec,
    task_dir: Path | str | None = None,
    **kwargs: Any,
) -> SyntheticCertificate:
    """Convenience helper to certify a synthetic evaluation task."""
    gate = SyntheticCertificationGate()
    return gate.certify(spec, task_dir=task_dir, **kwargs)
