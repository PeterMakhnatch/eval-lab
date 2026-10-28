"""Prepare, but never dispatch, a disjoint single-stage OQ-6 continuation.

API: prepare_continuation(parent_protocol_path=..., output_root=...,
checkpoint_at="...Z", stage="dev") returns the immutable preparation manifest. An optional
prior_snapshot_path binds an already captured snapshot after checking it against
closed-parent metadata. capture_prior_snapshot(parent_protocol_path, now=...)
returns that exact metadata-only snapshot without creating files.

CLI: python continue_dev.py --parent-protocol PATH --output-root NEW_PATH
    --checkpoint-at UTC_TIMESTAMP [--stage dev|cross] [--prior-snapshot PATH]

The parent cutoff and every initialized parent worker must be finished first.
The explicit future checkpoint bounds this preparation, not the ALL-DEV mission.
Only Sonnet/Kimi slots from the selected original stage are prepared; consumed
cross roles are never retried. Neither quality labels nor outcome metrics select
work. No scientific helper, provider, command, or model is run. A shared optional
provider_lock_root coordinates provider locks across separate stage roots.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import math
import os
import stat
import time
import types
from pathlib import Path, PurePosixPath

ACTIVE_ROUTES = ("anthropic/claude-sonnet-5-5", "kimi-code/kimi-for-coding")
DEV_CASES = 399
CROSS_CASES = 15
SNAPSHOT_SCHEMA = "oq6.prior-consumption-snapshot/v1"
PLAN_SCHEMA = "oq6.stage-continuation-plan/v1"
FREEZE_SCHEMA = "oq6.stage-continuation-freeze/v1"
REQUEST_FIELDS = (
    "requested_route", "requested_model", "requested_provider", "requested_model_id",
    "prompt_sha256", "system_sha256", "config_sha256", "expected_array",
    "timeout_seconds", "contract_sha256", "attestation_source_sha256",
)
HERE = Path(__file__).resolve().parent


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate metadata key: " + key)
        value[key] = item
    return value


def _constant(value):
    raise ValueError("non-finite metadata number: " + value)


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)


def _object(path):
    value = _json(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object: " + str(path))
    return value


def _metadata(path):
    try:
        return _object(path)
    except (ValueError, UnicodeError):
        return None


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _present(path):
    return Path(path).exists() or Path(path).is_symlink()


def _absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("frozen paths must be absolute: " + str(path))
    return path


def _pin(path, expected=None):
    path = _absolute(path)
    if not path.is_file():
        raise ValueError("frozen file is unavailable: " + str(path))
    digest = _digest(path)
    if expected is not None and digest != expected:
        raise ValueError("frozen file bytes drifted: " + str(path))
    return {"path": str(path), "sha256": digest, "bytes": path.stat().st_size}


def _write_new(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _load_runner(path=None):
    path = _absolute(Path(path) if path is not None else HERE / "runner.py")
    module = types.ModuleType("oq6_continuation_scheduler")
    module.__file__ = str(path)
    # Execute stdlib-only scheduler source without making an evidence __pycache__.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def _transport_profile(protocol):
    """Read the frozen transport's literal metadata, without importing helpers."""
    path = Path(protocol["execution"]["science_root"]) / "transport.py"
    rows = [row for row in protocol["code_files"] if Path(row["path"]).resolve() == path.resolve()]
    if len(rows) != 1:
        raise ValueError("the immutable transport must have exactly one source pin")
    raw = path.read_bytes()
    if _sha(raw) != rows[0]["sha256"]:
        raise ValueError("frozen transport source drifted")
    literals = {}
    for node in ast.parse(raw, filename=str(path)).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"SCHEMA", "OWNED_ARTIFACTS"}:
                    literals[target.id] = ast.literal_eval(node.value)
    names = literals.get("OWNED_ARTIFACTS")
    if (not isinstance(literals.get("SCHEMA"), str) or
            not isinstance(names, tuple) or not names or
            any(not isinstance(name, str) or Path(name).name != name for name in names) or
            "call-consumed.json" not in names):
        raise ValueError("frozen transport consumption metadata is malformed")
    contract_path = path.parent / "transport-contract.json"
    pins = [row for row in protocol["pre_cross_data_files"] if Path(row["path"]).resolve() == contract_path.resolve()]
    if len(pins) != 1:
        raise ValueError("the native transport contract is not uniquely pinned")
    contract = _object(contract_path)
    attestation = _absolute(contract["session_attestation"])
    attestations = [row for row in protocol["code_files"] if Path(row["path"]).resolve() == attestation.resolve()]
    if len(attestations) != 1:
        raise ValueError("the original native attestation source is not uniquely pinned")
    return {"schema": literals["SCHEMA"], "owned": names,
            "contract_sha256": pins[0]["sha256"],
            "attestation_source_sha256": attestations[0]["sha256"]}


def _parent(parent_protocol_path, routes, now, runner_path, stage):
    runner = _load_runner(runner_path)
    if stage not in ("dev", "cross"):
        raise ValueError("continuation stage must be dev or cross")
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("capture time must be finite epoch seconds")
    if len(routes) != len(ACTIVE_ROUTES) or set(routes) != set(ACTIVE_ROUTES):
        raise ValueError("continuation must prepare both original active Sonnet/Kimi routes")
    protocol_path = _absolute(Path(parent_protocol_path).resolve())
    raw = protocol_path.read_bytes()
    protocol_sha = _sha(raw)
    if protocol_path.with_suffix(".sha256").read_text().strip() != protocol_sha:
        raise ValueError("parent protocol bytes drifted")
    protocol = _json(raw)
    if not isinstance(protocol, dict):
        raise ValueError("parent protocol must be an object")
    if now < runner.timestamp(protocol["execution"]["cutoff_at"]):
        raise ValueError("parent cutoff has not passed")
    # Runtime/config resolution and every inherited scientific pin are checked by
    # the existing runner, never by a second native runtime or science loader.
    checked, checked_sha = runner.verify_protocol(protocol_path, deep=True)
    if checked != protocol or checked_sha != protocol_sha:
        raise ValueError("parent protocol changed during preparation")
    root = _absolute(protocol["execution"]["raw_root"]).resolve()
    plan_path = _absolute(protocol["execution"]["plan_path"])
    plan_raw = plan_path.read_bytes()
    plan_sha = _sha(plan_raw)
    if protocol["execution"].get("plan_sha256") != plan_sha:
        raise ValueError("parent plan bytes are not bound by its protocol")
    plan = _json(plan_raw)
    if not isinstance(plan, dict) or "continuation" in plan:
        raise ValueError("the parent must be the original closed v4 plan")
    cases, workers = runner.validate_plan(protocol, plan)
    stage_cases = [case for case in plan["cases"] if case["stage"] == stage]
    if len(stage_cases) != (DEV_CASES if stage == "dev" else CROSS_CASES):
        raise ValueError("the full original 399-DEV or 15-cross stage cohort is required")
    if any(route not in runner.route_order(protocol) for route in routes):
        raise ValueError("a continuation route was not scheduled in its parent")
    expected_paths = {root / "pending" / (key + ".json") for key in workers}
    initialized_paths = set((root / "pending").glob("*.json"))
    if initialized_paths != expected_paths:
        raise ValueError("all and only parent workers must be initialized and finished")
    state_pins = []
    for key, worker in workers.items():
        path = root / "pending" / (key + ".json")
        state = _object(path)
        if (state.get("schema"), state.get("route"), state.get("arm"),
                state.get("freeze_sha256"), state.get("plan_sha256")) != (
                "oq6.scheduler-worker/v1", worker["route"], worker["arm"], protocol_sha, plan_sha):
            raise ValueError("parent worker identity/freeze drift: " + key)
        if (state.get("status") != "finished" or type(state.get("next_index")) is not int or
                state["next_index"] != len(worker["case_ids"]) or
                state.get("next_case_id") is not None or state.get("next_priority") is not None):
            raise ValueError("all initialized parent workers must be finished: " + key)
        state_pins.append(_pin(path))
    return types.SimpleNamespace(runner=runner, now=now, protocol=protocol, protocol_path=protocol_path,
                                 protocol_sha=protocol_sha, plan=plan, plan_path=plan_path,
                                 plan_sha=plan_sha, root=root, cases=cases, stage=stage,
                                 stage_cases=stage_cases, workers=workers,
                                 state_pins=state_pins, transport=_transport_profile(protocol))


def _role(directory, profile):
    """Hash role bytes opaquely; unknown/orphan native files never authorize retry."""
    directory = _absolute(directory)
    if (directory.resolve() != directory or directory.is_symlink() or
            (_present(directory) and not directory.is_dir())):
        raise ValueError("prior role directory must not alias another slot: " + str(directory))
    files = []
    if directory.exists():
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise ValueError("prior native evidence has an unsafe file: " + str(path))
            if path.is_file():
                files.append(_pin(path))
    receipt_path = directory / "receipt.json"
    receipt_pin = next((row for row in files if row["path"] == str(receipt_path)), None)
    owned = [row for row in files if row is not receipt_pin]
    consumed = _present(receipt_path) or any(_present(directory / name) for name in profile["owned"])
    receipt = _metadata(receipt_path) if receipt_pin else None
    status = receipt.get("status") if receipt else None
    record = {"directory": str(directory), "consumed": consumed,
              "receipt_path": str(receipt_path) if receipt_pin else None,
              "receipt_sha256": receipt_pin["sha256"] if receipt_pin else None,
              "status": status if isinstance(status, str) else None,
              "owned_files": [{"path": row["path"], "sha256": row["sha256"]} for row in owned]}
    artifacts = {str(Path(row["path"]).relative_to(directory)): {"sha256": row["sha256"], "bytes": row["bytes"]}
                 for row in owned}
    return record, receipt, artifacts


def _audit_reusable(context, case, route, arm, role, receipt, artifacts):
    """Validate attested status/custody only, never findings, gates, or quality."""
    if not receipt or not role["consumed"]:
        return False
    directory = Path(role["directory"])
    profile = context.transport
    expected = {"schema": profile["schema"], "status": "answered", "attempt_consumed": True,
                "requested_route": route, "requested_model": route,
                "requested_provider": route.split("/", 1)[0], "requested_model_id": route.split("/", 1)[1],
                "expected_array": "findings",
                "config_sha256": context.protocol["config"]["sha256"],
                "prompt_sha256": case["prompts"][arm]["sha256"],
                "system_sha256": context.protocol["auditor_system_sha256"],
                "contract_sha256": profile["contract_sha256"],
                "attestation_source_sha256": profile["attestation_source_sha256"],
                "native_response_matches_stdout": True, "native_prompt_matches_frozen_bytes": True,
                "isolation_valid": True, "served_identities": [route], "artifacts": artifacts}
    if any(receipt.get(key) != value for key, value in expected.items()):
        return False
    if any(receipt.get(key) is not True for key in (
            "attempt_consumed", "native_response_matches_stdout", "native_prompt_matches_frozen_bytes", "isolation_valid")):
        return False
    if (type(receipt.get("timeout_seconds")) is not int or not 1 <= receipt["timeout_seconds"] <= 600 or
            receipt.get("receipt_path") != role["receipt_path"] or
            receipt.get("response_path") != str(directory / "response.txt")):
        return False
    marker_artifact = artifacts.get("call-consumed.json")
    marker_path = directory / "call-consumed.json"
    if (marker_artifact is None or receipt.get("call_consumed_path") != str(marker_path) or
            receipt.get("call_consumed_sha256") != marker_artifact["sha256"]):
        return False
    marker = _metadata(marker_path)
    if (marker is None or type(marker.get("timeout_seconds")) is not int or
            any(field not in marker or field not in receipt or marker[field] != receipt[field] for field in REQUEST_FIELDS)):
        return False
    if any(receipt.get(key) for key in ("errors", "fallback_events", "memory_events",
                                        "tool_events", "retry_events", "context_violations", "model_fallback")):
        return False
    changes = receipt.get("model_change_events")
    if not isinstance(changes, list):
        return False
    for event in changes:
        if not isinstance(event, dict) or "identity" not in event:
            return False
        # Frozen transport permits an initial same-route selection and filters
        # null identities here; its reported unbound-identity errors still fail
        # the receipt above, rather than inventing another identity policy.
        if event["identity"] is not None and event["identity"] != route:
            return False
        fallback = event.get("resolvedModelIsFallback")
        if fallback is not None and fallback is not False:
            return False
    response = artifacts.get("response.txt")
    return response is not None and receipt.get("response_sha256") == response["sha256"]


def _prediction_reusable(path, case, route, arm, audit, receipt):
    prediction = _metadata(path)
    expected = {"schema": "oq6.frozen-prediction/v1", "case_id": case["case_id"],
                "route": route, "arm": arm, "audit_status": "answered",
                "source_sha256": case.get("source_sha256"),
                "audit_receipt_sha256": audit["receipt_sha256"],
                "response_sha256": receipt.get("response_sha256"), "parse_error": None}
    return (prediction is not None and all(key in prediction and prediction[key] == value for key, value in expected.items())
            and isinstance(prediction.get("parsed_payloads"), list)
            and type(prediction.get("raw_findings_count")) is int
            and prediction["raw_findings_count"] == len(prediction["parsed_payloads"]))


def _capture(context, routes):
    workers = {}
    consumed_directories = set()
    for route in routes:
        for arm in ("A", "B"):
            cases = {}
            for case in context.stage_cases:
                directory = context.root / "calls" / route.replace("/", "--") / arm / context.stage / case["case_id"]
                if directory.is_symlink():
                    raise ValueError("prior cell directory must not alias another slot")
                audit, receipt, artifacts = _role(directory / "audit", context.transport)
                scorer, _, _ = _role(directory / "scorer", context.transport)
                for role in (audit, scorer):
                    if role["consumed"]:
                        canonical = str(Path(role["directory"]).resolve())
                        if canonical in consumed_directories:
                            raise ValueError("duplicate prior native consumption directory")
                        consumed_directories.add(canonical)
                final, prediction = directory / "cell.json", directory / "prediction.json"
                for path in (final, prediction):
                    if path.is_symlink() or (_present(path) and not path.is_file()):
                        raise ValueError("prior cell/prediction evidence is not an immutable file")
                final_pin = _pin(final) if final.exists() else None
                prediction_pin = _pin(prediction) if prediction.exists() else None
                action = "prior_terminal"
                if final_pin is None and not scorer["consumed"]:
                    if not audit["consumed"]:
                        if prediction_pin is None:
                            action = ("unavailable_input" if context.stage == "cross" and
                                      case["preparation_status"] != "prepared" else "audit")
                    elif (case["preparation_status"] == "prepared" and
                          _audit_reusable(context, case, route, arm, audit, receipt, artifacts) and
                          (prediction_pin is None or _prediction_reusable(prediction, case, route, arm, audit, receipt))):
                        action = "resume_scorer"
                cases[case["case_id"]] = {"action": action, "audit": audit, "scorer": scorer,
                                         "cell_path": final_pin["path"] if final_pin else None,
                                         "cell_sha256": final_pin["sha256"] if final_pin else None,
                                         "prediction_path": prediction_pin["path"] if prediction_pin else None,
                                         "prediction_sha256": prediction_pin["sha256"] if prediction_pin else None}
            workers[context.runner.worker_key(route, arm)] = {"route": route, "arm": arm, "cases": cases}
    return {"schema": SNAPSHOT_SCHEMA, "stage": context.stage, "parent_root": str(context.root),
            "parent_protocol_sha256": context.protocol_sha, "parent_plan_sha256": context.plan_sha,
            "captured_at": context.runner.utc(context.now), "workers": workers}


def capture_prior_snapshot(parent_protocol_path, *, stage="dev", routes=ACTIVE_ROUTES, now=None, runner_path=None):
    """Read closed-parent consumption metadata; do not create or dispatch anything."""
    context = _parent(parent_protocol_path, routes, now, runner_path, stage)
    return _capture(context, routes)


def _snapshot_pins(snapshot):
    pins = []
    for worker in snapshot["workers"].values():
        for case in worker["cases"].values():
            for role_name in ("audit", "scorer"):
                role = case[role_name]
                if role["receipt_path"] is not None:
                    pins.append(_pin(role["receipt_path"], role["receipt_sha256"]))
                pins.extend(_pin(row["path"], row["sha256"]) for row in role["owned_files"])
            for field in ("cell", "prediction"):
                if case[field + "_path"] is not None:
                    pins.append(_pin(case[field + "_path"], case[field + "_sha256"]))
    return pins


def _case_pins(context):
    pins = []
    cache = Path(context.protocol["execution"]["science_root"]).resolve() / "scorer-only-keys"
    for case in context.stage_cases:
        for field, suffix in (("keys", ".json"), ("keys_provenance", ".provenance.json")):
            path = _absolute(case[field + "_path"])
            if path.resolve() != cache / (case["case_id"] + suffix):
                raise ValueError("continuation must retain the exact original scorer key cache")
            pins.append(_pin(path, case[field + "_sha256"]))
        if case["preparation_status"] != "prepared":
            continue
        if case.get("source_path") is not None:
            pins.append(_pin(case["source_path"], case["source_sha256"]))
        for arm in ("A", "B"):
            prompt = case["prompts"][arm]
            pins.append(_pin(prompt["path"], prompt["sha256"]))
        package = _absolute(case["package"]).resolve()
        for row in case["files"]:
            relative = PurePosixPath(row["path"])
            path = (package / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(package):
                raise ValueError("frozen source file escapes its original package")
            pins.append(_pin(path, row["sha256"]))
    return pins


def _append_pins(original, additions):
    rows = copy.deepcopy(original)
    by_path = {}
    for row in rows:
        if row["path"] in by_path and by_path[row["path"]] != row["sha256"]:
            raise ValueError("conflicting inherited frozen pins")
        by_path[row["path"]] = row["sha256"]
    for row in additions:
        if row["path"] in by_path:
            if by_path[row["path"]] != row["sha256"]:
                raise ValueError("conflicting continuation frozen pin: " + row["path"])
        else:
            rows.append(row)
            by_path[row["path"]] = row["sha256"]
    return rows


def _source_snapshots(root, code_rows):
    snapshots, seen = [], {}
    for row in code_rows:
        source = _absolute(row["path"])
        raw = source.read_bytes()
        if _sha(raw) != row["sha256"]:
            raise ValueError("scientific source changed during preparation")
        path = root / "frozen-source" / row["sha256"] / source.name
        if path not in seen:
            _write_new(path, raw)
            seen[path] = _pin(path, row["sha256"])
        snapshots.append({"source_path": str(source), **seen[path]})
    return snapshots


def _seed(root, snapshot):
    for worker in snapshot["workers"].values():
        for case_id, case in worker["cases"].items():
            if case["action"] != "resume_scorer":
                continue
            directory = root / "calls" / worker["route"].replace("/", "--") / worker["arm"] / snapshot["stage"] / case_id
            directory.mkdir(parents=True, exist_ok=False)
            (directory / "audit").symlink_to(case["audit"]["directory"], target_is_directory=True)
            if case["prediction_path"] is not None:
                raw = Path(case["prediction_path"]).read_bytes()
                if _sha(raw) != case["prediction_sha256"]:
                    raise ValueError("prior prediction changed before exact-byte seeding")
                _write_new(directory / "prediction.json", raw)


def prepare_continuation(*, parent_protocol_path, output_root, checkpoint_at,
                         stage="dev", routes=ACTIVE_ROUTES, now=None, prior_snapshot_path=None,
                         runner_path=None, coverage_path=None, provider_lock_root=None):
    """Freeze route-local plans and seed only unsent-scorer resumes; never send.

    output_root must not exist. Every returned route protocol uses that same raw
    root, but has only its own two workers. provider_lock_root is the directory
    containing provider-<provider>.lock files and may be shared across stage roots.
    A supplied prior snapshot must match a fresh closed-parent census, apart from
    capture time; an exact immutable copy is placed in the new root and bound by plans.
    """
    context = _parent(parent_protocol_path, routes, now, runner_path, stage)
    checkpoint = context.runner.timestamp(checkpoint_at)
    if checkpoint <= context.now:
        raise ValueError("continuation checkpoint must be explicitly in the future")
    root = Path(output_root).resolve()
    science_root = Path(context.protocol["execution"]["science_root"]).resolve()
    if (_present(output_root) or _present(root) or
            root.is_relative_to(context.root) or root.is_relative_to(science_root)):
        raise ValueError("continuation needs a new raw root outside immutable parent/science roots")
    lock_root = (Path(provider_lock_root).resolve() if provider_lock_root is not None else root / "locks")
    if lock_root.is_relative_to(context.root) or lock_root.is_relative_to(science_root):
        raise ValueError("continuation provider locks must not modify immutable parent/science roots")
    snapshot = _capture(context, routes)
    snapshot_raw = _bytes(snapshot)
    supplied_pin = []
    if prior_snapshot_path is not None:
        supplied = _absolute(Path(prior_snapshot_path).resolve())
        supplied_snapshot = _object(supplied)
        captured_at = supplied_snapshot.get("captured_at")
        if (not isinstance(captured_at, str) or
                not context.runner.timestamp(context.protocol["execution"]["cutoff_at"]) <=
                context.runner.timestamp(captured_at) <= context.now):
            raise ValueError("prior snapshot capture time must follow parent close and not be in the future")
        comparison = dict(supplied_snapshot, captured_at=snapshot["captured_at"])
        if _bytes(comparison) != _bytes(snapshot):
            raise ValueError("prior consumption snapshot is malformed, stale, or conflicts with the closed parent")
        snapshot_raw = supplied.read_bytes()
        supplied_pin = [_pin(supplied, _sha(snapshot_raw))]
        snapshot = supplied_snapshot
    artifact_pins = _snapshot_pins(snapshot)
    input_pins = _case_pins(context)
    source = _absolute(Path(runner_path).resolve() if runner_path is not None else HERE / "runner.py")
    coverage = _absolute(Path(coverage_path).resolve() if coverage_path is not None else HERE / "coverage.py")
    replacement = _pin(source)
    selector_pins = [_pin(Path(__file__).resolve()), _pin(coverage)]
    original_code = context.protocol["code_files"]
    # The detached production runtime also pins src/evallab/runner.py; only the
    # experiment scheduler is replaceable, never a same-basename science file.
    scheduler_suffix = ("research", "experiments", "oq6-task-quality-gate-rerun", "runner.py")
    old_runners = [row for row in original_code if Path(row["path"]).parts[-4:] == scheduler_suffix]
    if len(old_runners) != 1:
        raise ValueError("parent must pin exactly one scheduling runner source")
    code = [copy.deepcopy(replacement if row is old_runners[0] else row) for row in original_code]
    code = _append_pins(code, selector_pins)
    # All validation above is read-only. Exclusive creation refuses an accidental
    # re-preparation and never rewrites a parent, previous freeze, or call cell.
    root.mkdir(parents=True, exist_ok=False)
    snapshot_path = root / "prior-consumption.json"
    _write_new(snapshot_path, snapshot_raw)
    _write_new(snapshot_path.with_suffix(".sha256"), (_sha(snapshot_raw) + "\n").encode())
    references = {"schema": PLAN_SCHEMA, "stage": stage, "parent_protocol_path": str(context.protocol_path),
                  "parent_protocol_sha256": context.protocol_sha, "parent_plan_path": str(context.plan_path),
                  "parent_plan_sha256": context.plan_sha, "parent_root": str(context.root),
                  "prior_snapshot_path": str(snapshot_path), "prior_snapshot_sha256": _sha(snapshot_raw)}
    source_snapshots = _source_snapshots(root, code)
    data = _append_pins(context.protocol["pre_cross_data_files"],
                        [_pin(context.protocol_path, context.protocol_sha), _pin(context.plan_path, context.plan_sha),
                         _pin(snapshot_path, references["prior_snapshot_sha256"]),
                         *context.state_pins, *supplied_pin, *artifact_pins, *input_pins,
                         *[{key: row[key] for key in ("path", "sha256", "bytes")} for row in source_snapshots]])
    _seed(root, snapshot)
    ordered_ids = [case["case_id"] for case in sorted(
        context.stage_cases, key=lambda case: (context.runner.cohort(case), case["scheduling_priority"]))]
    route_rows = []
    for route in routes:
        route_root = root / "routes" / route.replace("/", "--")
        plan_path, protocol_path = route_root / "plan.json", route_root / "protocol.json"
        workers = []
        for arm in ("A", "B"):
            cases = snapshot["workers"][context.runner.worker_key(route, arm)]["cases"]
            workers.append({"route": route, "arm": arm,
                            "case_ids": [cid for cid in ordered_ids if cases[cid]["action"] in {"audit", "resume_scorer"}],
                            "prior_case_ids": [cid for cid in ordered_ids if cases[cid]["action"] in {"prior_terminal", "unavailable_input"}],
                            "resume_scorer_case_ids": [cid for cid in ordered_ids if cases[cid]["action"] == "resume_scorer"]})
        plan = {"cases": copy.deepcopy(context.stage_cases), "workers": workers, "continuation": references}
        plan_raw = _bytes(plan)
        plan_sha = _sha(plan_raw)
        protocol = copy.deepcopy(context.protocol)
        protocol["frozen_at"] = context.runner.utc(context.now)
        protocol["study_version"] = "oq6-gate-rerun-v5-" + stage + "-continuation"
        protocol["code_files"] = code
        protocol["pre_cross_data_files"] = _append_pins(data, [
            {"path": str(plan_path), "sha256": plan_sha, "bytes": len(plan_raw)}])
        protocol["continuation"] = {**references, "schema": FREEZE_SCHEMA,
                                    "checkpoint_is_completion": False, "source_snapshots": source_snapshots}
        protocol["execution"].update(raw_root=str(root), plan_path=str(plan_path), plan_sha256=plan_sha,
                                      route_order=[route], cutoff_at=checkpoint_at, provider_lock_root=str(lock_root))
        protocol_raw = _bytes(protocol)
        protocol_sha = _sha(protocol_raw)
        states = context.runner.initial_states(protocol, plan, protocol_sha, plan_sha, now=context.now)
        _write_new(plan_path, plan_raw)
        _write_new(plan_path.with_suffix(".sha256"), (plan_sha + "\n").encode())
        _write_new(protocol_path, protocol_raw)
        _write_new(protocol_path.with_suffix(".sha256"), (protocol_sha + "\n").encode())
        for key, state in states.items():
            context.runner.write_json(root / "pending" / (key + ".json"), state, immutable=True)
        route_rows.append({"route": route, "protocol_path": str(protocol_path), "protocol_sha256": protocol_sha,
                           "plan_path": str(plan_path), "plan_sha256": plan_sha,
                           "worker_state_paths": [str(root / "pending" / (key + ".json")) for key in states]})
    # A changed role inventory, receipt, prediction, or close state invalidates a
    # prospective preparation; none of these checks writes into prior evidence.
    if _capture(context, routes) != dict(snapshot, captured_at=context.runner.utc(context.now)):
        raise ValueError("parent consumption changed while continuation was being frozen")
    for row in data:
        _pin(row["path"], row["sha256"])
    manifest = {"schema": "oq6.stage-continuation-preparation/v1", "stage": stage,
                "prepared_at": context.runner.utc(context.now), "raw_root": str(root),
                "provider_lock_root": str(lock_root), "checkpoint_at": checkpoint_at, "checkpoint_is_completion": False,
                "represented_cases_per_worker": len(context.stage_cases), "prior_snapshot_path": str(snapshot_path),
                "prior_snapshot_sha256": references["prior_snapshot_sha256"], "routes": route_rows,
                "dispatch_performed": False, "paid_spend_usd": 0}
    _write_new(root / "preparation.json", _bytes(manifest))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-protocol", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--checkpoint-at", required=True, help="Explicit future UTC checkpoint, not ALL-DEV completion")
    parser.add_argument("--prior-snapshot", type=Path)
    parser.add_argument("--stage", choices=("dev", "cross"), default="dev")
    parser.add_argument("--provider-lock-root", type=Path, help="Shared provider-*.lock directory across stage roots")
    parser.add_argument("--runner-source", type=Path)
    parser.add_argument("--coverage-source", type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare_continuation(parent_protocol_path=args.parent_protocol, output_root=args.output_root,
                                      checkpoint_at=args.checkpoint_at, prior_snapshot_path=args.prior_snapshot,
                                      stage=args.stage, provider_lock_root=args.provider_lock_root,
                                      runner_path=args.runner_source, coverage_path=args.coverage_source)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, "continuation preparation rejected: " + str(error) + "\n")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
