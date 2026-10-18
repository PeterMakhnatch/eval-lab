"""Execute only an explicitly Verifier-accepted v7 release, with durable reservations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import runpy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from evallab import runner
from evallab.qa import counterfactual as cf
from evallab.registry import harbor_task_digest
from evallab.regrade import CloudSpendGate, SpendCapReached

HERE = Path(__file__).resolve().parent
INVENTORY = runpy.run_path(str(HERE / "regrade.py"))
REPORT = runpy.run_path(str(HERE / "report.py"))
file_ref = INVENTORY["file_ref"]
bound_bytes = INVENTORY["bound_bytes"]
create_json = INVENTORY["create_json"]
CAP_USD = 3.0
APP = "mimo-clean-census-v7-classifiers-20261017"
APPROVAL = "Peter-approved 2026-10-18 focused sprint; cumulative heldout regrade $3 inside Trainer $12 (native $6.50, serving $2.50); ledger 2026-10-17-v7"


def admit(
    inventory: dict[str, Any], manifest_path: Path, accepted_sha: str, verifier_receipt: Path,
) -> tuple[dict[str, Any], dict[str, cf.Grader]]:
    """Bind a released producer artifact to an independently accepted exact hash.

    accepted_sha is the operator's explicit Verifier-accepted identity, not an
    inferred verdict. Preserve the independent receipt bytes; do not pretend a
    Markdown keyword search constitutes independent review.
    """
    data, manifest_ref = bound_bytes({"path": str(manifest_path), "sha256": accepted_sha})
    manifest = json.loads(data)
    if (manifest.get("schema") != "mimo.heldout/v1" or manifest.get("successor_version") != "v7"
            or manifest.get("scoring_allowed") is not True or manifest.get("freeze_ready") is not True
            or manifest.get("pending_gates") != []
            or str((manifest.get("independent_verifier") or {}).get("status")).lower() != "pass"):
        raise ValueError("v7 producer release and independent Verifier PASS are required")
    canonical_task = INVENTORY["canonical_task"]
    tasks = manifest["heldout"]
    if (len(tasks) != 33 or len(set(tasks)) != 33
            or sorted(canonical_task(task) for task in tasks) != inventory["frozen_task_ids"]):
        raise ValueError("v7 membership differs from the authorized original33")
    if INVENTORY["bare_sha"](manifest["frozen_sources"]["heldout.json"]["sha256"]) != INVENTORY["FROZEN_SHA"]:
        raise ValueError("v7 release does not bind the original frozen artifact")
    for reference in manifest["sources"]:
        bound_bytes(reference)
    verifier_ref = file_ref(verifier_receipt)
    source_parents = {row["task_id"]: row["source_package"]["sha256"] for row in inventory["rows"]
                      if row["population"] == "official"}
    graders: dict[str, cf.Grader] = {}
    for row in manifest["tasks"]:
        task = canonical_task(row["task_id"])
        if task in graders or task not in inventory["frozen_task_ids"]:
            raise ValueError("duplicate or unauthorized selected v7 task")
        if row["frozen_parent"]["package_digest"] != source_parents[task]:
            raise ValueError("v7 frozen parent differs from the native source package")
        target = row["evaluation_target"]
        package = Path(target["package_path"]).resolve(strict=True)
        actual_task, digest = INVENTORY["_package_identity"](package)
        if (actual_task != task or digest != target["package_digest"]
                or harbor_task_digest(package) != target["harbor_digest"]):
            raise ValueError("selected v7 package identity changed")
        lineage = target["lineage"]
        newest = lineage["records_newest_first"][0]
        record_bytes, record_ref = bound_bytes(newest["record"])
        from evallab.task_variants import VariantRecord

        record = VariantRecord.model_validate_json(record_bytes)
        if (record.variant_digest != digest or canonical_task(record.task_name) != task
                or record.variant_harbor_digest != target["harbor_digest"]
                or record.transform != newest["transform"] or newest["package_digest"] != digest
                or newest["harbor_digest"] != target["harbor_digest"]
                or INVENTORY["bare_sha"](record_ref["sha256"]) != INVENTORY["bare_sha"](lineage["record_sha256"])):
            raise ValueError("selected v7 grader record does not bind its package")
        graders[task] = cf.Grader(task, record.transform, digest, package, record_ref["path"])
    if set(graders) != set(inventory["frozen_task_ids"]):
        raise ValueError("v7 must select exactly one grader for each frozen task")
    cf.canonical_graders(list(graders.values()))
    return {
        "schema": "heldout-v7.admission/v1", "scoring_allowed": True,
        "manifest": manifest_ref, "verifier_receipt": verifier_ref,
        "operator_attested_verifier_accepted_sha256": manifest_ref["sha256"],
        "acceptance_basis": "Explicit independently accepted exact hash plus the producer's released PASS; receipt bytes retained, not natural-language parsed.",
        "graders": {task: grader.to_json() for task, grader in sorted(graders.items())},
    }, graders


class JournalGate(CloudSpendGate):
    """Reserve before dispatch; failures and process restarts never refund spend."""

    def __init__(self, path: Path, usd_per_execution: float) -> None:
        super().__init__(APPROVAL, CAP_USD, usd_per_execution)
        self.path = path
        self.context = threading.local()
        self.reserved_uids: set[tuple[str, str]] = set()
        self.reserved_keys: set[tuple[str, str, str | None]] = set()
        if path.exists():
            for line in path.read_text().splitlines():
                entry = json.loads(line)
                uid = tuple(entry["source_uid"])
                digest = entry.get("grader_digest")
                key = (*uid, digest)
                if (len(uid) != 2 or key in self.reserved_keys
                        or digest is not None and "sha256:" + INVENTORY["bare_sha"](digest) != digest
                        or entry["execution"] != self.executions + 1
                        or entry["usd_per_execution"] != self.usd_per_execution
                        or entry["ledger_tag"] != "2026-10-17-v7"
                        or entry["reserved_usd"] != round((self.executions + 1) * self.usd_per_execution, 8)):
                    raise ValueError("invalid cumulative reservation journal")
                self.reserved_uids.add(uid)
                self.reserved_keys.add(key)
                self.executions += 1
        if self.executions * self.usd_per_execution > self.cap_usd + 1e-9:
            raise ValueError("prior reservations exceed the lane cap")

    def is_reserved(self, uid: tuple[str, str], digest: str | None) -> bool:
        if digest is None:
            return uid in self.reserved_uids
        # Legacy source-only reservations are never silently reassigned or refunded.
        return (*uid, None) in self.reserved_keys or (*uid, digest) in self.reserved_keys

    def charge(self) -> None:
        uid = tuple(self.context.source_uid)
        digest = getattr(self.context, "grader_digest", None)
        if digest is not None:
            INVENTORY["bare_sha"](digest)
        with self._lock:
            reserved = (self.executions + 1) * self.usd_per_execution
            if reserved > self.cap_usd + 1e-9:
                raise SpendCapReached("the cumulative heldout-v7 reservation cap is exhausted")
            if self.is_reserved(uid, digest):
                raise SpendCapReached("source already reserved; no implicit retry or refund")
            entry = {
                "ledger_tag": "2026-10-17-v7", "source_uid": list(uid),
                "execution": self.executions + 1, "usd_per_execution": self.usd_per_execution,
                "reserved_usd": round(reserved, 8),
            }
            if digest is not None:
                entry["grader_digest"] = digest
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, sort_keys=True, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.reserved_uids.add(uid)
            self.reserved_keys.add((*uid, digest))
            self.executions += 1


def _retained_observation(path: Path) -> dict[str, Any]:
    reference = json.loads(path.with_name("observation-ref.json").read_bytes())
    if Path(reference["path"]).resolve() != path.resolve():
        raise ValueError("retained observation pointer changed")
    data, _ = bound_bytes(reference)
    observation = json.loads(data)
    for reference in observation["native_evidence"].get("files", {}).values():
        bound_bytes(reference)
    return observation


def execute(
    *, inventory_path: Path, manifest_path: Path, accepted_sha: str | None, verifier_receipt: Path | None,
    rate_evidence: Path, usd_per_execution: float, work: Path, output: Path,
    repo_root: Path, workers: int = 4, staged_plan_path: Path | None = None, reuse_work: Path | None = None,
    sandbox_timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run the complete bounded inventory. No source/graded row is silently omitted."""
    inventory_ref = file_ref(inventory_path)
    inventory = json.loads(inventory_path.read_bytes())
    for reference in inventory["sources"].values():
        bound_bytes(reference)
    reconstructed = INVENTORY["inventory"](Path(inventory["sources"]["configuration"]["path"]))
    if {**inventory, "created": None} != {**reconstructed, "created": None}:
        raise ValueError("inventory cells differ from their bound canonical/native source evidence")
    staged = runpy.run_path(str(HERE / "staged.py"))
    plan = None
    if staged_plan_path is not None:
        plan, graders = staged["load_plan"](staged_plan_path, inventory)
        if (Path(plan["sources"]["manifest"]["path"]).resolve() != manifest_path.resolve()
                or plan["budget"]["usd_per_execution"] != usd_per_execution
                or file_ref(rate_evidence) != bound_bytes(plan["sources"]["rate_evidence"])[1]):
            raise ValueError("staged execution changes its bound manifest or reservation basis")
        admission, targets = plan["admission"], plan["selected_packages"]
        sandbox_timeout_seconds = plan["budget"]["sandbox_timeout_seconds"]
    else:
        if accepted_sha is None or verifier_receipt is None:
            raise ValueError("canonical run requires an independently accepted exact manifest")
        admission, graders = admit(inventory, manifest_path, accepted_sha, verifier_receipt)
        targets = staged["canonical_bindings"](manifest_path)
    # Validate the entire raw map before any paid call, including all exclusions.
    REPORT["_validate"](inventory, [], admission)
    if workers < 1 or workers > 4:
        raise ValueError("workers must be between 1 and 4")
    if type(sandbox_timeout_seconds) is not int or sandbox_timeout_seconds < 1:
        raise ValueError("an explicit positive integer Sandbox TTL is required")
    rate_ref = file_ref(rate_evidence)
    work, output, repo_root = work.resolve(), output.resolve(), repo_root.resolve()
    if output.exists() or output.is_relative_to(work) or work.is_relative_to(output):
        raise ValueError("report output must be new and disjoint from campaign work")
    immutable = [Path(row["source_trial_dir"]).parent for row in inventory["rows"]]
    immutable.extend(Path(row["source_package"]["path"]) for row in inventory["rows"])
    immutable.extend(grader.package for grader in graders.values())
    for target in (work, output):
        if any(target.is_relative_to(path) or path.is_relative_to(target) for path in immutable):
            raise ValueError("campaign output overlaps immutable input evidence")
    try:
        replay_and_collect = runpy.run_path(str(HERE / "native.py"))["replay_and_collect"]
        from evallab import harbor_modal
        from evallab.schemas import ExperimentMatrix

        if (getattr(harbor_modal, "NATIVE_SINGLE_CREATE_SUPPORTED", False) is not True
                or "modal_sandbox_create_max_attempts" not in ExperimentMatrix.model_fields):
            raise ValueError("heldout regrades require the integrated typed single-create Modal runtime before reserving spend")
    except ImportError as exc:
        raise ValueError("install the pinned Harbor/Modal native runtime before v7 execution") from exc
    work.mkdir(parents=True, exist_ok=True)
    budget_root = Path(inventory["sources"]["configuration"]["path"]).resolve().parent
    with (budget_root / "classifiers-v7-ledger.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        header = {
            "schema": "heldout-v7.campaign/v1", "ledger_tag": "2026-10-17-v7",
            "inventory": inventory_ref, "admission": admission,
            "rate_evidence": rate_ref, "usd_per_execution": usd_per_execution, "cap_usd": CAP_USD,
            "modal_policy": {"app": APP, "resource_policy": "limit", "sandbox_timeout_seconds": sandbox_timeout_seconds,
                             "sandbox_create_max_attempts": 1},
            "measurement_mode": "staged" if plan is not None else "canonical",
            "canonical_scoring_allowed": plan is None, "citable": plan is None,
            "staged_plan": file_ref(staged_plan_path) if staged_plan_path is not None else None,
            "reuse_work": str(reuse_work.resolve()) if reuse_work is not None else None,
            "consumer_code": {name: file_ref(HERE / name) for name in ("regrade.py", "campaign.py", "native.py", "report.py", "staged.py")},
            "reservation_caveat": "Cumulative estimates, not settled provider bills; retain all failures and interrupted reservations.",
        }
        header_path = work / "campaign.json"
        if header_path.exists():
            if json.loads(header_path.read_bytes()) != header:
                raise ValueError("resume changes the admitted inventory, graders or budget basis")
        else:
            create_json(header_path, header)
        gate = JournalGate(budget_root / "classifiers-v7-reservations.jsonl", usd_per_execution)
        cases = work / "cases"
        cases.mkdir(exist_ok=True)
        def case_path(source: dict[str, Any]) -> Path:
            key = "\n".join([*source["source_uid"], targets[source["task_id"]]["package_digest"]])
            return cases / hashlib.sha256(key.encode()).hexdigest()

        cached: dict[tuple[str, str], dict[str, Any]] = (
            staged["retained_observations"](reuse_work.resolve(), inventory, targets) if reuse_work is not None else {}
        )
        for source in inventory["rows"]:
            uid = tuple(source["source_uid"])
            case = case_path(source)
            if (case / "observation.json").exists():
                retained = _retained_observation(case / "observation.json")
                if retained["source_uid"] != list(uid) or not staged["same_measurement"](source, retained, targets[source["task_id"]]):
                    raise ValueError("retained observation source identity changed")
                staged["qualify_retained"](source, retained, targets[source["task_id"]])
                cached[uid] = retained
        needed = sum(
            tuple(source["source_uid"]) not in cached and source["diff"] is not None
            and not (plan is not None and not targets[source["task_id"]]["changed_from_v6"])
            and not gate.is_reserved(tuple(source["source_uid"]), targets[source["task_id"]]["package_digest"])
            for source in inventory["rows"]
        )
        if (gate.executions + needed) * usd_per_execution > CAP_USD + 1e-9:
            raise SpendCapReached("the complete selected cohort does not fit the remaining cumulative $3 subcap; no new dispatch")

        def one(source: dict[str, Any]) -> dict[str, Any]:
            uid = tuple(source["source_uid"])
            target = targets[source["task_id"]]
            case = case_path(source)
            case.mkdir(exist_ok=True)
            saved = case / "observation.json"
            if uid in cached:
                observation = cached[uid]
                if not saved.exists():
                    reference = create_json(saved, observation)
                    create_json(case / "observation-ref.json", reference)
                return observation
            if plan is not None and not target["changed_from_v6"]:
                observation = staged["inherit"](source, target)
            elif gate.is_reserved(uid, target["package_digest"]):
                # Never silently spend again after a crash in the dispatch window.
                observation = {
                    "source_uid": list(uid), "v7_outcome": None,
                    "v7_error": "interrupted_after_reservation_requires_native_evidence_reconciliation",
                    "replayed": None, "native_evidence": {"files": {}, "case_dir": str(case)},
                }
            else:
                gate.context.source_uid = uid
                gate.context.grader_digest = target["package_digest"]

                def dispatch(matrix: Any, *, root: Path) -> Any:
                    configured = matrix.model_copy(update={
                        "modal_app_name": APP, "modal_resource_policy": "limit",
                        "modal_sandbox_timeout_seconds": sandbox_timeout_seconds,
                        "modal_sandbox_create_max_attempts": 1,
                    })
                    create_json(case / "dispatch.json", configured.model_dump(mode="json"))
                    return runner.run_matrix(configured, root=root)

                observation = replay_and_collect(
                    source, graders[source["task_id"]], repo_root=repo_root, work=case / "replay",
                    spend_gate=gate, matrix_runner=dispatch,
                )
                observation["measurement_origin"] = (
                    "new_native_model_free_regrade"
                    if (observation.get("native_evidence") or {}).get("dispatched") is True
                    else "not_dispatched"
                )
            observation["measurement_binding"] = staged["measurement_binding"](source, target)
            reference = create_json(saved, observation)
            create_json(case / "observation-ref.json", reference)
            return observation

        previous_executions = gate.executions
        with ThreadPoolExecutor(max_workers=workers) as pool:
            observations = list(pool.map(one, inventory["rows"]))
        report = REPORT["build_report"](inventory, observations, admission)
        report["campaign"] = {**header, "spend": gate.to_json(), "journal": file_ref(gate.path) if gate.path.exists() else None}
        report["campaign"]["this_invocation"] = {
            "new_reservations": gate.executions - previous_executions,
            "new_native_dispatches": sum(
                tuple(row["source_uid"]) not in cached and row["native_evidence"].get("dispatched") is True
                for row in observations
            ),
            "reused_observations": len(cached),
            "pricing_basis": "resource_bound_plus_build_buffer",
            "all_in_invoice_hard_bound_proven": False,
        }
        result = staged["publish"](report, output, staged=plan is not None)
        result["spend"] = gate.to_json()
        return result
