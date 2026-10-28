"""Read-only OQ6 phase/unique-union coverage over frozen native evidence.

API: ``build_coverage(parent_protocol_path, continuation_protocol_paths=())``.
The optional science_loader seam is for synthetic fixtures; production loads the
original pinned paired, accounting, transport and same-route A/B helpers. No
model/scoring transport is created, and no input or receipt is ever written.

CLI: --parent-protocol PATH [--continuation-protocol PATH ...]
     [--private-output NEW_PATH] [--aggregate-only]
The default stdout document is private. --aggregate-only emits only aggregate
counts/metrics, without task/key identifiers, paths, judgments or response bodies.
--private-output additionally saves the complete private document, exclusively,
outside the evidence roots. A phase's quality denominator is its scheduled
subset; represented/prepared counts retain the full frozen cohort. The unique
union denominator is the original full cohort, including honest missing cells.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import types
from collections import Counter
from pathlib import Path

SNAPSHOT_SCHEMA = "oq6.prior-consumption-snapshot/v1"
PLAN_SCHEMA = "oq6.stage-continuation-plan/v1"
FREEZE_SCHEMA = "oq6.stage-continuation-freeze/v1"
SCHEDULER_SUFFIX = ("research", "experiments", "oq6-task-quality-gate-rerun", "runner.py")
REFERENCES = (
    "parent_protocol_path", "parent_protocol_sha256", "parent_plan_path",
    "parent_plan_sha256", "parent_root", "prior_snapshot_path",
    "prior_snapshot_sha256", "stage",
)
REQUEST_FIELDS = (
    "requested_route", "requested_model", "requested_provider", "requested_model_id",
    "prompt_sha256", "system_sha256", "config_sha256", "expected_array",
    "timeout_seconds", "contract_sha256", "attestation_source_sha256",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _checked_json(path, expected=None, *, sidecar=False):
    path = Path(path).resolve()
    raw = path.read_bytes()
    digest = _sha(raw)
    if sidecar:
        frozen = path.with_suffix(".sha256").read_text().strip()
        if not SHA256.fullmatch(frozen) or digest != frozen:
            raise ValueError("frozen protocol digest drift")
    if expected is not None and (not SHA256.fullmatch(expected) or digest != expected):
        raise ValueError("frozen JSON digest drift: " + str(path))
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("frozen JSON must be an object")
    return value, digest


def _absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("frozen evidence paths must be absolute")
    return path


def _worker_key(route, arm):
    if (not isinstance(route, str) or
            not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9][A-Za-z0-9._:/-]*", route) or
            any(part in {".", ".."} for part in route.split("/")) or arm not in ("A", "B")):
        raise ValueError("unsafe route/arm identity")
    return route.replace("/", "--") + "__" + arm


def _base(phase, route, arm, case):
    return phase["root"] / "calls" / route.replace("/", "--") / arm / case["stage"] / case["case_id"]


def _pinned_rows(protocol, field):
    result = {}
    for row in protocol.get(field, []):
        path = _absolute(row["path"]).resolve()
        if path in result or not SHA256.fullmatch(row["sha256"]):
            raise ValueError("duplicate or malformed frozen file pin")
        result[path] = row["sha256"]
    return result


def _read_phase(path, name):
    path = Path(path).resolve()
    protocol, digest = _checked_json(path, sidecar=True)
    execution = protocol["execution"]
    plan_path = _absolute(execution["plan_path"]).resolve()
    if not SHA256.fullmatch(execution["plan_sha256"]):
        raise ValueError("missing frozen plan digest")
    plan, plan_digest = _checked_json(plan_path, execution["plan_sha256"])
    models = protocol["models"]
    if not isinstance(models, list) or not models or len(set(models)) != len(models):
        raise ValueError("frozen model census must be distinct")
    routes = execution.get("route_order", models)
    if not routes or len(set(routes)) != len(routes) or any(route not in models for route in routes):
        raise ValueError("invalid frozen route order")
    for route in models:
        _worker_key(route, "A")
    cases = {}
    for case in plan["cases"]:
        cid = case["case_id"]
        if (not isinstance(cid, str) or not SAFE_ID.fullmatch(cid) or cid in {".", ".."} or
                cid in cases or case["stage"] not in ("dev", "cross") or
                type(case["key_count"]) is not int or case["key_count"] < 0):
            raise ValueError("duplicate or invalid frozen case metadata")
        cases[cid] = case
    workers = {}
    continuation = plan.get("continuation")
    if continuation is not None and continuation.get("schema") != PLAN_SCHEMA:
        raise ValueError("unknown continuation plan schema")
    for worker in plan["workers"]:
        key = _worker_key(worker["route"], worker["arm"])
        if key in workers or worker["route"] not in routes:
            raise ValueError("duplicate or unscheduled frozen worker")
        remaining = worker["case_ids"]
        prior = worker.get("prior_case_ids", [])
        resume = worker.get("resume_scorer_case_ids", [])
        for ids in (remaining, prior, resume):
            if not isinstance(ids, list) or len(set(ids)) != len(ids) or any(cid not in cases for cid in ids):
                raise ValueError("duplicate or unknown worker case")
        if (set(remaining) & set(prior) or set(remaining) | set(prior) != set(cases) or
                not set(resume) <= set(remaining)):
            raise ValueError("worker remaining/prior cases must be a disjoint exact partition")
        if continuation is None and (prior or resume):
            raise ValueError("ordinary phase must schedule every case")
        workers[key] = worker
    if set(workers) != {_worker_key(route, arm) for route in routes for arm in ("A", "B")}:
        raise ValueError("every scheduled route must retain both arms")
    for field in ("code_files", "pre_cross_data_files"):
        for source, expected in _pinned_rows(protocol, field).items():
            if _file_sha(source) != expected:
                raise ValueError("frozen source/input digest drift: " + str(source))
    if ("config" in protocol and
            _file_sha(_absolute(protocol["config"]["path"])) != protocol["config"]["sha256"]):
        raise ValueError("frozen invocation config digest drift")
    return {
        "phase": name, "protocol_path": path, "protocol_sha256": digest,
        "protocol": protocol, "plan_path": plan_path, "plan_sha256": plan_digest,
        "plan": plan, "root": _absolute(execution["raw_root"]).resolve(),
        "cases": cases, "workers": workers, "routes": routes, "snapshot": None,
    }


def _validate_continuation(parent, phase):
    continuation = phase["plan"].get("continuation")
    freeze = phase["protocol"].get("continuation")
    if not continuation or not freeze or freeze.get("schema") != FREEZE_SCHEMA:
        raise ValueError("continuation requires frozen protocol and plan custody")
    if any(freeze.get(field) != continuation.get(field) for field in REFERENCES):
        raise ValueError("continuation protocol/plan references differ")
    expected = {
        "parent_protocol_path": str(parent["protocol_path"]),
        "parent_protocol_sha256": parent["protocol_sha256"],
        "parent_plan_path": str(parent["plan_path"]),
        "parent_plan_sha256": parent["plan_sha256"], "parent_root": str(parent["root"]),
    }
    if any(continuation.get(field) != value for field, value in expected.items()):
        raise ValueError("continuation does not bind the supplied original phase")
    if len(phase["routes"]) != 1 or phase["root"] == parent["root"]:
        raise ValueError("continuation must have its own route-local plan and new raw root")
    stage = continuation.get("stage")
    if stage not in ("dev", "cross"):
        raise ValueError("continuation must name one original stage")
    cohort = {cid: case for cid, case in parent["cases"].items() if case["stage"] == stage}
    if phase["cases"] != cohort:
        raise ValueError("continuation must retain exactly the original stage metadata/cohort")
    original, current = parent["protocol"], phase["protocol"]
    for field in ("models", "config", "runtime", "fixed_scorer", "gates", "DEV", "cross", "arms"):
        if original.get(field) != current.get(field):
            raise ValueError("continuation changed frozen science: " + field)
    for field in ("science_root", "disabled_routes"):
        if original["execution"].get(field) != current["execution"].get(field):
            raise ValueError("continuation changed original execution science: " + field)
    for field in ("code_files", "pre_cross_data_files"):
        new_pins = _pinned_rows(current, field)
        for source, digest in _pinned_rows(original, field).items():
            if field == "code_files" and source.parts[-4:] == SCHEDULER_SUFFIX:
                continue
            if new_pins.get(source) != digest:
                raise ValueError("continuation removed/changed an immutable original pin")
    snapshot_path = _absolute(continuation["prior_snapshot_path"]).resolve()
    snapshot, digest = _checked_json(snapshot_path, continuation["prior_snapshot_sha256"])
    if snapshot.get("schema") != SNAPSHOT_SCHEMA or any(
        snapshot.get(field) != expected[field]
        for field in ("parent_root", "parent_protocol_sha256", "parent_plan_sha256")
    ) or snapshot.get("stage") != stage:
        raise ValueError("prior snapshot parent identity drift")
    phase.update(snapshot=snapshot, snapshot_path=snapshot_path, snapshot_sha256=digest, stage=stage)


def _module(path, name, expected_sha256):
    source = Path(path).read_bytes()
    if _sha(source) != expected_sha256:
        raise ValueError("frozen helper source digest drift: " + str(path))
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def load_science(protocol):
    """Reuse the frozen parser environment and helpers without bytecode writes."""
    pins = _pinned_rows(protocol, "code_files")
    schedulers = [path for path in pins if path.parts[-4:] == SCHEDULER_SUFFIX]
    if len(schedulers) != 1:
        raise ValueError("exactly one original scheduler source must be pinned")
    runner = _module(schedulers[0], "oq6_coverage_runner", pins[schedulers[0]])
    science = runner.load_science(protocol)
    root = Path(protocol["execution"]["science_root"]).resolve()
    accounting_path = root / "aggregate.py"
    summaries = [path for path in pins if path.name == "result_summary.py"]
    if accounting_path not in pins or len(summaries) != 1:
        raise ValueError("original accounting and same-route summary helpers must be pinned")
    previous = {name: sys.modules.get(name) for name in runner.HELPERS}
    try:
        for name in runner.HELPERS:
            sys.modules[name] = getattr(science, name)
        accounting = _module(accounting_path, "oq6_coverage_accounting", pins[accounting_path]).accounting
        ab_report = _module(summaries[0], "oq6_coverage_summary", pins[summaries[0]]).paired_ab_report
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
    contract_path = root / "transport-contract.json"
    data_pins = _pinned_rows(protocol, "pre_cross_data_files")
    if contract_path not in data_pins:
        raise ValueError("original native contract must be pinned")
    contract, contract_sha = _checked_json(contract_path, data_pins[contract_path])
    attestation = _absolute(contract["session_attestation"]).resolve()
    if attestation not in pins:
        raise ValueError("original native attestation must be pinned")
    return types.SimpleNamespace(
        paired=science.paired, transport=science.transport,
        accounting=accounting, paired_ab_report=ab_report,
        scorer_route=science.scorer.SCORER_ROUTE,
        request_pins={"contract_sha256": contract_sha, "attestation_source_sha256": pins[attestation]},
    )


def _consumed(directory, transport):
    return ((directory / "receipt.json").exists() or
            any((directory / name).exists() for name in transport.OWNED_ARTIFACTS))


def _owned_files(directory):
    """The snapshot pins all native files, not only consumption sentinel names."""
    result = {}
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            raise ValueError("native artifact files cannot alias another evidence slot")
        if item.is_file() and item != directory / "receipt.json":
            resolved = item.resolve()
            if not resolved.is_relative_to(directory.resolve()):
                raise ValueError("owned native artifact escaped its canonical directory")
            result[str(resolved)] = _file_sha(item)
    return result


def _optional_json(path):
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("native record must be an object")
        return value, None
    except (ValueError, OSError) as error:
        return None, type(error).__name__


def _inspect_attempt(directory, route, arm, role, case, protocol, science):
    transport = science.transport
    marker_path, receipt_path = directory / "call-consumed.json", directory / "receipt.json"
    marker, marker_error = _optional_json(marker_path)
    stored, receipt_error = _optional_json(receipt_path)
    expected_route = route if role == "audit" else science.scorer_route
    carrier = marker or stored or {}
    request = {field: carrier[field] for field in REQUEST_FIELDS if field in carrier}
    errors = []
    for source in (marker, stored):
        if source is not None and source.get("requested_route", expected_route) != expected_route:
            errors.append("frozen_request_identity_drift")
    if marker_error or receipt_error:
        errors.append("unreadable_native_record")
    if marker is None or any(field not in request for field in REQUEST_FIELDS):
        errors.append("incomplete_consumption_marker")
    for field in REQUEST_FIELDS:
        if marker is not None and stored is not None and marker.get(field) != stored.get(field):
            errors.append("marker_receipt_request_custody_drift")
    if marker is not None and (type(marker.get("attempt_limit")) is not int or marker["attempt_limit"] != 1):
        errors.append("invalid_consumption_limit")
    timeout = request.get("timeout_seconds")
    if type(timeout) is not int or not 1 <= timeout <= 600:
        errors.append("invalid_consumed_request_timeout")
    expected = {
        "requested_model": expected_route, "requested_provider": expected_route.split("/", 1)[0],
        "requested_model_id": expected_route.split("/", 1)[1],
        "expected_array": "findings" if role == "audit" else "judgments",
        **science.request_pins,
    }
    if "config" in protocol:
        expected["config_sha256"] = protocol["config"]["sha256"]
    if role == "audit":
        if "auditor_system_sha256" in protocol:
            expected["system_sha256"] = protocol["auditor_system_sha256"]
        prompt_sha = case.get("prompts", {}).get(arm, {}).get("sha256")
        if prompt_sha is not None:
            expected["prompt_sha256"] = prompt_sha
    if any(request.get(field, value) != value for field, value in expected.items()):
        errors.append("frozen_native_request_pin_drift")
    for filename, field in (("prompt.txt", "prompt_sha256"), ("system.txt", "system_sha256"),
                            ("controlled-subscription.yml", "config_sha256")):
        path = directory / filename
        if path.is_file() and field in request and _file_sha(path) != request[field]:
            errors.append("request_byte_custody_drift")
    marker_sha = _file_sha(marker_path) if marker_path.is_file() else None
    if stored is not None:
        if (stored.get("attempt_consumed") is not True or
                type(stored.get("attempt_limit")) is not int or stored["attempt_limit"] != 1):
            errors.append("malformed_consumed_receipt")
        if stored.get("call_consumed_sha256") != marker_sha:
            errors.append("consumption_marker_hash_drift")
        if stored.get("call_consumed_path") != str(marker_path):
            errors.append("consumption_marker_path_drift")
        changes = stored.get("model_change_events")
        invalid_changes = not isinstance(changes, list) or any(
            not isinstance(event, dict) or "identity" not in event or
            event["identity"] is not None and event["identity"] != expected_route or
            event.get("resolvedModelIsFallback") is not None and event["resolvedModelIsFallback"] is not False
            for event in changes
        )
        if stored.get("status") == "answered" and (
                stored.get("receipt_path") != str(receipt_path) or
                stored.get("response_path") != str(directory / "response.txt") or
                invalid_changes or any(stored.get(field) for field in (
                    "errors", "fallback_events", "memory_events", "tool_events",
                    "retry_events", "context_violations", "model_fallback",
                ))):
            errors.append("answered_receipt_custody_drift")
    if not errors and stored is not None:
        evidence = transport._existing(directory, request)
    else:
        evidence = transport._abandoned(
            directory, {"requested_route": expected_route, **request},
            "coverage_consumed_without_valid_receipt" if errors else "coverage_consumed_orphan",
        )
    valid = stored is not None and not errors and evidence.get("artifacts_are_snapshot") is not True
    if evidence.get("attempt_consumed") is not True:
        raise ValueError("read-only recovery lost actual native consumption")
    return {
        "receipt": evidence, "complete_native_receipt": valid,
        "receipt_error": receipt_error, "marker_error": marker_error,
        "custody_errors": errors,
        "receipt_sha256": _file_sha(receipt_path) if receipt_path.is_file() else None,
        "consumption_sha256": marker_sha,
        "request": request, "owned_files": _owned_files(directory),
    }


def _reconcile(phases, science):
    attempts, canonical, slots = [], {}, {}
    for phase in phases:
        routes = phase["protocol"]["models"] if phase["snapshot"] is None else phase["routes"]
        for route in routes:
            for arm in ("A", "B"):
                worker = phase["workers"].get(_worker_key(route, arm))
                for case in phase["cases"].values():
                    cid = case["case_id"]
                    for role in ("audit", "scorer"):
                        directory = _base(phase, route, arm, case) / role
                        if not _consumed(directory, science.transport):
                            continue
                        slot = (route, arm, case["stage"], cid, role)
                        identity = directory.resolve()
                        if identity in canonical:
                            previous = canonical[identity]
                            allowed = (
                                role == "audit" and phase["snapshot"] is not None and
                                previous["slot"] == slot and previous["origin_phase"] == phases[0]["phase"] and
                                cid in worker["resume_scorer_case_ids"] and directory.is_symlink()
                            )
                            if not allowed:
                                raise ValueError("duplicate native consumption/canonical directory")
                            previous["aliases"].append({"phase": phase["phase"], "directory": str(directory)})
                            continue
                        if slot in slots:
                            raise ValueError("duplicate consumed role in a second native directory")
                        if phase["snapshot"] is not None:
                            if cid not in worker["case_ids"]:
                                raise ValueError("native consumption outside remaining continuation subset")
                            if role == "audit" and cid in worker["resume_scorer_case_ids"]:
                                raise ValueError("scorer resumption must reuse the canonical original audit")
                            if directory.is_symlink():
                                raise ValueError("new continuation consumption cannot borrow another native role")
                        inspected = _inspect_attempt(directory, route, arm, role, case, phase["protocol"], science)
                        attempt = {
                            "slot": slot, "auditor_route": route, "arm": arm, "stage": case["stage"],
                            "case_id": cid, "role": role, "origin_phase": phase["phase"],
                            "directory": str(identity), "aliases": [], **inspected,
                        }
                        attempts.append(attempt)
                        canonical[identity] = slots[slot] = attempt
    return attempts, slots


def _prediction_valid(prediction, case, route, arm, audit):
    if prediction is None or audit is None:
        return False
    expected = {
        "schema": "oq6.frozen-prediction/v1", "case_id": case["case_id"],
        "route": route, "arm": arm, "audit_status": "answered",
        "source_sha256": case.get("source_sha256"),
        "audit_receipt_sha256": audit["receipt_sha256"],
        "response_sha256": audit["receipt"].get("response_sha256"), "parse_error": None,
    }
    payloads = prediction.get("parsed_payloads")
    return (
        all(key in prediction and prediction[key] == value for key, value in expected.items()) and
        isinstance(payloads, list) and type(prediction.get("raw_findings_count")) is int and
        prediction["raw_findings_count"] == len(payloads)
    )


def _verify_snapshot(parent, phase, slots, science, *, pins_only=False):
    transport = science.transport
    snapshot = phase["snapshot"]
    for key, worker in phase["workers"].items():
        prior = snapshot["workers"].get(key)
        if prior is None or (prior.get("route"), prior.get("arm")) != (worker["route"], worker["arm"]):
            raise ValueError("snapshot worker identity drift")
        if set(prior["cases"]) != set(phase["cases"]):
            raise ValueError("snapshot must represent every original-stage case per worker")
        actions = {}
        for cid, case in phase["cases"].items():
            record = prior["cases"][cid]
            actions[cid] = record["action"]
            origin = _base(parent, worker["route"], worker["arm"], case)
            if origin.is_symlink() or _base(phase, worker["route"], worker["arm"], case).is_symlink():
                raise ValueError("entire cell directories cannot be reused through symlinks")
            for role in ("audit", "scorer"):
                pinned = record[role]
                directory = origin / role
                if _absolute(pinned["directory"]).resolve() != directory.resolve():
                    raise ValueError("snapshot native directory identity drift")
                actual_consumed = _consumed(directory, transport)
                if type(pinned["consumed"]) is not bool or pinned["consumed"] != actual_consumed:
                    raise ValueError("snapshot consumption changed")
                receipt = directory / "receipt.json"
                expected_path = str(receipt) if receipt.is_file() else None
                expected_sha = _file_sha(receipt) if receipt.is_file() else None
                if (pinned["receipt_path"], pinned["receipt_sha256"]) != (expected_path, expected_sha):
                    raise ValueError("snapshot receipt custody drift")
                stored, _ = _optional_json(receipt)
                status = stored.get("status") if stored is not None and isinstance(stored.get("status"), str) else None
                if pinned.get("status") != status:
                    raise ValueError("snapshot stored receipt status drift")
                owned = {}
                for item in pinned["owned_files"]:
                    path = _absolute(item["path"]).resolve()
                    if str(path) in owned or not path.is_relative_to(directory.resolve()):
                        raise ValueError("duplicate/escaped snapshot artifact")
                    owned[str(path)] = item["sha256"]
                if owned != _owned_files(directory):
                    raise ValueError("snapshot owned artifact custody drift")
            for field in ("cell", "prediction"):
                path = origin / (field + ".json")
                expected = (str(path), _file_sha(path)) if path.is_file() else (None, None)
                if (record[field + "_path"], record[field + "_sha256"]) != expected:
                    raise ValueError("snapshot " + field + " custody drift")
            if pins_only:
                continue
            audit = slots.get((worker["route"], worker["arm"], case["stage"], cid, "audit"))
            scorer = slots.get((worker["route"], worker["arm"], case["stage"], cid, "scorer"))
            # Only origin evidence can decide whether a role was already consumed.
            audit = audit if audit is not None and audit["origin_phase"] == parent["phase"] else None
            scorer = scorer if scorer is not None and scorer["origin_phase"] == parent["phase"] else None
            finalized = (origin / "cell.json").is_file()
            prediction_path = origin / "prediction.json"
            prediction, _ = _optional_json(prediction_path)
            valid_prediction = _prediction_valid(prediction, case, worker["route"], worker["arm"], audit)
            if (finalized or scorer is not None or
                    (prediction_path.exists() and not valid_prediction) or
                    (audit is not None and (audit["receipt"].get("status") != "answered" or
                                           case.get("preparation_status") != "prepared"))):
                action = "prior_terminal"
            elif case.get("preparation_status") != "prepared" and audit is None:
                action = "unavailable_input"
            elif audit is not None:
                action = "resume_scorer"
            else:
                action = "audit"
            if record["action"] != action:
                raise ValueError("snapshot action conflicts with consumed/finalized role custody")
            if action == "resume_scorer":
                alias = _base(phase, worker["route"], worker["arm"], case) / "audit"
                if not alias.is_symlink() or alias.resolve() != (origin / "audit").resolve():
                    raise ValueError("resume scorer audit must be an original-directory symlink")
                old_prediction = origin / "prediction.json"
                new_prediction = alias.parent / "prediction.json"
                if old_prediction.is_file() and (not new_prediction.is_file() or _file_sha(old_prediction) != _file_sha(new_prediction)):
                    raise ValueError("resumed prediction must preserve exact original bytes")
        if set(worker["prior_case_ids"]) != {cid for cid, action in actions.items() if action in {"prior_terminal", "unavailable_input"}}:
            raise ValueError("continuation prior subset does not match terminal snapshot actions")
        if set(worker["resume_scorer_case_ids"]) != {cid for cid, action in actions.items() if action == "resume_scorer"}:
            raise ValueError("continuation resume subset does not match snapshot actions")


def _view(phase, route, arm, case, slots, science):
    directory = _base(phase, route, arm, case)
    final = directory / "cell.json"
    outcome = phase["root"] / "outcomes" / _worker_key(route, arm) / (case["case_id"] + ".json")
    path = final if final.is_file() else outcome if outcome.is_file() else None
    record = None
    if path is not None:
        record, digest = _checked_json(path)
        if ((record.get("route"), record.get("arm"), record.get("case_id"), record.get("stage")) !=
                (route, arm, case["case_id"], case["stage"]) or
                record.get("freeze_sha256") != phase["protocol_sha256"]):
            raise ValueError("quality outcome frozen identity drift")
        if path == outcome and record.get("plan_sha256") != phase["plan_sha256"]:
            raise ValueError("scheduler outcome frozen plan drift")
        row = record["paired"]
        if (row.get("case_id"), row.get("key_count")) != (case["case_id"], case["key_count"]) or type(row.get("complete")) is not bool:
            raise ValueError("quality row changed frozen denominator/identity")
        if path == outcome and (row["complete"] or record.get("status") == "complete"):
            raise ValueError("complete quality requires its final scientific cell")
    else:
        digest = None
        row = science.paired.evaluate_cell(case, None, None, None, audit_status="unfinalized_at_boundary")
    roles = {}
    for role in ("audit", "scorer"):
        attempt = slots.get((route, arm, case["stage"], case["case_id"], role))
        # Earlier phase quality cannot borrow a future native attempt.
        if attempt is not None and (attempt["origin_phase"] == phase["phase"] or
                any(alias["phase"] == phase["phase"] for alias in attempt["aliases"])):
            roles[role] = attempt
    prediction, prediction_error = _optional_json(directory / "prediction.json")
    parse = None
    audit = roles.get("audit")
    prediction_invalid = prediction_error is not None or (
        prediction is not None and not _prediction_valid(prediction, case, route, arm, audit)
    )
    if prediction is not None and not prediction_invalid:
        parse = science.paired.evaluate_cell(
            case, prediction.get("parsed_payloads"), None, None,
            audit_status=prediction.get("audit_status", "unavailable"),
            raw_findings_count=prediction.get("raw_findings_count"),
        )
    consumed = bool(roles)
    terminal = final.is_file() or "scorer" in roles or (
        "audit" in roles and roles["audit"]["receipt"].get("status") != "answered"
    ) or prediction_invalid or (parse is not None and parse.get("parse_status") == "invalid")
    if row["complete"] and prediction_invalid:
        raise ValueError("complete quality has invalid frozen prediction custody")
    if row["complete"] and (not roles.get("audit") or roles["audit"]["receipt"].get("status") != "answered"):
        raise ValueError("complete quality lacks its answered native auditor")
    if row["complete"] and row.get("scorer_required") is True and (
            not roles.get("scorer") or roles["scorer"]["receipt"].get("status") != "answered"):
        raise ValueError("complete nonempty union lacks its answered native scorer")
    required = row.get("scorer_required")
    if required is None and parse is not None:
        required = parse.get("scorer_required")
    if row["complete"]:
        classification = "fully_union_scored"
    elif terminal and consumed:
        classification = "terminal_consumed_failure"
    elif case.get("preparation_status") != "prepared":
        classification = "unavailable_input"
    elif terminal:
        classification = "terminal_unconsumed_missing"
    elif "audit" not in roles:
        classification = "auditor_unsent_backlog"
    elif required is True:
        classification = "scorer_only_unsent_backlog"
    elif required is False:
        classification = "answered_unfinalized_no_scorer_required"
    else:
        classification = "answered_unfinalized_scorer_requirement_unknown"
    return {
        "case_id": case["case_id"], "row": row, "phase": phase["phase"],
        "quality_origin_phase": phase["phase"],
        "quality_path": str(path) if path else None, "quality_sha256": digest,
        "final_cell": final.is_file(), "terminal": terminal,
        "classification": classification, "scorer_required": required,
        "roles": {role: attempt["directory"] for role, attempt in roles.items()},
    }


def _group(phase_name, route, arm, stage, represented, selected, views, attempts, science, disabled):
    quality_rows = [view["row"] for view in views]
    counts = Counter(view["classification"] for view in views)
    native = [attempt for attempt in attempts if attempt["auditor_route"] == route and
              attempt["arm"] == arm and attempt["stage"] == stage and
              (phase_name == "unique_union" or attempt["origin_phase"] == phase_name)]
    audits = [attempt["receipt"] for attempt in native if attempt["role"] == "audit"]
    scorers = [attempt["receipt"] for attempt in native if attempt["role"] == "scorer"]
    return {
        "phase": phase_name, "auditor_route": route, "arm": arm, "stage": stage,
        "active": not disabled, "disabled_reason": disabled,
        "represented_tasks": len(represented),
        "prepared_tasks": sum(case.get("preparation_status") == "prepared" for case in represented),
        "unavailable_input_tasks": sum(case.get("preparation_status") != "prepared" for case in represented),
        "scheduled_tasks": len(selected), "prior_tasks_excluded_from_phase_quality": len(represented) - len(selected),
        "quality_denominator": "original_full_cohort" if phase_name == "unique_union" else "phase_scheduled_subset",
        "auditor_dispatched": len(audits), "auditor_answered": sum(item.get("status") == "answered" for item in audits),
        "scorer_dispatched": len(scorers), "scorer_answered": sum(item.get("status") == "answered" for item in scorers),
        "fully_union_scored": counts["fully_union_scored"],
        "terminal_consumed_failures": counts["terminal_consumed_failure"],
        "auditor_unsent_backlog": counts["auditor_unsent_backlog"] if not disabled else 0,
        "scorer_only_unsent_backlog": counts["scorer_only_unsent_backlog"] if not disabled else 0,
        "answered_unfinalized_scorer_requirement_unknown": counts["answered_unfinalized_scorer_requirement_unknown"] if not disabled else 0,
        "answered_unfinalized_no_scorer_required": counts["answered_unfinalized_no_scorer_required"] if not disabled else 0,
        "terminal_unconsumed_missing": counts["terminal_unconsumed_missing"],
        "disabled_missing_tasks": sum(not row["complete"] for row in quality_rows) if disabled else 0,
        "classification_counts": dict(counts),
        "scorer_requirement_known_required": sum(view["scorer_required"] is True for view in views),
        "scorer_requirement_known_not_required": sum(view["scorer_required"] is False for view in views),
        "scorer_requirement_unknown": sum(view["scorer_required"] is None for view in views),
        "quality": science.paired.aggregate_cells(quality_rows),
        "audit_accounting": science.accounting(audits), "scorer_accounting": science.accounting(scorers),
        "combined_accounting": science.accounting(audits + scorers),
        "gate_tokens_shared_not_additive": True, "slots": views,
    }


def build_coverage(parent_protocol_path, continuation_protocol_paths=(), *, science_loader=None):
    """Return complete private evidence; all filesystem operations are read-only.

    Injected science_loader(protocol) must supply paired.evaluate_cell and
    paired.aggregate_cells, transport.OWNED_ARTIFACTS/_existing/_abandoned,
    accounting(receipts), paired_ab_report(left, right, aggregate), scorer_route
    (the frozen scorer constant), and request_pins (contract/attestation hashes).
    """
    parent = _read_phase(parent_protocol_path, "phase4")
    if parent["plan"].get("continuation") is not None:
        raise ValueError("first protocol must be the immutable original phase")
    phases = [parent]
    seen_route_stages = set()
    roots_by_stage = {}
    for index, path in enumerate(continuation_protocol_paths, 1):
        phase = _read_phase(path, "phase5-" + str(index))
        _validate_continuation(parent, phase)
        identity = (phase["routes"][0], phase["stage"])
        if identity in seen_route_stages:
            raise ValueError("duplicate continuation route/stage protocol")
        seen_route_stages.add(identity)
        if phase["stage"] in roots_by_stage and roots_by_stage[phase["stage"]] != phase["root"]:
            raise ValueError("route-local continuation protocols must share one root per stage")
        if any(stage != phase["stage"] and root == phase["root"] for stage, root in roots_by_stage.items()):
            raise ValueError("DEV and original-cross continuation roots must be separate")
        roots_by_stage[phase["stage"]] = phase["root"]
        phases.append(phase)
    science = (science_loader or load_science)(parent["protocol"])
    for phase in phases[1:]:
        _verify_snapshot(parent, phase, None, science, pins_only=True)
    attempts, slots = _reconcile(phases, science)
    for phase in phases[1:]:
        _verify_snapshot(parent, phase, slots, science)
        # No phase may acquire another stage or any new cohort/key intake.
        for route in phase["routes"]:
            for arm in ("A", "B"):
                for case in parent["cases"].values():
                    if case["stage"] != phase["stage"]:
                        directory = _base(phase, route, arm, case)
                        if (directory / "cell.json").exists() or any(
                            _consumed(directory / role, science.transport) for role in ("audit", "scorer")
                        ):
                            raise ValueError("continuation contains evidence outside its frozen stage")
    disabled_routes = parent["protocol"]["execution"].get("disabled_routes", {})
    disabled = {
        route: (disabled_routes.get(route) or "disabled/not scheduled in original phase")
        if route in disabled_routes or route not in parent["routes"] else None
        for route in parent["protocol"]["models"]
    }
    groups, by_group, union = [], {}, {}
    for phase in phases:
        routes = parent["protocol"]["models"] if phase is parent else phase["routes"]
        for route in routes:
            for arm in ("A", "B"):
                worker = phase["workers"].get(_worker_key(route, arm))
                for stage in ("dev", "cross") if phase is parent else (phase["stage"],):
                    represented = [case for case in phase["cases"].values() if case["stage"] == stage]
                    selected = [case for case in represented if phase is parent or case["case_id"] in worker["case_ids"]]
                    views = []
                    for case in represented:
                        slot = (route, arm, stage, case["case_id"])
                        view = _view(phase, route, arm, case, slots, science)
                        if phase is not parent and case["case_id"] not in worker["case_ids"]:
                            if view["quality_path"] is not None:
                                raise ValueError("continuation copied/finalized an excluded prior cell")
                            continue
                        if slot in union and union[slot]["terminal"]:
                            raise ValueError("continuation cannot overwrite terminal prior quality/consumption")
                        authoritative = view
                        if slot in union and view["quality_path"] is None:
                            previous = union[slot]
                            authoritative = {
                                **view, "row": previous["row"],
                                "quality_path": previous["quality_path"],
                                "quality_sha256": previous["quality_sha256"],
                                "quality_origin_phase": previous["quality_origin_phase"],
                            }
                        union[slot] = authoritative
                        views.append(view)
                    group = _group(phase["phase"], route, arm, stage, represented, selected, views, attempts, science, disabled[route])
                    groups.append(group)
                    by_group[phase["phase"], route, arm, stage] = group
    union_groups = []
    for route in parent["protocol"]["models"]:
        for arm in ("A", "B"):
            for stage in ("dev", "cross"):
                represented = [case for case in parent["cases"].values() if case["stage"] == stage]
                views = [union[route, arm, stage, case["case_id"]] for case in represented]
                group = _group("unique_union", route, arm, stage, represented, represented, views, attempts, science, disabled[route])
                union_groups.append(group)
                by_group["unique_union", route, arm, stage] = group
    comparisons = []
    for name, route, arm, stage in by_group:
        if arm != "A":
            continue
        left = by_group[name, route, "A", stage]
        right = by_group[name, route, "B", stage]
        paired = science.paired_ab_report(
            [view["row"] for view in left["slots"]], [view["row"] for view in right["slots"]],
            science.paired.aggregate_cells,
        )
        incomplete = paired["shared_complete_tasks"] != left["represented_tasks"] or paired["shared_complete_tasks"] != right["represented_tasks"]
        comparisons.append({
            "phase": name, "auditor_route": route, "stage": stage, "active": left["active"],
            **paired, "not_full_original_stage_coverage": incomplete,
            **({"not_full_ALL_DEV_coverage": incomplete} if stage == "dev" else {}),
        })
    active_dev = [group for group in union_groups if group["stage"] == "dev" and group["active"]]
    all_dev = [group for group in union_groups if group["stage"] == "dev"]
    return {
        "schema": "oq6.stage-continuation-coverage/v1",
        "phases": [{
            "phase": phase["phase"], "protocol_path": str(phase["protocol_path"]),
            "protocol_sha256": phase["protocol_sha256"], "plan_path": str(phase["plan_path"]),
            "science_root": str(Path(phase["protocol"]["execution"]["science_root"]).resolve()),
            "plan_sha256": phase["plan_sha256"], "root": str(phase["root"]),
            "stage": phase.get("stage"),
            "snapshot_path": str(phase["snapshot_path"]) if phase["snapshot"] is not None else None,
            "snapshot_sha256": phase.get("snapshot_sha256"),
        } for phase in phases],
        "phase_groups": groups, "unique_union_groups": union_groups,
        "same_route_AB_shared_complete": comparisons, "native_attempts": attempts,
        "combined_accounting": science.accounting([attempt["receipt"] for attempt in attempts]),
        "active_DEV_auditor_coverage_complete": bool(active_dev) and all(
            group["auditor_dispatched"] == group["prepared_tasks"] == group["represented_tasks"] for group in active_dev
        ),
        "active_DEV_all_quality_complete": bool(active_dev) and all(group["quality"]["full_cohort_complete"] for group in active_dev),
        "all_DEV_complete_including_disabled_catalog": bool(all_dev) and all(group["quality"]["full_cohort_complete"] for group in all_dev),
        "acceptance_verdict": None,
        "disabled_catalog_completeness_is_not_acceptance": True,
        "unkeyed_tasks_are_not_certified_negatives": True,
        "true_population_false_positive_rate": None, "repair_qualification_rate": None,
        "new_grader_executions": 0, "paid_spend_usd": 0, "subscription_usd": None,
    }


def aggregate_summary(report):
    """Publish only known aggregate fields, never private extension fields."""
    quality_fields = (
        "planned_tasks", "complete_tasks", "planned_keys", "evaluable_keys",
        "task_coverage", "key_coverage", "full_cohort_complete", "parsed_tasks",
        "unparsed_tasks", "observed_parsed_findings", "full_cohort_parsed_findings",
        "gate_added_verified", "gate_added_unverified",
        "true_population_false_positive_rate",
    )
    gate_fields = (
        "eligible_findings", "matched_keys", "key_match_claims", "source_contradictions",
        "plausible_unkeyed", "unclear", "evaluable_tasks", "evaluable_keys",
        "observed_recall", "full_cohort_recall", "key_backed_precision",
        "full_cohort_key_backed_precision", "source_contradiction_proportion",
        "full_cohort_source_contradiction_proportion",
        "true_population_false_positive_rate",
    )
    group_fields = (
        "phase", "auditor_route", "arm", "stage", "active", "represented_tasks",
        "prepared_tasks", "unavailable_input_tasks", "scheduled_tasks",
        "prior_tasks_excluded_from_phase_quality", "quality_denominator",
        "auditor_dispatched", "auditor_answered", "scorer_dispatched", "scorer_answered",
        "fully_union_scored", "terminal_consumed_failures", "auditor_unsent_backlog",
        "scorer_only_unsent_backlog", "disabled_missing_tasks",
        "answered_unfinalized_scorer_requirement_unknown",
        "answered_unfinalized_no_scorer_required", "terminal_unconsumed_missing",
        "scorer_requirement_known_required", "scorer_requirement_known_not_required",
        "scorer_requirement_unknown",
    )

    def quality(value):
        if value is None:
            return None
        result = {key: value[key] for key in quality_fields if key in value}
        result["gates"] = {
            gate: {key: stats[key] for key in gate_fields if key in stats}
            for gate, stats in value.get("gates", {}).items()
            if gate in {"old_0_8", "new_verified_0_5"}
        }
        return result

    def group(value):
        return {**{key: value[key] for key in group_fields}, "quality": quality(value["quality"])}

    accounting_fields = (
        "transport_attempts", "answered_responses", "paid_spend_usd", "subscription_usd",
        "subscription_usd_applicability", "native_cost_estimates_are_not_bills",
        "input_plus_output_tokens", "native_wall_seconds_sum",
        *(prefix + suffix for prefix in ("input", "output", "reasoning")
          for suffix in ("_reported_sum", "_missing_usage_attempts", "_tokens")),
    )
    native_accounting = report["combined_accounting"]

    result = {key: report[key] for key in (
        "active_DEV_auditor_coverage_complete",
        "active_DEV_all_quality_complete", "all_DEV_complete_including_disabled_catalog",
        "acceptance_verdict", "disabled_catalog_completeness_is_not_acceptance",
        "unkeyed_tasks_are_not_certified_negatives", "true_population_false_positive_rate",
        "repair_qualification_rate", "new_grader_executions", "paid_spend_usd", "subscription_usd",
    )}
    result["combined_accounting"] = {
        key: native_accounting[key] for key in accounting_fields if key in native_accounting
    }
    if "status_counts" in native_accounting:
        result["combined_accounting"]["status_counts"] = {
            key: value for key, value in native_accounting["status_counts"].items()
            if key in {"answered", "unanswered", "quarantined_identity"}
        }
    result["schema"] = "oq6.stage-continuation-coverage-summary/v1"
    result["phase_groups"] = [group(value) for value in report["phase_groups"] if value["active"]]
    result["unique_union_groups"] = [group(value) for value in report["unique_union_groups"] if value["active"]]
    result["disabled_strata"] = [
        {key: value[key] for key in (
            "auditor_route", "arm", "stage", "represented_tasks", "prepared_tasks",
            "unavailable_input_tasks", "auditor_dispatched", "fully_union_scored", "disabled_missing_tasks",
        )} for value in report["unique_union_groups"] if not value["active"]
    ]
    result["same_route_AB_shared_complete"] = [
        {**{key: value[key] for key in (
            "phase", "auditor_route", "stage", "status", "shared_complete_tasks", "shared_evaluable_keys",
            "not_full_original_stage_coverage",
        )}, "A": quality(value["A"]), "B": quality(value["B"])}
        for value in report["same_route_AB_shared_complete"] if value["active"]
    ]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-protocol", type=Path, required=True)
    parser.add_argument("--continuation-protocol", type=Path, action="append", default=[])
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--aggregate-only", action="store_true")
    args = parser.parse_args(argv)
    report = build_coverage(args.parent_protocol, args.continuation_protocol)
    private = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if args.private_output is not None:
        destination = args.private_output.resolve()
        if any(destination.is_relative_to(Path(phase[field]))
               for phase in report["phases"] for field in ("root", "science_root")):
            raise ValueError("private report output must be outside immutable evidence roots")
        with destination.open("x") as stream:
            stream.write(private)
    print(json.dumps(aggregate_summary(report), sort_keys=True, indent=2, allow_nan=False)
          if args.aggregate_only else private, end="\n" if args.aggregate_only else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
