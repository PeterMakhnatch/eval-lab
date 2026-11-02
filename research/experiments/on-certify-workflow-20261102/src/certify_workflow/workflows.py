"""Temporal workflows for corpus-scale gold certification: CertifyCorpusWorkflow and CertifyPackageWorkflow."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from certify_workflow.activities import (
        generate_mutants,
        run_cell,
        settle_spend,
        synthesize_tests,
        write_manifest,
    )
    from certify_workflow.models import (
        CellResult,
        CellSpec,
        CorpusBoard,
        CorpusManifest,
        CorpusPlan,
        CorpusWorkflowInput,
        FakeCellConfig,
        MutantSummary,
        PackageCertificationResult,
        PackageSpec,
        SettleSpendRequest,
    )


def compute_cell_key(package_id: str, cell_type: str, qualifier: str = "") -> str:
    """Deterministic hash of a cell's evaluation parameters."""
    payload = f"{package_id}\0{cell_type}\0{qualifier}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@workflow.defn
class CertifyPackageWorkflow:
    """Child workflow certifying a single task package through the 6-gate mutation pipeline."""

    def __init__(self) -> None:
        self._status: str = "pending"
        self._cell_count: int = 0
        self._cancelled: bool = False

    @workflow.signal
    def cancel(self) -> None:
        """Cancel in-flight package certification."""
        self._cancelled = True

    @workflow.query
    def package_status(self) -> str:
        return self._status

    @workflow.run
    async def run(
        self,
        pkg: PackageSpec,
        config: FakeCellConfig,
        task_queue_override: str | None = None,
        task_queue_synth: str | None = None,
        task_queue_modal: str | None = None,
    ) -> PackageCertificationResult:
        if isinstance(pkg, dict):
            pkg = PackageSpec(**pkg)
        if isinstance(config, dict):
            config = FakeCellConfig(**config)

        self._status = "running"
        if self._cancelled:
            self._status = "cancelled"
            return PackageCertificationResult(
                package_id=pkg.package_id,
                status="cancelled",
                failure_reason="Cancelled by signal",
            )

        tq = task_queue_override or workflow.info().task_queue
        tq_synth = task_queue_synth or tq
        tq_modal = task_queue_modal or tq
        retry_policy_infra = RetryPolicy(
            initial_interval=timedelta(milliseconds=100),
            backoff_coefficient=2.0,
            maximum_attempts=4,
            non_retryable_error_types=["SubmissionFailure"],
        )

        # 1. Activity: generate_mutants
        try:
            mutants: list[MutantSummary] = await workflow.execute_activity(
                generate_mutants,
                pkg,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy_infra,
                task_queue=tq,
            )
        except Exception as exc:
            self._status = "failed"
            return PackageCertificationResult(
                package_id=pkg.package_id,
                status="failed",
                failure_reason=f"generate_mutants failed: {exc}",
            )
        # 2. Deterministic mutant split (train vs eval)
        mutants = [MutantSummary(**m) if isinstance(m, dict) else m for m in mutants]
        train_mutants = [m for m in mutants if m.is_train]
        eval_mutants = [m for m in mutants if not m.is_train]

        # 3. Activity: synthesize_tests (LLM synthesizer route)
        try:
            await workflow.execute_activity(
                synthesize_tests,
                args=[pkg, train_mutants],
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy_infra,
                task_queue=tq_synth,
            )
        except Exception as exc:
            self._status = "failed"
            return PackageCertificationResult(
                package_id=pkg.package_id,
                status="failed",
                failure_reason=f"synthesize_tests failed: {exc}",
            )

        # 4. Plan evaluation cells
        cells_to_run: list[CellSpec] = [
            CellSpec(
                cell_key=compute_cell_key(pkg.package_id, "reference"),
                package_id=pkg.package_id,
                cell_type="reference",
                expected_reward=1.0,
            ),
            CellSpec(
                cell_key=compute_cell_key(pkg.package_id, "nop"),
                package_id=pkg.package_id,
                cell_type="nop",
                expected_reward=0.0,
            ),
            CellSpec(
                cell_key=compute_cell_key(pkg.package_id, "witness"),
                package_id=pkg.package_id,
                cell_type="witness",
                expected_reward=0.0,
            ),
            CellSpec(
                cell_key=compute_cell_key(pkg.package_id, "exit0_pth"),
                package_id=pkg.package_id,
                cell_type="exit0_pth",
                expected_reward=0.0,
            ),
        ]

        # Train mutants on upgraded package
        for m in train_mutants:
            cells_to_run.append(
                CellSpec(
                    cell_key=compute_cell_key(pkg.package_id, "train_mutant", m.sha256),
                    package_id=pkg.package_id,
                    cell_type="train_mutant",
                    operator=m.operator,
                    mutant_line=m.line,
                    expected_reward=0.0,
                )
            )

        # Eval mutants on upgraded package
        for m in eval_mutants:
            cells_to_run.append(
                CellSpec(
                    cell_key=compute_cell_key(pkg.package_id, "eval_mutant", m.sha256),
                    package_id=pkg.package_id,
                    cell_type="eval_mutant",
                    operator=m.operator,
                    mutant_line=m.line,
                    expected_reward=0.0,
                )
            )

        # Eval mutants on base package (to detect which mutants base grader accepted)
        for m in eval_mutants:
            cells_to_run.append(
                CellSpec(
                    cell_key=compute_cell_key(pkg.package_id, "eval_base_mutant", m.sha256),
                    package_id=pkg.package_id,
                    cell_type="eval_base_mutant",
                    operator=m.operator,
                    mutant_line=m.line,
                    expected_reward=1.0,
                )
            )

        # 5. Execute Cells on Modal route
        cell_results: list[CellResult] = []
        for cell_spec in cells_to_run:
            if self._cancelled:
                self._status = "cancelled"
                return PackageCertificationResult(
                    package_id=pkg.package_id,
                    status="cancelled",
                    failure_reason="Cancelled by signal mid-cells",
                    cell_results=cell_results,
                )

            try:
                res: CellResult = await workflow.execute_activity(
                    run_cell,
                    args=[cell_spec, config],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy_infra,
                    task_queue=tq_modal,
                )
                if isinstance(res, dict):
                    res = CellResult(**res)
                cell_results.append(res)
                self._cell_count += 1
            except ActivityError as exc:
                cause = exc.cause
                if isinstance(cause, ApplicationError) and cause.type == "SubmissionFailure":
                    # Non-retryable candidate failure
                    cell_results.append(
                        CellResult(
                            cell_key=cell_spec.cell_key,
                            package_id=pkg.package_id,
                            cell_type=cell_spec.cell_type,
                            exit_code=1,
                            reward=0.0,
                            cost_usd=config.base_cost_usd,
                            error=f"SubmissionFailure: {cause.message}",
                        )
                    )
                else:
                    self._status = "failed"
                    return PackageCertificationResult(
                        package_id=pkg.package_id,
                        status="failed",
                        failure_reason=f"Cell execution failed on {cell_spec.cell_key}: {exc}",
                        cell_results=cell_results,
                    )

        # 6. Settle spend
        pkg_cost = sum(c.cost_usd for c in cell_results)
        await workflow.execute_activity(
            settle_spend,
            SettleSpendRequest(
                package_id=pkg.package_id,
                reserved_usd=pkg_cost,
                actual_usd=pkg_cost,
                ledger_file=config.ledger_file,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            task_queue=tq,
        )

        # 7. Evaluate 6-Gate Verdict
        # Gate checks:
        results_by_type: dict[str, list[CellResult]] = {}
        for r in cell_results:
            results_by_type.setdefault(r.cell_type, []).append(r)

        ref_ok = any(r.exit_code == 0 and r.reward == 1.0 for r in results_by_type.get("reference", []))
        nop_ok = all(r.exit_code == 0 and r.reward == 0.0 for r in results_by_type.get("nop", []))
        witness_ok = all(r.exit_code == 0 and r.reward == 0.0 for r in results_by_type.get("witness", []))
        exit0_ok = all(r.exit_code == 0 and r.reward == 0.0 for r in results_by_type.get("exit0_pth", []))
        train_ok = all(r.exit_code == 0 and r.reward == 0.0 for r in results_by_type.get("train_mutant", []))

        structural_pass = ref_ok and nop_ok and witness_ok and exit0_ok and train_ok

        # Eval mutants kill rate among base survivors
        eval_res = results_by_type.get("eval_mutant", [])

        # Count mutants accepted by base grader (reward == 1.0)
        killed_count = 0
        total_eligible = 0
        for r in eval_res:
            total_eligible += 1
            if r.reward == 0.0:
                killed_count += 1

        kill_rate = (killed_count / total_eligible) if total_eligible > 0 else 0.0
        gold_heldout = structural_pass and (kill_rate >= 0.75 or total_eligible == 0)

        if gold_heldout:
            status = "gold_heldout"
        elif structural_pass:
            status = "gold_structural"
        else:
            status = "not_gold"

        self._status = status
        return PackageCertificationResult(
            package_id=pkg.package_id,
            status=status,
            failure_reason="" if structural_pass else "Structural gate failure",
            structural_pass=structural_pass,
            heldout_kill_rate=kill_rate,
            killed_mutants=killed_count,
            total_eval_mutants=total_eligible,
            cost_usd=pkg_cost,
            cell_results=cell_results,
        )


@workflow.defn
class CertifyCorpusWorkflow:
    """Master workflow orchestrating corpus-scale certification with dynamic spend approvals."""

    def __init__(self) -> None:
        self._cap_usd: float = 0.0
        self._paused: bool = False
        self._cancelled_packages: set[str] = set()
        self._running_packages: set[str] = set()
        self._completed_packages: list[PackageCertificationResult] = []
        self._cumulative_spend: float = 0.0
        self._manifest_written: bool = False
        self._is_partial: bool = False
        self._package_statuses: dict[str, str] = {}
        self._plan: CorpusPlan | None = None
        self._active_modal: int = 0
        self._active_synth: int = 0

    @workflow.signal
    def raise_cap(self, usd: float, approver: str = "") -> None:
        """Dynamically raise the spend cap, unblocking parked workflows."""
        self._cap_usd += usd

    @workflow.signal
    def pause(self, paused: bool = True) -> None:
        """Pause or unpause workflow dispatch."""
        self._paused = paused

    @workflow.signal
    def cancel_package(self, package_id: str) -> None:
        """Cancel a pending or running package."""
        self._cancelled_packages.add(package_id)

    @workflow.query
    def board(self) -> CorpusBoard:
        """Query the live certification status board."""
        total = len(self._plan.packages) if self._plan else 0
        return CorpusBoard(
            total_packages=total,
            completed_packages=len(self._completed_packages),
            failed_packages=sum(1 for p in self._completed_packages if p.status in ("failed", "cap_exhausted")),
            running_packages=list(self._running_packages),
            paused=self._paused,
            cumulative_spend=self._cumulative_spend,
            cap_usd=self._cap_usd,
            coverage_k=len(self._completed_packages),
            coverage_n=total,
            manifest_written=self._manifest_written,
            is_partial=self._is_partial,
            package_statuses=dict(self._package_statuses),
        )
    @workflow.run
    async def run(
        self,
        inp: CorpusWorkflowInput | CorpusPlan,
    ) -> CorpusManifest:
        if isinstance(inp, dict):
            inp = CorpusWorkflowInput(**inp) if "plan" in inp else CorpusPlan(**inp)

        if isinstance(inp, CorpusPlan):
            plan = inp
            generation = 1
            prior_completed: list[PackageCertificationResult] = []
            prior_spend = 0.0
            pending_indices = list(range(len(plan.packages)))
            current_cap = None
        else:
            plan = inp.plan
            generation = inp.generation
            prior_completed = inp.prior_completed
            prior_spend = inp.prior_spend
            pending_indices = inp.pending_indices
            current_cap = inp.current_cap

        if isinstance(plan, dict):
            plan = CorpusPlan(**plan)
        plan.packages = [PackageSpec(**p) if isinstance(p, dict) else p for p in plan.packages]
        if isinstance(plan.fake_config, dict):
            plan.fake_config = FakeCellConfig(**plan.fake_config)

        self._plan = plan
        self._cap_usd = current_cap if current_cap is not None else plan.cap_usd
        self._cumulative_spend = prior_spend
        self._completed_packages = [
            PackageCertificationResult(**p) if isinstance(p, dict) else p for p in prior_completed
        ]

        # Populate initial statuses
        for res in self._completed_packages:
            self._package_statuses[res.package_id] = res.status

        indices = pending_indices if pending_indices is not None else list(range(len(plan.packages)))
        for i in indices:
            pkg = plan.packages[i]
            if pkg.package_id not in self._package_statuses:
                self._package_statuses[pkg.package_id] = "pending"

        cells_executed_in_gen = 0
        tq = workflow.info().task_queue
        idx_pointer = 0

        while idx_pointer < len(indices):
            # 1. Pause check
            if self._paused:
                await workflow.wait_condition(lambda: not self._paused)

            curr_idx = indices[idx_pointer]
            pkg = plan.packages[curr_idx]

            # 2. Check if cancelled before starting
            if pkg.package_id in self._cancelled_packages:
                self._package_statuses[pkg.package_id] = "cancelled"
                self._completed_packages.append(
                    PackageCertificationResult(
                        package_id=pkg.package_id,
                        status="cancelled",
                        failure_reason="Cancelled by signal before launch",
                    )
                )
                idx_pointer += 1
                continue

            # 3. Spend Gate Check: estimate package cost
            # Reference, nop, witness, exit0, plus train/eval/evalbase mutants
            est_cells = 4 + (pkg.mutant_count * 2)
            est_cost = est_cells * plan.fake_config.base_cost_usd

            if self._cumulative_spend + est_cost > self._cap_usd:
                # Cap reached: park and wait for raise_cap signal or timeout
                projected_needed = self._cumulative_spend + est_cost
                timed_out = False
                try:
                    await workflow.wait_condition(
                        lambda projected=projected_needed: self._cap_usd >= projected or self._paused,
                        timeout=timedelta(seconds=plan.cap_timeout_seconds),
                    )
                except TimeoutError:
                    timed_out = True

                if (timed_out or self._cap_usd < projected_needed) and not self._paused:
                    self._is_partial = True
                    for remaining_idx in indices[idx_pointer:]:
                        rem_pkg = plan.packages[remaining_idx]
                        self._package_statuses[rem_pkg.package_id] = "cap_exhausted"
                        self._completed_packages.append(
                            PackageCertificationResult(
                                package_id=rem_pkg.package_id,
                                status="cap_exhausted",
                                failure_reason=f"CapReached: spend cap ${self._cap_usd:.4f} exceeded without signal approval",
                            )
                        )
                    break

            # 4. Route Concurrency Enforcement
            # Wait until modal and synth route slots are available
            await workflow.wait_condition(
                lambda: (self._active_modal < plan.modal_concurrency and self._active_synth < plan.synth_concurrency)
                or self._paused
            )
            if self._paused:
                continue

            # Reserve route slots and mark running
            self._active_modal += 1
            self._active_synth += 1
            self._running_packages.add(pkg.package_id)
            self._package_statuses[pkg.package_id] = "running"

            # 5. Launch Child Package Workflow
            try:
                pkg_res = await workflow.execute_child_workflow(
                    CertifyPackageWorkflow.run,
                    args=[pkg, plan.fake_config, tq],
                    id=f"{workflow.info().workflow_id}-pkg-{pkg.package_id}",
                    task_queue=tq,
                )
                if isinstance(pkg_res, dict):
                    pkg_res = PackageCertificationResult(**pkg_res)
            except Exception as exc:
                pkg_res = PackageCertificationResult(
                    package_id=pkg.package_id,
                    status="failed",
                    failure_reason=f"Child workflow execution failed: {exc}",
                )
            finally:
                self._active_modal -= 1
                self._active_synth -= 1
                self._running_packages.discard(pkg.package_id)

            self._completed_packages.append(pkg_res)
            self._package_statuses[pkg.package_id] = pkg_res.status
            self._cumulative_spend += pkg_res.cost_usd
            cells_executed_in_gen += len(pkg_res.cell_results)
            idx_pointer += 1

            # 6. Continue-As-New check for history size control
            if cells_executed_in_gen >= plan.continue_as_new_threshold and idx_pointer < len(indices):
                remaining_indices = indices[idx_pointer:]
                workflow.continue_as_new(
                    CorpusWorkflowInput(
                        plan=plan,
                        generation=generation + 1,
                        prior_completed=self._completed_packages,
                        prior_spend=self._cumulative_spend,
                        pending_indices=remaining_indices,
                        current_cap=self._cap_usd,
                    )
                )

        # 7. Finalize and Write Manifest
        certified_count = sum(1 for p in self._completed_packages if p.status in ("gold_structural", "gold_heldout"))
        total_pkgs = len(plan.packages)
        coverage_k = len(self._completed_packages)

        manifest = CorpusManifest(
            timestamp=workflow.now().isoformat(),
            total_packages=total_pkgs,
            certified_packages=certified_count,
            partial=self._is_partial,
            coverage_k=coverage_k,
            coverage_n=total_pkgs,
            cumulative_spend_usd=self._cumulative_spend,
            cap_usd=self._cap_usd,
            packages={p.package_id: p.to_dict() for p in self._completed_packages},
        )

        await workflow.execute_activity(
            write_manifest,
            args=[manifest, plan.manifest_output_dir],
            start_to_close_timeout=timedelta(seconds=30),
            task_queue=tq,
        )
        self._manifest_written = True

        return manifest
