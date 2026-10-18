"""Execute only an explicitly Verifier-accepted v7 release.

Canonical execution also requires an admitted parent envelope within Trainer's
combined $12 approval. Its heldout cap continues the same cumulative journal:
raising that allocation never resets charges or authorizes retries. Staged runs
retain their frozen historical $3 cap and approval, without a new envelope.
Financial admission never releases a scientific HOLD.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import runpy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
APP = "mimo-clean-census-v7-classifiers-20261017"


def _money(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be a finite nonnegative monetary amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite nonnegative monetary amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite nonnegative monetary amount")
    return amount


def checked_budget(
    path: Path, *, journal: Path, usd_per_execution: float, now: datetime | None = None,
) -> tuple[dict, dict]:
    """Read a hash-bound parent allocation without dispatching or changing its journal."""
    reference = file_ref(path)
    envelope = json.loads(bound_bytes(reference)[0])
    if (not isinstance(envelope, dict) or envelope.get("schema") != "mimo.phase2-envelope/v1"
            or envelope.get("admitted") is not True):
        raise ValueError("an admitted mimo.phase2-envelope/v1 parent budget is required")
    if not isinstance(envelope.get("approval"), str) or not envelope["approval"].strip():
        raise ValueError("the parent budget requires a nonempty approval")
    if envelope.get("ledger_tag") != "2026-10-17-v7":
        raise ValueError("the parent budget must retain ledger 2026-10-17-v7")
    if not isinstance(envelope.get("expires_at"), str):
        raise ValueError("the parent budget requires a timezone-aware expiry")
    expires = datetime.fromisoformat(envelope["expires_at"])
    instant = now if now is not None else datetime.now(UTC)
    if (expires.tzinfo is None or expires.utcoffset() is None
            or instant.tzinfo is None or instant.utcoffset() is None or expires <= instant):
        raise ValueError("the parent budget must be unexpired with a timezone-aware clock and expiry")

    approved = _money(envelope.get("approved_usd"), "approved_usd")
    limit = _money(envelope.get("require_envelope_sum_at_most_usd"), "require_envelope_sum_at_most_usd")
    if not 0 < approved <= 12 or not 0 < limit <= approved:
        raise ValueError("the aggregate parent approval and envelope ceiling must be positive and within the actual $12 approval")
    for name in ("serving", "native", "heldout_regrade"):
        if not isinstance(envelope.get(name), dict) or "cap_usd" not in envelope[name]:
            raise ValueError(f"the parent budget requires a {name} cap")
    caps = [_money(section["cap_usd"], f"{name}.cap_usd") for name, section in envelope.items()
            if isinstance(section, dict) and "cap_usd" in section]
    total = sum(caps, Decimal(0)) + _money(envelope.get("training_reservation_usd", 0), "training_reservation_usd")
    if total > approved or total > limit:
        raise ValueError("aggregate envelope caps plus training reservation exceed the parent approval or envelope ceiling")

    heldout = envelope["heldout_regrade"]
    unit = _money(usd_per_execution, "execution quote")
    if (_money(heldout["cap_usd"], "heldout_regrade.cap_usd") <= 0 or unit <= 0
            or _money(heldout.get("usd_per_execution"), "heldout_regrade.usd_per_execution") != unit):
        raise ValueError("the heldout cap must be positive and its unit must exactly match the positive execution quote")
    if (heldout.get("reset_authorized") is not False
            or heldout.get("canonical_reuses_exact_measurements_only") is not True):
        raise ValueError("the heldout budget must forbid journal resets and require exact measurement reuse")
    try:
        expected = journal.expanduser().resolve(strict=True)
        admitted_journal = Path(heldout["durable_journal_path"]).expanduser().resolve(strict=True)
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("the heldout budget must bind the existing cumulative journal") from exc
    if not expected.is_file() or admitted_journal != expected:
        raise ValueError("the heldout budget binds a different cumulative journal")

    sources = envelope.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("parent budget sources must be named file references")
    for source in sources.values():
        if (not isinstance(source, dict) or not isinstance(source.get("path"), str)
                or not isinstance(source.get("sha256"), str)):
            raise ValueError("each parent budget source requires a path and SHA-256")
        bound_bytes(source)
    return envelope, reference


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

    def __init__(
        self, path: Path, usd_per_execution: float, *, cap_usd: float, approval: str,
    ) -> None:
        if not isinstance(approval, str):
            raise ValueError("a cumulative reservation journal requires its recorded approval")
        cap, unit = _money(cap_usd, "cap_usd"), _money(usd_per_execution, "usd_per_execution")
        super().__init__(approval, float(cap), float(unit))
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
                        or _money(entry["usd_per_execution"], "journal usd_per_execution") != unit
                        or entry["ledger_tag"] != "2026-10-17-v7"
                        or _money(entry["reserved_usd"], "journal reserved_usd") != Decimal(str(round((self.executions + 1) * self.usd_per_execution, 8)))):
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
    sandbox_timeout_seconds: int = 300, budget_envelope: Path | None = None,
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
    budget_root = Path(inventory["sources"]["configuration"]["path"]).resolve().parent
    journal = budget_root / "classifiers-v7-reservations.jsonl"
    budget_ref = None
    if plan is None:
        if budget_envelope is None:
            raise ValueError("canonical run requires an admitted parent budget envelope")
        envelope, budget_ref = checked_budget(budget_envelope, journal=journal, usd_per_execution=usd_per_execution)
        cap_usd = float(_money(envelope["heldout_regrade"]["cap_usd"], "heldout_regrade.cap_usd"))
        approval = envelope["approval"]
    else:
        cap_usd, approval = plan["budget"]["cap_usd"], plan["budget"]["prior"]["approval"]
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
    with (budget_root / "classifiers-v7-ledger.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        header = {
            "schema": "heldout-v7.campaign/v1", "ledger_tag": "2026-10-17-v7",
            "inventory": inventory_ref, "admission": admission,
            "rate_evidence": rate_ref, "usd_per_execution": usd_per_execution, "cap_usd": cap_usd,
            "budget_envelope": budget_ref,
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
        gate = JournalGate(journal, usd_per_execution, cap_usd=cap_usd, approval=approval)
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
        if (gate.executions + needed) * usd_per_execution > cap_usd + 1e-9:
            raise SpendCapReached("the complete selected cohort does not fit the remaining admitted cumulative heldout cap; no new dispatch")

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
