"""Explicit pre-release measurement, never a substitute for canonical admission.

Staged plans retain their exact historical $3 cap and approval on the cumulative
journal. Only canonical execution accepts a new heldout allocation from Trainer's
admitted cumulative parent envelope; neither path resets or refunds prior
reservations.
"""
from __future__ import annotations

import json
import math
import runpy
import tomllib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evallab.qa import counterfactual as cf
from evallab.registry import compute_task_digests, harbor_task_digest
from evallab.results import load_trial
from evallab.task_variants import VariantRecord

HERE = Path(__file__).resolve().parent
SOURCE = runpy.run_path(str(HERE / "regrade.py"))
file_ref = SOURCE["file_ref"]
bound_bytes = SOURCE["bound_bytes"]
canonical_task = SOURCE["canonical_task"]
REPLACEMENTS = {"000371": "eaefb3cc164b", "000783": "bb1da511153d", "000657": "302936515e85"}
REPAIRS = {"000143", "000149", "000371", "000657", "000783", "000878", "001425", "001686",
           "001948", "002371", "002451", "002915"}
FLAGS = {"measurement_mode": "staged", "release_status": "pre_release", "measurement_allowed": True,
         "scoring_allowed": False, "canonical_scoring_allowed": False, "citable": False}
STAGED_APPROVAL = "Peter-approved 2026-10-18 focused sprint; cumulative heldout regrade $3 inside Trainer $12 (native $6.50, serving $2.50); ledger 2026-10-17-v7"


def load_inventory(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    for reference in value["sources"].values():
        bound_bytes(reference)
    rebuilt = SOURCE["inventory"](Path(value["sources"]["configuration"]["path"]))
    if {**value, "created": None} != {**rebuilt, "created": None}:
        raise ValueError("inventory cells differ from their bound canonical/native source evidence")
    return value


def _json_ref(path: Path, expected: str | None = None) -> tuple[dict, dict]:
    reference = file_ref(path, expected)
    return json.loads(bound_bytes(reference)[0]), reference


def _control_custody(value: Any) -> dict[str, Any]:
    """Retain available control bytes and missing/stale declarations, not a verdict."""
    references: dict[str, dict] = {}
    gaps: list[dict] = []

    def capture(path: str, expected: str | None) -> None:
        if path in references:
            return
        try:
            actual = file_ref(Path(path))
        except OSError as exc:
            gaps.append({"path": path, "declared_sha256": expected, "reason": type(exc).__name__})
            return
        references[path] = actual
        if expected is not None and actual["sha256"] != "sha256:" + SOURCE["bare_sha"](expected):
            gaps.append({"path": path, "declared_sha256": expected, "actual_sha256": actual["sha256"],
                         "reason": "control_reference_changed_since_producer_declaration"})

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            if isinstance(node.get("path"), str) and isinstance(node.get("sha256"), str):
                capture(node["path"], node["sha256"])
            for key, child in node.items():
                if isinstance(child, str) and isinstance(node.get(key + "_sha256"), str):
                    capture(child, node[key + "_sha256"])
                walk(child)
            if isinstance(node.get("job_dir"), str):
                job = Path(node["job_dir"])
                paths = [job / name for name in ("result.json", "config.json", "lock.json", "lab-metadata.json")]
                paths.extend(job.glob("*/result.json"))
                paths.extend(job.glob("*/lock.json"))
                paths.extend(job.glob("*/verifier/*"))
                for path in paths:
                    if path.is_file():
                        capture(str(path), None)

    walk(value)
    return {"declarations": deepcopy(value), "files": dict(sorted(references.items())), "gaps": gaps,
            "scope": "Hash custody only; no control, correctness or canonical acceptance is inferred."}


def _target(task: str, declaration: dict, record_ref: dict, controls: dict) -> tuple[dict, cf.Grader]:
    package = Path(declaration["package_path"]).resolve(strict=True)
    actual_task, digest = SOURCE["_package_identity"](package)
    harbor = harbor_task_digest(package)
    verifier = compute_task_digests(package).verifier
    record_bytes, actual_record_ref = bound_bytes(record_ref)
    record_ref = {**record_ref, **actual_record_ref}
    record = VariantRecord.model_validate_json(record_bytes)
    if (actual_task != task or digest != declaration["package_digest"]
            or harbor != declaration["harbor_digest"]
            or declaration.get("verifier_digest", verifier) != verifier
            or canonical_task(record.task_name) != task or record.variant_digest != digest
            or record.variant_harbor_digest != harbor):
        raise ValueError("selected staged record does not bind its actual full package and Harbor identity")
    if any(path.is_symlink() for path in package.rglob("*")):
        raise ValueError("selected staged package contains symlinks")
    grader = cf.Grader(task, record.transform, digest, package, record_ref["path"])
    binding = {"task_id": task, "package_path": str(package), "package_digest": digest,
               "harbor_digest": harbor, "verifier_digest": verifier, "record": record_ref,
               "transform": record.transform, "controls": controls}
    return binding, grader


def prepare(
    *, inventory_path: Path, manifest_path: Path, repairs_path: Path, replacement_scope: Path,
    rate_evidence: Path, usd_per_execution: float, sandbox_timeout_seconds: int,
) -> dict[str, Any]:
    """Freeze actual sources for the original33 and exactly twelve selected repairs."""
    if type(sandbox_timeout_seconds) is not int or sandbox_timeout_seconds < 1:
        raise ValueError("staging requires an explicit positive integer Sandbox TTL")
    inventory = load_inventory(inventory_path)
    manifest, manifest_ref = _json_ref(manifest_path)
    repairs, repairs_ref = _json_ref(repairs_path)
    scope, scope_ref = _json_ref(replacement_scope)
    tasks = inventory["frozen_task_ids"]
    if (manifest.get("schema") != "mimo.heldout/v1" or manifest.get("successor_version") != "v7"
            or sorted(canonical_task(task) for task in manifest["heldout"]) != tasks
            or SOURCE["bare_sha"](manifest["frozen_sources"]["heldout.json"]["sha256"]) != SOURCE["FROZEN_SHA"]):
        raise ValueError("staged manifest must retain the exact original33 frozen binding")
    if repairs.get("schema") != "envcheck.repairs-v7/v1":
        raise ValueError("expected the actual repairs-v7 producer artifact")
    selected = {canonical_task(row["task_id"]): row for row in repairs["repairs"]}
    if len(selected) != len(repairs["repairs"]) or {task.rsplit("-", 1)[-1] for task in selected} != REPAIRS:
        raise ValueError("staging requires exactly the twelve original repaired endpoints")
    manifest_rows = {canonical_task(row["task_id"]): row for row in manifest["tasks"]}
    if len(manifest_rows) != len(manifest["tasks"]) or set(manifest_rows) != set(tasks):
        raise ValueError("staging requires all33 manifest rows, with no duplicates or replacements")
    candidates: dict[str, tuple[dict, dict]] = {}
    for row in scope["per_task"]:
        task = canonical_task(row["task_id"])
        suffix = task.rsplit("-", 1)[-1]
        if suffix not in REPLACEMENTS:
            continue
        if task in candidates:
            raise ValueError("duplicate staged replacement candidate")
        candidate, reference = _json_ref(Path(row["candidate"]), row["candidate_sha256"])
        if (candidate.get("schema") != "envcheck.v7.candidate/v1" or canonical_task(candidate["task_id"]) != task
                or candidate.get("heldout") is not True
                or candidate["package_digest"] != row["package_digest"]
                or not SOURCE["bare_sha"](candidate["package_digest"]).startswith(REPLACEMENTS[suffix])):
            raise ValueError("staged replacement must resolve its authorized prefix from actual full candidate bytes")
        candidates[task] = candidate, reference
    if {task.rsplit("-", 1)[-1] for task in candidates} != set(REPLACEMENTS):
        raise ValueError("all three explicitly authorized staged replacements are required")
    bindings, graders, caveats = {}, {}, []
    historical_controls = _control_custody(scope.get("historical_receipts_not_overwritten", []))
    for task in tasks:
        original = manifest_rows[task]["frozen_parent"]
        parent_digests = {row["source_package"]["sha256"] for row in inventory["rows"] if row["task_id"] == task}
        if parent_digests != {original["package_digest"]}:
            raise ValueError("staged frozen parent differs from retained native source packages")
        if task in selected:
            producer = selected[task]
            declaration, candidate_ref = candidates.get(task, (producer, None))
            if candidate_ref is None:
                candidate, candidate_ref = _json_ref(Path(producer["candidate"]), producer["candidate_sha256"])
                if candidate["package_digest"] != declaration["package_digest"] or canonical_task(candidate["task_id"]) != task:
                    raise ValueError("producer and candidate selected endpoints disagree")
            control_declaration = {"producer_source": repairs_ref, "candidate": candidate_ref,
                                   "producer_status": producer.get("status"),
                                   "producer_qualified_for_v7": producer.get("qualified_for_v7"),
                                   "replacement_pending": task in candidates}
            if task in candidates:
                control_declaration["historical_controls"] = historical_controls
                control_declaration["candidate_controls"] = declaration
            else:
                qualification, reference = _json_ref(Path(producer["qualification"]), producer["qualification_sha256"])
                if qualification["package_digest"] != declaration["package_digest"]:
                    raise ValueError("control qualification is for a different staged package")
                control_declaration.update(qualification=reference, qualification_declaration=qualification)
            controls = _control_custody(control_declaration)
            reference = file_ref(Path(declaration["native_record"]), declaration["native_record_sha256"])
            if task in candidates or producer.get("qualified_for_v7") is not True:
                caveats.append({"task_id": task, "reason": "selected_endpoint_not_canonically_qualified",
                                "producer_status": producer.get("status"), "replacement": task in candidates})
        else:
            declaration = original
            reference = original["lineage"]["records_newest_first"][0]["record"]
            controls = _control_custody({"manifest_source": manifest_ref, "native_controls": manifest_rows[task].get("native_controls")})
        binding, grader = _target(task, declaration, reference, controls)
        binding["changed_from_v6"] = task in selected
        if task in selected and binding["package_digest"] == original["package_digest"]:
            raise ValueError("a selected repaired endpoint is identical to its frozen grader")
        bindings[task], graders[task] = binding, grader
    cf.canonical_graders(list(graders.values()))
    if not math.isfinite(usd_per_execution) or usd_per_execution <= 0:
        raise ValueError("reservation rate must be finite and positive")
    campaign = runpy.run_path(str(HERE / "campaign.py"))
    journal = Path(inventory["sources"]["configuration"]["path"]).parent / "classifiers-v7-reservations.jsonl"
    gate = campaign["JournalGate"](journal, usd_per_execution, cap_usd=3.0, approval=STAGED_APPROVAL)
    changed_rows = [row for row in inventory["rows"] if bindings[row["task_id"]]["changed_from_v6"]]
    executions = sum(row["diff"] is not None for row in changed_rows)
    new_reservations = sum(row["diff"] is not None and not gate.is_reserved(tuple(row["source_uid"]), bindings[row["task_id"]]["package_digest"]) for row in changed_rows)
    inherited = [inherit(row, bindings[row["task_id"]]) for row in inventory["rows"]
                 if not bindings[row["task_id"]]["changed_from_v6"]]
    return {"schema": "heldout-v7.staged-plan/v1", **FLAGS,
            "sources": {"inventory": file_ref(inventory_path), "manifest": manifest_ref, "repairs": repairs_ref,
                        "replacement_scope": scope_ref, "rate_evidence": file_ref(rate_evidence)},
            "selected_packages": bindings,
            "admission": {"schema": "heldout-v7.staged-admission/v1", **FLAGS, "manifest": manifest_ref,
                          "frozen_task_ids": tasks, "graders": {task: grader.to_json() for task, grader in graders.items()}},
            "caveats": caveats,
            "inheritance_audit": {
                "scope": "Provider-free original reward and identical full-grader proof; never a new native measurement.",
                "unmeasured": [{"source_uid": row["source_uid"], "reason": row["v7_error"]}
                               for row in inherited if row["v7_outcome"] is None],
            },
            "counts": {"frozen_tasks": 33, "changed_graders": 12, "unchanged_graders": 21,
                       "all_native_sources": len(inventory["rows"]),
                       "all_retained_diffs": sum(row["diff"] is not None for row in inventory["rows"]),
                       "original_passes": sum(row["v6_outcome"] is True for row in inventory["rows"]),
                       "changed_task_native_sources": len(changed_rows), "native_execution_count": executions,
                       "new_cost_reservation_count": new_reservations,
                       "inherited_measured_source_rows": sum(row["v7_outcome"] is not None for row in inherited),
                       "unchanged_unmeasured_source_rows": sum(row["v7_outcome"] is None for row in inherited),
                       "unchanged_source_rows": len(inventory["rows"]) - len(changed_rows)},
            "budget": {"ledger_tag": "2026-10-17-v7", "cap_usd": 3.0, "combined_trainer_ceiling_usd": 12.0,
                       "allocation": {"native_generation_usd": 6.5, "shared_serving_usd": 2.5, "heldout_regrade_usd": 3.0},
                       "journal": str(journal), "lease": str(journal.with_name("classifiers-v7-ledger.lock")),
                       "usd_per_execution": usd_per_execution, "prior": gate.to_json(),
                       "sandbox_timeout_seconds": sandbox_timeout_seconds,
                       "new_reservation_bound_usd": round(new_reservations * usd_per_execution, 8),
                       "fits_remaining_subcap": (gate.executions + new_reservations) * usd_per_execution <= 3.0 + 1e-9,
                       "implicit_retries": False, "modal_sandbox_create_max_attempts": 1,
                       "pricing_basis": "resource_bound_plus_build_buffer",
                       "image_build_costs_proven_zero": False, "all_in_invoice_hard_bound_proven": False,
                       "uncertainty": "Image/registry/provider extras are not proven zero or hard bounded; the explicitly quoted unit includes an operator-allocated buffer, not an invoice guarantee.",
                       "caveat": "Reservations are cumulative estimates, not settled invoices or a tail-cost guarantee."}}


def write_plan(path: Path, **kwargs: Any) -> dict:
    """Snapshot actual bytes, so later producer publication cannot erase custody."""
    if path.exists():
        raise ValueError("staged plan output must be new")
    plan = prepare(**kwargs)
    snapshots = path.with_name(path.stem + ".sources")
    snapshots.mkdir(parents=True, exist_ok=False)

    def snapshot(reference: dict) -> dict:
        data, actual = bound_bytes(reference)
        destination = snapshots / (SOURCE["bare_sha"](actual["sha256"]) + Path(actual["path"]).suffix)
        if destination.exists():
            file_ref(destination, actual["sha256"])
        else:
            with destination.open("xb") as stream:
                stream.write(data)
        return {**file_ref(destination), "origin_path": actual["path"]}

    plan["sources"] = {name: snapshot(reference) for name, reference in plan["sources"].items()}
    plan["admission"]["manifest"] = plan["sources"]["manifest"]
    graders = {}
    for task, binding in plan["selected_packages"].items():
        binding["record"] = snapshot(binding["record"])
        binding["controls"]["files"] = {origin: snapshot(reference)
                                       for origin, reference in binding["controls"]["files"].items()}
        _, graders[task] = _target(task, binding, binding["record"], binding["controls"])
    cf.canonical_graders(list(graders.values()))
    plan["admission"]["graders"] = {task: grader.to_json() for task, grader in graders.items()}
    reference = SOURCE["create_json"](path, plan)
    return {"output": reference, "counts": plan["counts"], "budget": plan["budget"],
            "canonical_scoring_allowed": False, "citable": False}


def load_plan(path: Path, inventory: dict) -> tuple[dict, dict[str, cf.Grader]]:
    plan = json.loads(path.read_bytes())
    if plan.get("schema") != "heldout-v7.staged-plan/v1" or any(plan.get(k) != v for k, v in FLAGS.items()):
        raise ValueError("expected an explicit pre-release noncitable staged plan")
    if (plan["budget"].get("cap_usd") != 3.0 or plan["budget"]["prior"].get("cap_usd") != 3.0
            or plan["budget"]["prior"].get("approval") != STAGED_APPROVAL):
        raise ValueError("staged execution must retain its exact historical $3 cap and recorded approval")
    for reference in plan["sources"].values():
        bound_bytes(reference)
    if inventory != load_inventory(Path(plan["sources"]["inventory"]["path"])):
        raise ValueError("staged execution inventory differs from its plan")
    manifest = json.loads(bound_bytes(plan["sources"]["manifest"])[0])
    repairs = json.loads(bound_bytes(plan["sources"]["repairs"])[0])
    scope = json.loads(bound_bytes(plan["sources"]["replacement_scope"])[0])
    tasks = inventory["frozen_task_ids"]
    originals = {canonical_task(row["task_id"]): row["frozen_parent"] for row in manifest["tasks"]}
    repaired = {canonical_task(row["task_id"]): row for row in repairs["repairs"]}
    replacements = {canonical_task(row["task_id"]): row for row in scope["per_task"]
                    if canonical_task(row["task_id"]).rsplit("-", 1)[-1] in REPLACEMENTS}
    if (manifest.get("schema") != "mimo.heldout/v1" or manifest.get("successor_version") != "v7"
            or sorted(canonical_task(task) for task in manifest["heldout"]) != tasks
            or SOURCE["bare_sha"](manifest["frozen_sources"]["heldout.json"]["sha256"]) != SOURCE["FROZEN_SHA"]
            or set(originals) != set(tasks) or len(manifest["tasks"]) != 33
            or repairs.get("schema") != "envcheck.repairs-v7/v1" or len(repaired) != 12
            or len(repairs["repairs"]) != 12 or {task.rsplit("-", 1)[-1] for task in repaired} != REPAIRS
            or set(plan["selected_packages"]) != set(tasks)
            or {task.rsplit("-", 1)[-1] for task in replacements} != set(REPLACEMENTS)):
        raise ValueError("staged plan must preserve original33 and the exact authorized twelve repairs")
    graders = {}
    for task in tasks:
        binding = plan["selected_packages"][task]
        controls = binding["controls"]
        for reference in controls["files"].values():
            bound_bytes(reference)
        if task in repaired:
            producer = replacements.get(task, repaired[task])
            candidate_ref = controls["declarations"]["candidate"]
            retained = controls["files"][candidate_ref["path"]]
            if SOURCE["bare_sha"](retained["sha256"]) != SOURCE["bare_sha"](producer["candidate_sha256"]):
                raise ValueError("staged candidate differs from the actual selected producer bytes")
            candidate = json.loads(bound_bytes(retained)[0])
            declaration = candidate if task in replacements else repaired[task]
            if (canonical_task(candidate["task_id"]) != task or candidate["package_digest"] != producer["package_digest"]
                    or candidate["package_digest"] != declaration["package_digest"]
                    or SOURCE["bare_sha"](binding["record"]["sha256"]) != SOURCE["bare_sha"](declaration["native_record_sha256"])):
                raise ValueError("staged candidate, producer and immutable VariantRecord disagree")
            if task in replacements and not SOURCE["bare_sha"](candidate["package_digest"]).startswith(REPLACEMENTS[task.rsplit("-", 1)[-1]]):
                raise ValueError("staged candidate is not the explicitly authorized replacement endpoint")
        else:
            declaration = originals[task]
            expected = declaration["lineage"]["records_newest_first"][0]["record"]["sha256"]
            if SOURCE["bare_sha"](binding["record"]["sha256"]) != SOURCE["bare_sha"](expected):
                raise ValueError("unchanged endpoint record is not the actual frozen parent")
        actual, grader = _target(task, declaration, binding["record"], controls)
        actual["changed_from_v6"] = task in repaired
        if actual != binding:
            raise ValueError("staged plan selected package/Harbor/record/control binding changed")
        if {row["source_package"]["sha256"] for row in inventory["rows"] if row["task_id"] == task} != {originals[task]["package_digest"]}:
            raise ValueError("staged original parent differs from retained native sources")
        graders[task] = grader
    cf.canonical_graders(list(graders.values()))
    if (plan["admission"] != {"schema": "heldout-v7.staged-admission/v1", **FLAGS,
                             "manifest": plan["sources"]["manifest"], "frozen_task_ids": tasks,
                             "graders": {task: grader.to_json() for task, grader in graders.items()}}):
        raise ValueError("staged plan admission cannot open or replace the canonical gate")
    return plan, graders


def inherit(source: dict, binding: dict) -> dict:
    """Inherit only an identical full grader and an actually measured original reward."""
    evidence = {"schema": "heldout-v7.inherited/v1", "files": {}, "dispatched": False,
                "source_uid": source["source_uid"], "grader": deepcopy(binding),
                "origin": "original_v6_native_measurement_not_new_native_execution"}
    observation = {"source_uid": source["source_uid"], "v7_outcome": None, "v7_error": None,
                   "replayed": False, "measurement_origin": "inherited_identical_v6_grader", "native_evidence": evidence}
    try:
        old, error = SOURCE["canonical_outcome"]({"reward": int(source["v6_outcome"]) if type(source["v6_outcome"]) is bool else None,
                                                 "passed": source["v6_outcome"], "error": source["v6_error"]})
        if old is None:
            raise ValueError(error or "original_v6_unmeasured")
        package = Path(source["source_package"]["path"])
        if (source["task_id"] != binding["task_id"] or source["source_package"]["sha256"] != binding["package_digest"]
                or SOURCE["_package_identity"](package) != (source["task_id"], binding["package_digest"])
                or harbor_task_digest(package) != binding["harbor_digest"]
                or compute_task_digests(package).verifier != binding["verifier_digest"]):
            raise ValueError("original_and_selected_grader_not_identical")
        trial = Path(source["source_trial_dir"])
        if cf.source_digest(trial) != source["source_digest"]:
            raise ValueError("original_native_source_digest_changed")
        for group in source["native_artifacts"].values():
            for reference in group.values():
                if reference is not None:
                    _, actual = bound_bytes(reference)
                    evidence["files"][actual["path"]] = actual
        parsed = load_trial(trial)
        raw = (parsed.result.get("verifier_result") or {}).get("rewards", {}).get("reward")
        if type(raw) not in (int, float) or raw not in (0, 1) or raw != int(old):
            raise ValueError("original_native_reward_does_not_prove_canonical_outcome")
        result_ref = file_ref(trial / "result.json")
        evidence["files"][result_ref["path"]] = result_ref
        # Historical agent termination and an actually measured verifier reward
        # are distinct; canonical v6 measurement errors were refused above.
        evidence["native_exception_info"] = deepcopy(parsed.result.get("exception_info"))
        evidence.update(source_digest=source["source_digest"], canonical_source_ref=source["canonical_source_ref"],
                        measured_original_reward=raw, identical_package_digest=binding["package_digest"],
                        identical_harbor_digest=binding["harbor_digest"], identical_verifier_digest=binding["verifier_digest"])
        observation["v7_outcome"] = old
    except (OSError, ValueError, KeyError, TypeError) as exc:
        observation["v7_error"] = str(exc)
    return observation


def measurement_binding(source: dict, target: dict) -> dict:
    return {"source_uid": source["source_uid"], "source_digest": source["source_digest"],
            "diff": deepcopy(source["diff"]), "task_id": source["task_id"],
            "package_digest": target["package_digest"], "harbor_digest": target["harbor_digest"],
            "verifier_digest": target["verifier_digest"], "record": deepcopy(target["record"]),
            "controls": deepcopy(target["controls"])}


def same_measurement(source: dict, observation: dict, target: dict) -> bool:
    previous = observation.get("measurement_binding", {})
    current = measurement_binding(source, target)
    return all(previous.get(key) == current[key] for key in
               ("source_uid", "source_digest", "diff", "task_id", "package_digest", "harbor_digest", "verifier_digest"))


def canonical_bindings(manifest_path: Path) -> dict[str, dict]:
    """Called only after campaign.admit has independently admitted these exact bytes."""
    manifest, manifest_ref = _json_ref(manifest_path)
    return {canonical_task(row["task_id"]): {
        "task_id": canonical_task(row["task_id"]),
        **{key: row["evaluation_target"][key] for key in ("package_path", "package_digest", "harbor_digest")},
        "verifier_digest": compute_task_digests(Path(row["evaluation_target"]["package_path"])).verifier,
        "record": file_ref(Path(row["evaluation_target"]["lineage"]["records_newest_first"][0]["record"]["path"])),
        "controls": _control_custody({"independently_accepted_manifest": manifest_ref,
                                     "native_controls": row.get("native_controls"),
                                     "release_gate": "all33 independently accepted by campaign.admit"}),
    } for row in manifest["tasks"]}


def qualify_retained(source: dict, observation: dict, target: dict) -> None:
    """Recheck real native artifacts without dispatch, spending, or changing a grade."""
    binding = observation["measurement_binding"]
    bound_bytes(binding["record"])
    for reference in binding["controls"]["files"].values():
        bound_bytes(reference)
    if cf.source_digest(Path(source["source_trial_dir"])) != source["source_digest"]:
        raise ValueError("retained original native source changed")
    if source["diff"] is not None:
        bound_bytes(source["diff"])
    if observation["v7_outcome"] is None or observation["v7_error"] is not None:
        return  # Null/error evidence is never promoted into a fresh measurement.
    if observation.get("measurement_origin") == "inherited_identical_v6_grader":
        verified = inherit(source, {**target, "task_id": source["task_id"]})
        if verified["v7_outcome"] is not observation["v7_outcome"] or verified["v7_error"] is not None:
            raise ValueError("retained inheritance no longer proves identical measured original reward")
        return
    evidence = observation["native_evidence"]
    if evidence.get("schema") != "heldout-v7.native/v1" or evidence.get("dispatched") is not True or observation["replayed"] is not True:
        raise ValueError("measured staged grade lacks a real model-free native execution")
    native = runpy.run_path(str(HERE / "native.py"))
    row = json.loads(bound_bytes(evidence["cf_result"])[0])
    receipt = json.loads(bound_bytes(evidence["matrix_receipt"])[0])
    receipt_path = Path(evidence["matrix_receipt"]["path"])
    execution = SimpleNamespace(receipt_path=receipt_path, results=receipt["results"], exit_code=0,
                                invocation_path=Path(evidence["invocations"]["path"]))
    record = VariantRecord.model_validate_json(bound_bytes(binding["record"])[0])
    grader = cf.Grader(source["task_id"], record.transform, target["package_digest"],
                       Path(target["package_path"]), binding["record"]["path"])
    config = tomllib.loads((grader.package / "task.toml").read_text())
    diff = bound_bytes(source["diff"])[0].decode("utf-8", errors="strict")
    script = cf._patch_script(diff, config["environment"]["workdir"]).encode()
    measured = native["_qualify"](row, execution, evidence["submitted_matrix"], grader, receipt_path.parents[2],
                                  Path(evidence["cf_result"]["path"]).parent, script, deepcopy(evidence))
    if measured is not observation["v7_outcome"]:
        raise ValueError("retained native measurement does not match the actual native grade proof")


def retained_observations(work: Path, inventory: dict, targets: dict) -> dict[tuple[str, str], dict]:
    campaign = runpy.run_path(str(HERE / "campaign.py"))
    header = json.loads((work / "campaign.json").read_bytes())
    bound_bytes(header["inventory"])
    if json.loads(bound_bytes(header["inventory"])[0]) != inventory:
        raise ValueError("reuse work belongs to a different original native inventory")
    sources = {tuple(row["source_uid"]): row for row in inventory["rows"]}
    # A granted retry observation supersedes the original in the same case dir;
    # hash-pointer ``*-ref.json`` files are never observations themselves.
    by_case: dict[Path, Path] = {}
    for path in sorted((work / "cases").glob("*/observation*.json")):
        if path.name.endswith("-ref.json"):
            continue
        chosen = by_case.get(path.parent)
        if path.name == "observation.json":
            by_case.setdefault(path.parent, path)
        elif chosen is None or chosen.name == "observation.json":
            by_case[path.parent] = path
        else:
            raise ValueError("duplicate retained retry observation")
    observations = {}
    for path in sorted(by_case.values()):
        observation = campaign["_retained_observation"](path)
        uid = tuple(observation["source_uid"])
        if uid not in sources or uid in observations:
            raise ValueError("duplicate or foreign source in retained campaign")
        source = sources[uid]
        if same_measurement(source, observation, targets[source["task_id"]]):
            qualify_retained(source, observation, targets[source["task_id"]])
            # Keep the actual prior outcome/error and native proof, not a re-labelled measurement.
            observation = deepcopy(observation)
            observation["reuse"] = {"observation": file_ref(path), "campaign": file_ref(work / "campaign.json"),
                                    "accepted_controls": targets[source["task_id"]]["controls"],
                                    "new_native_executions": 0, "new_reservations": 0}
            observations[uid] = observation
    return observations


def report_staged(*, plan_path: Path, work: Path, output: Path) -> dict:
    plan = json.loads(plan_path.read_bytes())
    inventory = load_inventory(Path(plan["sources"]["inventory"]["path"]))
    plan, _ = load_plan(plan_path, inventory)
    observations = retained_observations(work, inventory, plan["selected_packages"])
    if len(observations) != len(inventory["rows"]):
        raise ValueError("staged report requires every original native source observation, including null/error rows")
    report = runpy.run_path(str(HERE / "report.py"))["build_report"](inventory, list(observations.values()), plan["admission"])
    return publish(report, output, staged=True)


def publish(report: dict, output: Path, *, staged: bool) -> dict:
    if output.suffix == ".json":
        labels_path, report_path = output, output.with_name(output.stem + ".report.json")
        if labels_path.exists() or report_path.exists():
            raise ValueError("report and labels outputs must both be new")
    else:
        output.mkdir(parents=True, exist_ok=False)
        labels_path = output / ("regrade-v7-staged.json" if staged else "regrade-v7.json")
        report_path = output / "report.json"
    report_ref = SOURCE["create_json"](report_path, report)
    labels = {"schema": "heldout-v7.pass-labels/v1", "scoring_allowed": not staged,
              "canonical_scoring_allowed": not staged, "citable": not staged,
              "measurement_mode": "staged" if staged else "canonical",
              "release_status": "pre_release" if staged else "independently_accepted",
              "report": report_ref, "admission": report["admission"],
              "population": "canonical original v6 passes; all-attempt custody retained in report",
              "labels": report["per_pass_labels"],
              "limits": ["wrong_pass is grading correctness, never behavioral shortcut, cheating or intent evidence"]}
    labels_ref = SOURCE["create_json"](labels_path, labels)
    return {"report": report_ref, "labels": labels_ref, "scoring_allowed": not staged,
            "canonical_scoring_allowed": not staged, "citable": not staged,
            "primary_cohort": report["primary"]["cohort"]}


def promote(*, inventory_path: Path, manifest_path: Path, accepted_sha: str, verifier_receipt: Path,
            work: Path, output: Path) -> dict:
    """No dispatch: reuse identical actual measurements only after all33 release gates."""
    campaign = runpy.run_path(str(HERE / "campaign.py"))
    inventory = load_inventory(inventory_path)
    admission, _ = campaign["admit"](inventory, manifest_path, accepted_sha, verifier_receipt)
    targets = canonical_bindings(manifest_path)
    observations = retained_observations(work, inventory, targets)
    if len(observations) != len(inventory["rows"]):
        missing = [row["source_uid"] for row in inventory["rows"] if tuple(row["source_uid"]) not in observations]
        raise ValueError(f"promotion lacks identical grader/diff/native custody for {len(missing)} sources; use canonical run --reuse-work for only changed graders within the same cumulative $3 journal")
    report = runpy.run_path(str(HERE / "report.py"))["build_report"](inventory, list(observations.values()), admission)
    report["promotion"] = {"source_campaign": file_ref(work / "campaign.json"), "new_native_executions": 0,
                           "new_reservations": 0, "unmeasured_and_errors_preserved": True}
    return publish(report, output, staged=False)
