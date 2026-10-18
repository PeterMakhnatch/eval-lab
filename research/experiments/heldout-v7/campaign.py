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
APPROVAL = "Peter-approved held-out v7 kept-diff replay lane, 2026-10-17 18:15 ET; ledger 2026-10-17-v7"


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
        if path.exists():
            for line in path.read_text().splitlines():
                entry = json.loads(line)
                uid = tuple(entry["source_uid"])
                if (len(uid) != 2 or uid in self.reserved_uids
                        or entry["execution"] != self.executions + 1
                        or entry["usd_per_execution"] != self.usd_per_execution
                        or entry["ledger_tag"] != "2026-10-17-v7"
                        or entry["reserved_usd"] != round((self.executions + 1) * self.usd_per_execution, 8)):
                    raise ValueError("invalid cumulative reservation journal")
                self.reserved_uids.add(uid)
                self.executions += 1
        if self.executions * self.usd_per_execution > self.cap_usd + 1e-9:
            raise ValueError("prior reservations exceed the lane cap")

    def charge(self) -> None:
        uid = tuple(self.context.source_uid)
        with self._lock:
            reserved = (self.executions + 1) * self.usd_per_execution
            if reserved > self.cap_usd + 1e-9:
                raise SpendCapReached("the cumulative heldout-v7 reservation cap is exhausted")
            if uid in self.reserved_uids:
                raise SpendCapReached("source already reserved; no implicit retry or refund")
            entry = {
                "ledger_tag": "2026-10-17-v7", "source_uid": list(uid),
                "execution": self.executions + 1, "usd_per_execution": self.usd_per_execution,
                "reserved_usd": round(reserved, 8),
            }
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, sort_keys=True, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.reserved_uids.add(uid)
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
    *, inventory_path: Path, manifest_path: Path, accepted_sha: str, verifier_receipt: Path,
    rate_evidence: Path, usd_per_execution: float, work: Path, output: Path,
    repo_root: Path, workers: int = 4,
) -> dict[str, Any]:
    """Run the complete bounded inventory. No source/graded row is silently omitted."""
    inventory_ref = file_ref(inventory_path)
    inventory = json.loads(inventory_path.read_bytes())
    for reference in inventory["sources"].values():
        bound_bytes(reference)
    reconstructed = INVENTORY["inventory"](Path(inventory["sources"]["configuration"]["path"]))
    if {**inventory, "created": None} != {**reconstructed, "created": None}:
        raise ValueError("inventory cells differ from their bound canonical/native source evidence")
    admission, graders = admit(inventory, manifest_path, accepted_sha, verifier_receipt)
    # Validate the entire raw map before any paid call, including all exclusions.
    REPORT["_validate"](inventory, [], admission)
    if workers < 1 or workers > 4:
        raise ValueError("workers must be between 1 and 4")
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
            "modal_policy": {"app": APP, "resource_policy": "limit", "sandbox_timeout_seconds": 300},
            "consumer_code": {name: file_ref(HERE / name) for name in ("regrade.py", "campaign.py", "native.py", "report.py")},
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
        cached: dict[tuple[str, str], dict[str, Any]] = {}
        for source in inventory["rows"]:
            uid = tuple(source["source_uid"])
            case = cases / hashlib.sha256((uid[0] + "\n" + uid[1]).encode()).hexdigest()
            if (case / "observation.json").exists():
                retained = _retained_observation(case / "observation.json")
                if retained["source_uid"] != list(uid):
                    raise ValueError("retained observation source identity changed")
                cached[uid] = retained

        def one(source: dict[str, Any]) -> dict[str, Any]:
            uid = tuple(source["source_uid"])
            case = cases / hashlib.sha256((uid[0] + "\n" + uid[1]).encode()).hexdigest()
            case.mkdir(exist_ok=True)
            saved = case / "observation.json"
            if uid in cached:
                return cached[uid]
            if uid in gate.reserved_uids:
                # Never silently spend again after a crash in the dispatch window.
                observation = {
                    "source_uid": list(uid), "v7_outcome": None,
                    "v7_error": "interrupted_after_reservation_requires_native_evidence_reconciliation",
                    "replayed": None, "native_evidence": {"files": {}, "case_dir": str(case)},
                }
            else:
                gate.context.source_uid = uid

                def dispatch(matrix: Any, *, root: Path) -> Any:
                    configured = matrix.model_copy(update={
                        "modal_app_name": APP, "modal_resource_policy": "limit",
                        "modal_sandbox_timeout_seconds": 300,
                    })
                    create_json(case / "dispatch.json", configured.model_dump(mode="json"))
                    return runner.run_matrix(configured, root=root)

                observation = replay_and_collect(
                    source, graders[source["task_id"]], repo_root=repo_root, work=case / "replay",
                    spend_gate=gate, matrix_runner=dispatch,
                )
            reference = create_json(saved, observation)
            create_json(case / "observation-ref.json", reference)
            return observation

        with ThreadPoolExecutor(max_workers=workers) as pool:
            observations = list(pool.map(one, inventory["rows"]))
        report = REPORT["build_report"](inventory, observations, admission)
        report["campaign"] = {**header, "spend": gate.to_json(), "journal": file_ref(gate.path) if gate.path.exists() else None}
        output.mkdir(parents=True, exist_ok=False)
        report_ref = create_json(output / "report.json", report)
        label_ref = create_json(output / "regrade-v7.json", {
            "schema": "heldout-v7.pass-labels/v1", "scoring_allowed": True,
            "report": report_ref, "admission": admission,
            "population": "canonical original v6 passes; official estimates exclude other populations",
            "labels": report["per_pass_labels"],
            "limits": ["wrong_pass is a grading-correctness label, not behavioral shortcut or intent evidence"],
        })
        return {"report": report_ref, "labels": label_ref, "spend": gate.to_json(),
                "primary_cohort": report["primary"]["cohort"], "scoring_allowed": True}
