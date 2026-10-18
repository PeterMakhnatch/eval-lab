#!/usr/bin/env python3
"""Fresh BASE K=4 generation, not correction replay or a historical B-v2 build.

All commands run from an isolated eval-lab worktree with its owned dependencies::

    uv run --no-sync python research/experiments/repaired-data-sft-v2/native_generation.py \
      plan --config SETTINGS.json --out runs/phase2-NAME
    uv run --no-sync python research/experiments/repaired-data-sft-v2/native_generation.py \
      admit --plan runs/phase2-NAME/plan.json --envelope PARENT-ENVELOPE.json
    keys run -- uv run --no-sync python research/experiments/repaired-data-sft-v2/native_generation.py \
      run --plan runs/phase2-NAME/plan.json --envelope PARENT-ENVELOPE.json \
      --serving-window ACTUAL-WINDOW.json
    uv run --no-sync python research/experiments/repaired-data-sft-v2/native_generation.py \
      collect --plan runs/phase2-NAME/plan.json --out runs/phase2-NAME-admission

plan/admit/collect are provider-free. Only run submits/approves/ticks the public
ExperimentSpec -> Executor -> NativeMimoAgent/LockedModalEnvironment path.
There is no retry, resume, historical diff, restored conversation, training,
deployment, synthetic runner, readiness bypass, or global policy bypass.

SETTINGS schema mimo.phase2-native-settings/v1 (all operational values explicit):
{campaign_id, reference_profile, modal_app_name, concurrency, runtime:{
 timeout_seconds,max_requests,max_input_tokens,cost_limit_usd},
 sandbox_timeout_seconds, image_build_reserve_usd_per_cell,
 other_provider_reserve_usd_per_cell, cost_evidence:{path,sha256},
 controls:{path,sha256}, request:{path,sha256},
 control_admission:{path,sha256}, parent_packages:{digest:path}}
parent_packages is optional, for real archived parents whose historical local
path no longer exists; it never changes a VariantRecord. cost_evidence must be a
parent-owned quote with pricing_basis='resource_bound_plus_build_buffer',
provider_product='modal_sandbox', cpu_usd_per_core_second,
memory_usd_per_gib_second, image_build_usd_per_cell, other_provider_usd_per_cell,
unknown_extras_acknowledged=true, source_urls and mechanism text.
Reserves cover the explicit quote, NOT an invoice/account-dollar guarantee.
Sandbox exposure covers BOTH agent and separate-verifier setup, generation,
collection and grading within each provider TTL at actual hard CPU/RAM limits.
Image/registry creation lies OUTSIDE that TTL: the explicit build/provider
buffer is a conservative estimate, not a proved hard bound or zero-cache bill.
TTL exhaustion is incomplete, never a repaired pass. Declared task healthcheck
and verifier timeout bytes are not rewritten.

PARENT-ENVELOPE schema mimo.phase2-envelope/v1:
{reservation_id,approval,approved_usd,admitted:true,expires_at,plan:{path,sha256},
 native:{cap_usd,usd_per_execution,executions_max:76,durable_journal_path},
 serving:{cap_usd}, heldout_regrade:{cap_usd}, overhead:{cap_usd}}.
heldout_regrade/overhead are optional; every supplied section cap is counted.
No fixed old $4 allowance: the actual parent's immutable approval owns the cap.
Parent initializes the EXISTING native durable_journal_path once, before run:
{reservation_id,envelope:{path,sha256},approval,cap_usd,usd_per_execution,
 executions:0,reserved_usd:0,charges:[]}. Each one-attempt cell irrevocably charges
that same CloudSpendGate under flock + fsynced atomic replace BEFORE any tick,
including readiness probes, refusals or failures. No reset/refund is supported.

plan.json binds 19 immutable repaired packages/records/parents/controls and 76
unique specs (attempts=concurrency=1,diff_sources=[]). admit calls unchanged
validate_request on ALL cells, requiring the parent's exact profile/canonical
control-backed registration; candidates are never edited to validated.
executions.jsonl has one disposition per planned cell, actual IDs or null,
complete hash-bound native artifact inventory, raw ordered egress applications,
phase intervals, actual provider model bindings, full/kept prehidden diffs and
repaired reward/error endpoints. collect writes fresh-repaired-sources.jsonl,
faithful-conversations.jsonl and admission.json via fresh_native_admission.py.
That DISTINCT population uses proof_kind=fresh_native_repaired, explicitly
original_v6_grade=not_requested_unmeasured. Historical build.py's strict dual
regrade contract, minimum 51, and default exact token-matched C are unchanged.
No tokenizer/trainer/C schedule or speculative B-only build is dispatched here.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import math
import os
import re
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
BASE_MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
RETURNED_MODEL = BASE_MODEL.removeprefix("selfhosted/")
CONTROLS_SHA = "sha256:ca3d4460296cdfa017741d3557f3455bccf74b8f26f24643ff4a2c1f50501e80"
REQUEST_SHA = "sha256:eabdefff50840b7102ac680b9f729915eb41d0e50d234e2c9680686d945f5a33"
TASK_IDS = tuple(["000089", "000125", "000169", "000354", "000561", "000792", "000931", "000945", "001205", "001267", "001269", "001750", "001804", "001809", "001985", "002160", "002216", "002391", "002709"])
K = 4
CELLS = len(TASK_IDS) * K
PROBE_SECONDS = 10.0


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the existing native evidence helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Reuse the existing fsynced/credential-safe correction machinery, not its fixed
# old campaign allowance, correction prompt, observation, or one-row protocol.
helpers = load_module("phase2_native_correction_helpers", ROOT / "research/experiments/test-repair-performance-20261017/run_corrections.py")
Refusal = helpers.Refusal
require, same = helpers.require, helpers.same
ref, checked_ref = helpers.ref, helpers.checked_ref
write_json, append_json = helpers.write_json, helpers.append_json
now, failure = helpers.now, helpers.failure
utc_instant, safe_path = helpers.utc_instant, helpers.safe_path


def money(value: Any) -> float:
    """Normalize shared decimal strings at the legacy numeric spend-gate boundary."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise Refusal("USD amounts must be finite nonnegative decimal values") from exc
    return helpers.money(value)


def equal_money(actual: Any, expected: Any, message: str) -> None:
    helpers.equal_money(money(actual), money(expected), message)


def dependencies() -> tuple[Any, Any, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from evallab import execution_contracts, queue, schemas

    for module in (execution_contracts, queue, schemas):
        require(Path(module.__file__).resolve().is_relative_to(ROOT / "src/evallab"), "native modules loaded from another checkout")
    return execution_contracts, queue, schemas


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def bound_json(value: dict[str, Any]) -> Any:
    checked_ref(value)
    return read_json(Path(value["path"]))


def checked_settings(path: Path) -> dict[str, Any]:
    value = read_json(path)
    same(value["schema"], "mimo.phase2-native-settings/v1", "wrong native settings schema")
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,27}", value["campaign_id"]), "campaign_id must be a unique legal 3-28-character name")
    require(type(value["concurrency"]) is int and 1 <= value["concurrency"] <= 16, "parent must explicitly bound concurrency to 1..16")
    same(value["controls"]["sha256"], CONTROLS_SHA, "controls handoff is not the exact approved hash")
    same(value["request"]["sha256"], REQUEST_SHA, "control request is not the exact approved hash")
    for key in ("timeout_seconds", "max_requests", "max_input_tokens"):
        require(type(value["runtime"][key]) is int and value["runtime"][key] > 0, "runtime limits must be explicit positive integers")
    require(money(value["runtime"]["cost_limit_usd"]) > 0, "proxy cost fence must be explicit; it is not serving spend")
    require(type(value["sandbox_timeout_seconds"]) is int and value["sandbox_timeout_seconds"] > value["runtime"]["timeout_seconds"], "provider TTL must leave explicit non-agent lifecycle room")
    for key in ("image_build_reserve_usd_per_cell", "other_provider_reserve_usd_per_cell"):
        money(value[key])
    cost = bound_json(value["cost_evidence"])
    same(cost.get("pricing_basis"), "resource_bound_plus_build_buffer", "parent's explicit conservative resource/build-buffer pricing basis is required")
    require(cost.get("unknown_extras_acknowledged") is True, "unknown image/registry/provider extras must be acknowledged, not declared free or hard bounded")
    require(isinstance(cost.get("mechanism"), str) and cost["mechanism"].strip(), "parent's actual resource and build-buffer mechanism is required")
    require(isinstance(cost.get("source_urls"), list) and cost["source_urls"] and all(isinstance(url, str) and url.startswith("https://") for url in cost["source_urls"]), "actual Sandbox pricing source URLs are required")
    same(cost.get("provider_product"), "modal_sandbox", "ordinary Modal Function rates are not the native Sandbox price contract")
    require(money(cost["cpu_usd_per_core_second"]) > 0 and money(cost["memory_usd_per_gib_second"]) > 0, "actual Sandbox CPU/RAM rates must be explicitly quote-bound")
    require(money(value["image_build_reserve_usd_per_cell"]) >= money(cost["image_build_usd_per_cell"]), "image-build quote exceeds its reserve")
    require(money(value["other_provider_reserve_usd_per_cell"]) >= money(cost["other_provider_usd_per_cell"]), "other-provider quote exceeds its reserve")
    admission = bound_json(value["control_admission"])
    same(admission["schema"], "repaired_data_sft_v2.control_admission/v1", "wrong scoped control admission schema")
    same(admission["source_controls_handoff"]["sha256"], CONTROLS_SHA, "scoped control audit is not bound to the exact approved control source")
    checked_ref(admission["source_controls_handoff"])
    same(tuple(row["task_id"] for row in admission["tasks"]), TASK_IDS, "scoped control admission must bind exactly the approved nineteen")
    require(all(row.get("controls_qualified") is True for row in admission["tasks"]), "a selected task lacks actual retained control qualification")
    return value


def cohort(settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    controls = bound_json(settings["controls"])
    request = bound_json(settings["request"])
    strict = [row for row in controls["tasks"] if row.get("strict_assertion_scope_admission") is True]
    same(tuple(row["task_id"] for row in strict), TASK_IDS, "strict controls must select exactly the approved nineteen, never 002486")
    same(len(request["candidates"]), 20, "approved planned candidate count changed")
    excluded = set(request["heldout_exclusion"]["all_parent_and_descendant_ids"])
    excluded.update(row["task"] for row in request["selection"]["explicit_training_exclusions"])
    local_exclusion = read_json(Path(__file__).with_name("m1-cohort.json"))
    excluded.update(local_exclusion["training_exclusion"]["parent_task_ids"])
    candidates = {row["task"][-6:]: row for row in request["candidates"]}
    audited = {row["task_id"]: row for row in bound_json(settings["control_admission"])["tasks"]}
    rows = []
    for control in strict:
        candidate = candidates[control["task_id"]]
        require(candidate["task"] not in excluded and not candidate["task"].endswith(("001478", "002486")), "heldout/answer-visible/held task cannot enter generation")
        same(control.get("current_reader_gate_pass"), True, "control reader gate is not admitted")
        # Fresh cells use the current byte-audited controls, not historical-row reuse admission.
        identity = control["identity"]
        same(identity.get("package_digest", identity.get("package")), candidate["repaired_package"]["sha256"], "controls/request package mismatch")
        same(candidate["frozen_v6_package_digest"], candidate["repaired_child_v6_parent_digest"], "requested repair parent mismatch")
        audit = audited[control["task_id"]]
        same(audit["task"], candidate["task"], "scoped control audit changed original task ancestry")
        same(audit["identity"]["package_digest"], candidate["repaired_package"]["sha256"], "scoped control audit qualified another repaired package")
        same(audit["identity"]["harbor_digest"], identity.get("harbor_digest", identity.get("harbor")), "scoped control audit changed Harbor identity")
        same(audit["source_record"]["status"], "candidate", "immutable source candidate status must remain truthful")
        same(audit["canonical_record"]["status"], "candidate", "canonical copied source status must not be falsely promoted")
        checked_ref(audit["source_record"])
        canonical = audit["canonical_record"]
        canonical_path = Path(canonical["path"])
        require(not canonical_path.is_absolute() and ".." not in canonical_path.parts, "canonical admission records must be portable repository-relative references")
        checked_ref({"path": str((ROOT / canonical_path).resolve()), "sha256": canonical["sha256"]})
        rows.append({"candidate": candidate, "control": control})
    return controls, request, rows


def materialized_task(row: dict[str, Any], settings: dict[str, Any], out: Path) -> dict[str, Any]:
    from evallab.registry import compute_task_digests, harbor_task_digest
    from evallab.task_variants import VariantRecord, materialize, task_directory_digest, verify

    candidate, control = row["candidate"], row["control"]
    record_ref = candidate["native_variant_record"]
    record_data = bound_json(record_ref)
    record = VariantRecord.model_validate(record_data)
    same(record.task_name, candidate["task"], "repair record task changed")
    same(record.status, "candidate", "source candidate status must remain truthful, not rewritten")
    same(record.transform, "verifier-repair@1", "only the approved verifier repair is admitted")
    same(record.components_changed, ["verifier"], "repair changes more than the verifier")
    same(record.variant_digest, candidate["repaired_package"]["sha256"], "repair record child differs from approved package")
    same(record.parent.digest, candidate["repaired_child_v6_parent_digest"], "repair record lacks exact frozen-v6 ancestry")
    original = Path(candidate["repaired_package"]["path"])
    same(task_directory_digest(original), record.variant_digest, "external repair package bytes changed")
    same(harbor_task_digest(original), record.variant_harbor_digest, "external repair Harbor identity changed")
    historical_parent = (control.get("frozen_v6_parent") or {}).get("path") or record.parent.source.path
    parent = Path(settings.get("parent_packages", {}).get(record.parent.digest, historical_parent))
    same(task_directory_digest(parent), record.parent.digest, "real parent bytes changed or are missing")
    same(harbor_task_digest(parent), record.parent.harbor_digest, "real parent Harbor identity changed")
    package = materialize(record, parent, repo_root=ROOT, records_dir=out / "variant-records", variants_root=out / "task-store")
    same(verify(record, parent_dir=parent, repo_root=ROOT, records_dir=out / "variant-records", variants_root=out / "task-store"), [], "first-party repair reconstruction/lineage proof failed")
    same(task_directory_digest(package), candidate["repaired_package"]["sha256"], "materialization changed the package identity")
    exact_record = out / "source-records" / (record.variant_digest.removeprefix("sha256:") + ".json")
    exact_record.parent.mkdir(parents=True, exist_ok=True)
    with exact_record.open("xb") as stream:
        stream.write(Path(record_ref["path"]).read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    same(ref(exact_record)["sha256"], record_ref["sha256"], "materialized source record is not byte-identical")
    config = tomllib.loads((package / "task.toml").read_text())
    same(config["task"]["name"], candidate["task"], "materialized task name changed")
    same(config["metadata"]["source_id"], candidate["task"].split("/")[-1], "materialized parent source identity changed")
    same(config["verifier"]["environment_mode"], "separate", "fresh generation requires the real separate verifier")
    require(not (package / "environment/docker-compose.yaml").exists(), "this bound is for single-container separate-verifier tasks only")
    environment = config["environment"]
    require(environment.get("cpus") and environment.get("memory_mb"), "undeclared resources cannot be quoted as a hard ceiling")
    # The approved cohort's verifier inherits this exact shape. Parent's source
    # resource admission also checks the actual native separate-verifier config.
    same(environment["cpus"], 2, "cohort CPU shape changed")
    same(environment["memory_mb"], 8192, "cohort RAM shape changed")
    require(not environment.get("gpus", 0), "GPU native task resources are not in this budget")
    return {"task": candidate["task"], "task_id": control["task_id"], "package_path": str(package),
            "package_digest": record.variant_digest, "harbor_digest": record.variant_harbor_digest,
            "verifier_digest": compute_task_digests(package).verifier, "source_package": candidate["repaired_package"],
            "source_record": record_ref, "record": ref(exact_record), "record_status": record.status,
            "parent": {"path": str(parent.resolve()), "package_digest": record.parent.digest, "harbor_digest": record.parent.harbor_digest, "task_toml": ref(parent / "task.toml")},
            "task_toml": ref(package / "task.toml"), "grader_source": ref(package / "tests/test.sh"),
            "resources": {"physical_cpus": environment["cpus"], "memory_mb": environment["memory_mb"], "sandboxes_per_cell": 2,
                          "cpu_enforcement_policy": "limit", "memory_enforcement_policy": "limit", "sandbox_create_max_attempts": 1},
            "unchanged_task_timeouts": {"healthcheck": (environment.get("healthcheck") or {}).get("timeout_sec"), "verifier": config["verifier"].get("timeout_sec")},
            "control_task_row_sha256": helpers.digest(helpers.canonical(control))}


def cost_model(settings: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    cost = bound_json(settings["cost_evidence"])
    cpu_rate = money(cost["cpu_usd_per_core_second"])
    memory_rate = money(cost["memory_usd_per_gib_second"])
    ttl = settings["sandbox_timeout_seconds"]
    sandbox_per_cell = max(2 * ttl * (task["resources"]["physical_cpus"] * cpu_rate + task["resources"]["memory_mb"] / 1024 * memory_rate) for task in tasks)
    quote = sandbox_per_cell + settings["image_build_reserve_usd_per_cell"] + settings["other_provider_reserve_usd_per_cell"]
    # CloudSpendGate reports four decimals; round UP at the same precision so
    # its durable reserved_usd never understates the resource quote.
    unit = math.ceil(quote * 10000 - 1e-10) / 10000
    return {"reservation_type": "fresh_native_repaired", "executions_max": CELLS, "sandboxes_per_cell": 2,
            "sandbox_create_max_attempts": 1, "sandbox_timeout_seconds": ttl,
            "cpu_usd_per_physical_core_second": cpu_rate,
            "memory_usd_per_gib_second": memory_rate,
            "sandbox_exposure_usd_per_cell": sandbox_per_cell,
            "image_build_reserve_usd_per_cell": settings["image_build_reserve_usd_per_cell"],
            "other_provider_reserve_usd_per_cell": settings["other_provider_reserve_usd_per_cell"],
            "usd_per_execution_minimum": unit, "total_reservation_minimum_usd": round(CELLS * unit, 4),
            "pricing_basis": "resource_bound_plus_build_buffer", "image_provider_quote": settings["cost_evidence"],
            "unknown_extras_acknowledged": True, "image_registry_charges_hard_bounded": False,
            "quote_is_invoice_guarantee": False, "original_v6_replays": 0,
            "original_v6_grade": "not_requested_unmeasured", "actual_invoice_usd": None,
            "budget_truncation": {"provider_ttl_may_interrupt_setup_or_grading": True, "agent_seconds": settings["runtime"]["timeout_seconds"], "task_timeouts_unchanged": True}}


def fresh_spec(task: dict[str, Any], attempt: int, settings: dict[str, Any], unit: float, modules: tuple[Any, Any, Any]) -> Any:
    native, queue, schemas = modules
    from evallab.mimoagent_worker import WRAPPER_ADDITIONS
    from evallab.setup_fingerprint import (
        build_intended_fingerprint,
        compare_fingerprint,
        load_reference_profile,
    )

    runtime = settings["runtime"]
    identifier = native.new_ulid()
    values = {"spec_id": identifier, "name": f"{settings['campaign_id']}-{task['task_id']}-k{attempt}-{identifier.lower()}",
              "hypothesis": "Independent fresh BASE K4 rollout on the exact control-admitted repaired package; explicit reduced budget, no history or diff input.",
              "purpose": "elicitation", "question_ref": settings["controls"]["path"], "task": task["task"],
              "task_path": str(Path(task["package_path"]).relative_to(ROOT)), "task_id": task["task"].split("/")[-1],
              "task_package_digest": task["package_digest"], "verifier_digest": task["verifier_digest"],
              "reference_profile": settings["reference_profile"], "agent": "mimoagent", "model": BASE_MODEL,
              "environment": "modal", "egress_lock": True, "modal_app_name": settings["modal_app_name"],
              "modal_resource_policy": "limit", "modal_sandbox_timeout_seconds": settings["sandbox_timeout_seconds"],
              "modal_sandbox_create_max_attempts": 1, "jobs_dir": "runs",
              "attempts": 1, "concurrency": 1, "diff_sources": [], "submitted_by": "phase2-trainer-parent",
              "est_cost_usd": unit, **runtime,
              "max_output_tokens": runtime["max_requests"] * 262144,
              "max_total_tokens": runtime["max_input_tokens"] + runtime["max_requests"] * 262144}
    require("modal_sandbox_create_max_attempts" in schemas.ExperimentSpec.model_fields, "parent's scoped typed single-create forwarding is not installed")
    draft = schemas.ExperimentSpec.model_validate(values)
    fingerprint = build_intended_fingerprint(spec=draft, task_dir=Path(task["package_path"]), model=BASE_MODEL, agent="mimoagent", environment="modal", repo_root=ROOT)
    same(fingerprint["harness"]["additions"], WRAPPER_ADDITIONS, "fresh source must have no extra prompt/history additions")
    profile = load_reference_profile(ROOT, settings["reference_profile"])
    changes = compare_fingerprint(fingerprint, profile)
    require(all(item["field"].startswith("budgets.") or item["field"] == "harness.additions" for item in changes), "fresh setup differs outside explicitly authorized native wrapper/runtime bounds")
    values["deviations"] = [{"field": item["field"], "value": item["actual"], "reason": "Parent-explicit fresh BASE reduced resource budget/native wrapper; no source history, replay, or matched-runtime claim."} for item in changes]
    spec = schemas.ExperimentSpec.model_validate(values)
    request = queue.Executor.prepare_request(spec, repo_root=ROOT)
    same(request.diff_sources, (), "fresh native request acquired a historical diff")
    require(settings["sandbox_timeout_seconds"] <= request.trial_watchdog_seconds, "provider TTL exceeds native watchdog")
    for key in ("modal_sandbox_create_max_attempts", "modal_resource_policy", "modal_sandbox_timeout_seconds", *runtime):
        same(getattr(request, key, None), getattr(spec, key), "typed native propagation lost a declared runtime/resource bound")
    return spec


def create_plan(settings_path: Path, out: Path) -> dict[str, Any]:
    settings = checked_settings(settings_path)
    _, request, selections = cohort(settings)
    modules = dependencies()
    require(out.is_relative_to(ROOT / "runs"), "owned native outputs must be inside this worktree's ignored runs directory")
    require(not out.exists(), "plan output must be NEW; no implicit plan/job reuse")
    out.mkdir(parents=True)
    tasks = [materialized_task(row, settings, out) for row in selections]
    cost = cost_model(settings, tasks)
    cells = []
    for task_index, task in enumerate(tasks):
        for attempt in range(1, K + 1):
            spec = fresh_spec(task, attempt, settings, cost["usd_per_execution_minimum"], modules)
            spec_path = out / "specs" / (spec.name + ".json")
            write_json(spec_path, spec.model_dump(mode="json", exclude_none=True))
            cells.append({"cell_id": f"{task['task_id']}-k{attempt}", "task_index": task_index, "attempt_index": attempt,
                          "task_id": task["task_id"], "task": task["task"], "spec_id": spec.spec_id, "job_name": spec.name, "spec": ref(spec_path)})
    plan = {"schema": "mimo.phase2-native-plan/v1", "created_at": now(), "settings": ref(settings_path), "config": settings,
            "controls": settings["controls"], "request": settings["request"], "excluded_tasks": request["heldout_exclusion"]["all_parent_and_descendant_ids"],
            "control_admission": settings["control_admission"],
            "answer_visible_exclusion": "mimo-v2.6-rl/format-code-task-001478", "held_control_exclusion": "002486",
            "model": BASE_MODEL, "returned_model_required": RETURNED_MODEL, "K": K, "tasks": tasks, "cells": cells, "cost_model": cost,
            "max_transient_retries": 0, "provider_calls": 0, "model_calls": 0, "native_executions": 0,
            "original_v6_grade": "not_requested_unmeasured", "training_admitted": False}
    validate_cells(plan)
    write_json(out / "plan.json", plan)
    return plan


def validate_cells(plan: dict[str, Any]) -> None:
    same(plan["schema"], "mimo.phase2-native-plan/v1", "wrong native plan schema")
    same(tuple(task["task_id"] for task in plan["tasks"]), TASK_IDS, "plan is not exactly the admitted nineteen")
    same(plan["K"], K, "only fresh K4 is approved")
    same(len(plan["cells"]), CELLS, "exactly 76 independent native cells are required")
    for key in ("cell_id", "spec_id", "job_name"):
        same(len({row[key] for row in plan["cells"]}), CELLS, "native cells must have unique identities")
    same({(row["task_id"], row["attempt_index"]) for row in plan["cells"]}, {(task, attempt) for task in TASK_IDS for attempt in range(1, K + 1)}, "fresh task/attempt matrix changed")
    same(plan["max_transient_retries"], 0, "automatic native retry is forbidden")
    for cell in plan["cells"]:
        require(type(cell["task_index"]) is int and 0 <= cell["task_index"] < len(TASK_IDS), "native task index is invalid")
        task = plan["tasks"][cell["task_index"]]
        same(cell["task_id"], task["task_id"], "native task index changed identity")
        same(cell["task"], task["task"], "native cell changed original task ancestry")
        require(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", cell["job_name"]), "native job name is not admitted legal syntax")


def checked_plan(path: Path) -> dict[str, Any]:
    plan = read_json(path)
    validate_cells(plan)
    settings = checked_settings(Path(plan["settings"]["path"]))
    checked_ref(plan["settings"])
    same(settings, plan["config"], "settings changed after planning")
    cohort(settings)
    from evallab.registry import harbor_task_digest
    from evallab.task_variants import task_directory_digest

    for task in plan["tasks"]:
        for key in ("record", "source_record", "task_toml", "grader_source"):
            checked_ref(task[key])
        same(task_directory_digest(Path(task["package_path"])), task["package_digest"], "materialized native package changed")
        same(harbor_task_digest(Path(task["package_path"])), task["harbor_digest"], "materialized native Harbor identity changed")
        same(task_directory_digest(Path(task["parent"]["path"])), task["parent"]["package_digest"], "real frozen parent bytes changed")
    same(cost_model(settings, plan["tasks"]), plan["cost_model"], "native cost model changed")
    return plan


def envelope_sections(envelope: dict[str, Any]) -> dict[str, Any]:
    # Both placements are accepted only for the parent-owned v1 envelope.
    return envelope.get("sections", envelope)


def checked_envelope(path: Path, plan_path: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = read_json(path)
    same(envelope["schema"], "mimo.phase2-envelope/v1", "wrong shared parent envelope schema")
    require(envelope.get("admitted") is True, "parent shared serving/native envelope is NOT admitted")
    require(isinstance(envelope.get("approval"), str) and envelope["approval"].strip(), "parent approval identity is required")
    require(isinstance(envelope.get("reservation_id"), str) and envelope["reservation_id"], "parent reservation identity is required")
    same(envelope["plan"], ref(plan_path), "parent envelope does not bind this exact fresh plan")
    require(datetime.now(UTC) < utc_instant(envelope["expires_at"]), "parent native envelope expired")
    sections = envelope_sections(envelope)
    caps = [money(section["cap_usd"]) for section in sections.values() if isinstance(section, dict) and "cap_usd" in section]
    require(caps and sum(caps) <= money(envelope["approved_usd"]) + 1e-9, "shared envelope sections exceed the actual combined approval")
    native = sections["native"]
    same(native["executions_max"], CELLS, "parent must explicitly admit all 76 one-shot cells")
    unit = money(native["usd_per_execution"])
    require(unit >= plan["cost_model"]["usd_per_execution_minimum"], "parent native unit is below dual-sandbox plus image/provider reservation")
    require(unit * CELLS <= money(native["cap_usd"]) + 1e-9, "parent native envelope cannot cover all 76 cells")
    return envelope, native


def preflight(plan_path: Path, envelope_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[Any]]:
    native, queue, schemas = dependencies()
    plan = checked_plan(plan_path)
    envelope, native_cap = checked_envelope(envelope_path, plan_path, plan)
    from evallab import harbor_modal

    require(getattr(harbor_modal, "NATIVE_SINGLE_CREATE_SUPPORTED", False) is True, "parent's scoped one-create native adapter is required for this resource bound")
    specs = []
    for cell in plan["cells"]:
        spec = schemas.ExperimentSpec.model_validate(bound_json(cell["spec"]))
        same(spec.spec_id, cell["spec_id"], "planned native spec identity changed")
        same(spec.name, cell["job_name"], "planned native job name changed")
        same(spec.model, BASE_MODEL, "only exact BASE generation is approved")
        same((spec.attempts, spec.concurrency, spec.diff_sources), (1, 1, []), "no default trial expansion, historical diff or multi-attempt cell is allowed")
        require(spec.extra_instruction_path is None, "fresh generation cannot restore correction/history instructions")
        request = queue.Executor.prepare_request(spec, repo_root=ROOT)
        same(getattr(request, "modal_sandbox_create_max_attempts", None), 1, "one-create option was lost by the typed native queue")
        native.validate_request(request, repo_root=ROOT)
        require(not (ROOT / spec.jobs_dir / spec.name).exists(), "native job already exists; automatic retry/reuse is forbidden")
        specs.append(spec)
    return plan, envelope, native_cap, specs


class NativeBudget:
    """One existing parent envelope, no reset, no serving-as-native charge."""

    def __init__(self, envelope_path: Path, envelope: dict[str, Any], section: dict[str, Any]):
        self.envelope_path = envelope_path
        self.envelope_ref = ref(envelope_path)
        self.envelope = envelope
        self.section = section
        self.path = safe_path(Path(section["durable_journal_path"]))
        require(self.path.is_file(), "parent must initialize the durable native gate; worker/driver cannot mint an allowance")
        self.read()

    def read(self) -> dict[str, Any]:
        from evallab.regrade import CloudSpendGate

        checked_ref(self.envelope_ref)
        state = read_json(self.path)
        same(state["reservation_id"], self.envelope["reservation_id"], "native journal belongs to another reservation")
        same(state["envelope"], self.envelope_ref, "native journal is not bound to this immutable parent envelope")
        same(state["approval"], self.envelope["approval"], "native journal approval changed")
        for key in ("cap_usd", "usd_per_execution"):
            equal_money(state[key], self.section[key], "native gate amount changed")
        gate = CloudSpendGate(state["approval"], money(state["cap_usd"]), money(state["usd_per_execution"]), executions=state["executions"])
        same(type(state["executions"]), int, "native executions must be an integer")
        require(0 <= state["executions"] <= self.section["executions_max"], "native execution counter is outside the admitted envelope")
        equal_money(state["reserved_usd"], gate.to_json()["reserved_usd"], "durable native reserved amount disagrees with its nonrefundable charges")
        same(len(state["charges"]), state["executions"], "durable native gate lost charged events")
        same(len({row["cell_id"] for row in state["charges"]}), state["executions"], "native cell was charged twice")
        return state

    def charge(self, cell: dict[str, Any], window_ref: dict[str, Any]) -> dict[str, Any]:
        from evallab.regrade import CloudSpendGate

        with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            state = self.read()
            require(not any(row["cell_id"] == cell["cell_id"] for row in state["charges"]), "this cell already consumed its sole reservation; no retry/reset")
            require(state["executions"] < self.section["executions_max"], "all admitted native attempts were consumed")
            gate = CloudSpendGate(state["approval"], money(state["cap_usd"]), money(state["usd_per_execution"]), executions=state["executions"])
            gate.charge()
            event = {"at": now(), "ordinal": gate.executions, "cell_id": cell["cell_id"], "spec_id": cell["spec_id"], "job_name": cell["job_name"],
                     "reservation_type": "fresh_native_repaired", "reserved_usd": gate.usd_per_execution, "serving_window": window_ref}
            state.update(gate.to_json())
            state["charges"].append(event)
            write_json(self.path, state, replace=True)
            return event


def checked_window(path: Path, envelope_path: Path, plan: dict[str, Any], *, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    receipt = ref(path)
    if expected is not None:
        same(receipt, expected, "parent serving evidence changed during dispatch")
    value = read_json(path)
    same(value["schema"], "envcheck.modal-serving-window/v1", "wrong actual serving receipt schema")
    same(value["state"], "active", "parent serving window is not active")
    require(value.get("owner") and value.get("app_id") and value.get("window_id"), "actual parent serving ownership/app/window IDs are required")
    parsed = urlsplit(value["upstream"])
    require(parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".modal.run") and not parsed.username and not parsed.password and parsed.path in ("", "/") and not parsed.query and not parsed.fragment and parsed.port is None, "upstream must be the actual authenticated HTTPS Modal origin, not a fake endpoint")
    require(RETURNED_MODEL in value["allowed_models"], "serving window does not admit the exact actual upstream BASE ID")
    start, end = utc_instant(value["starts_at"]), utc_instant(value["expires_at"])
    now_utc = datetime.now(UTC)
    # This is a conservative launch margin for both sandbox TTLs and the
    # native probe, NOT a hard wall bound on outside-TTL image/registry builds.
    # Parent's independent stop guard still wins; expired/incomplete trials
    # retain null/error endpoints and cannot become repaired qualifiers.
    needed = timedelta(seconds=2 * plan["config"]["sandbox_timeout_seconds"] + PROBE_SECONDS + 30)
    require(start <= now_utc and now_utc + needed < end, "insufficient admitted serving deadline for the two-TTL native launch margin")
    guard = value["stop_guard"]
    require(guard["armed"] is True and guard["app_id"] == value["app_id"], "actual independent owned-app stop guard is not armed")
    require(utc_instant(guard["deadline"]) <= end, "stop guard exceeds serving expiry")
    for evidence in (value["deployment"], guard["evidence"], value["readiness"]["response"]):
        checked_ref(evidence)
    readiness = value["readiness"]
    require(readiness["authenticated"] is True and readiness["method"] == "GET" and readiness["path"] == "/v1/models" and readiness["status_code"] == 200 and readiness["upstream"] == value["upstream"], "actual authenticated BASE readiness evidence is missing")
    require(start <= utc_instant(readiness["checked_at"]) <= now_utc, "readiness timestamp is not in this actual serving window")
    models = bound_json(readiness["response"])
    require(RETURNED_MODEL in {row.get("id") for row in models.get("data", [])}, "actual model listing lacks the exact base ID")
    binding = value["authenticated_binding"]
    same(binding["requested_model"], RETURNED_MODEL, "authenticated upstream readiness requested another actual model (the selfhosted prefix is only a native route selector)")
    same(binding["returned_model"], RETURNED_MODEL, "readiness returned another model")
    same(value["budget"]["envelope"], ref(envelope_path), "serving/native drivers do not share the same admitted parent envelope")
    return {**value, "receipt": receipt}


def score(result: dict[str, Any]) -> float | None:
    value = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    return float(value) if type(value) in (int, float) and value in (0, 1) else None


def observation(cell: dict[str, Any], spec: Any, plan: dict[str, Any], queue: Any) -> dict[str, Any]:
    task = plan["tasks"][cell["task_index"]]
    directory = ROOT / spec.jobs_dir / spec.name
    record = {"schema": "mimo.phase2-native-execution/v1", "observed_at": now(), **cell,
              "proof_kind": "fresh_native_repaired", "model": BASE_MODEL, "package": task,
              "controls": plan["controls"], "original_v6_grade": "not_requested_unmeasured",
              "status": "native_no_terminal_trial", "execution_id": None, "native_job_id": None,
              "repaired_reward": None, "native_exception": None, "artifacts": {}, "problems": [], "training_admitted": False}
    try:
        record["queue_state"] = queue.locate(spec.spec_id).parent.name
    except FileNotFoundError:
        record["queue_state"] = None
    if directory.is_dir():
        record["artifacts"] = {str(path.relative_to(directory)): ref(path) for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink()}
    trials = sorted(directory.glob("*/result.json"))
    if len(trials) != 1:
        record["problems"].append("native job did not retain exactly one actual trial result")
        return record
    trial = trials[0].parent
    result = read_json(trials[0])
    record.update(execution_id=str(UUID(result["id"])), native_job_id=(result.get("config") or {}).get("job_id"),
                  trial_path=str(trial.resolve()), result=ref(trials[0]), native_exception=result.get("exception_info"),
                  native_repaired_reward=score(result), phases={key: result.get(key) for key in ("environment_setup", "agent_setup", "agent_execution", "verifier")},
                  started_at=result.get("started_at"), finished_at=result.get("finished_at"))
    for key, relative in {"trajectory": "agent/trajectory.json", "native_trajectory": "agent/mimoagent/native-trajectory.json",
                          "model_calls": "agent/mimoagent/model-calls.jsonl", "full_diff": "verifier/agent.full.diff", "kept_diff": "verifier/agent.kept.diff",
                          "workspace_artifacts": "artifacts/manifest.json", "lock": "lock.json", "effective_locks": "egress-lock.json",
                          "fingerprint": "setup-fingerprint.json", "verifier_stdout": "verifier/test_output.log", "reward_file": "verifier/reward.txt"}.items():
        if (trial / relative).is_file():
            record[key] = ref(trial / relative)
    metadata_path = directory / "lab-metadata.json"
    if metadata_path.is_file():
        metadata = read_json(metadata_path)
        record.update(metadata=ref(metadata_path), task_staging=metadata.get("task_staging"), model_identity=metadata.get("model_identity"),
                      provider_calls=(metadata.get("provider_usage") or {}).get("calls"))
    if record.get("effective_locks"):
        record["ordered_effective_lock_applications"] = bound_json(record["effective_locks"]).get("applications")
    if result.get("exception_info") is not None or not result.get("finished_at"):
        record["status"] = "native_error_or_incomplete"
    elif score(result) is None:
        record["status"] = "native_grade_missing_or_nonbinary"
    else:
        record.update(status="observed_repaired_pass" if score(result) == 1 else "observed_repaired_fail", repaired_reward=score(result))
    return record


def run(plan_path: Path, envelope_path: Path, serving_window: Path) -> dict[str, Any]:
    native, queue_module, _ = dependencies()
    plan, envelope, section, specs = preflight(plan_path, envelope_path)
    require(bool(os.environ.get("MIMO_SELFHOSTED_API_KEY")) and os.environ["MIMO_SELFHOSTED_API_KEY"] != native.MIMO_SELFHOSTED_PROXY_TOKEN, "parent-authenticated MIMO_SELFHOSTED_API_KEY is required; no fallback credentials")
    require(os.environ.get("EVALLAB_NO_PROCESS_JOB") != "1" and os.environ.get("EVALLAB_MODEL_CAPTURE") != "1", "native ingestion/custody cannot be bypassed or replaced by model capture")
    budget = NativeBudget(envelope_path, envelope, section)
    same(budget.read()["executions"], 0, "this exact76 plan cannot resume or reset an already consumed envelope")
    out = plan_path.parent
    require(not (out / "executions.jsonl").exists(), "execution output already exists; no implicit retry or overwrite")
    window = checked_window(serving_window, envelope_path, plan)
    queue = queue_module.DirectoryQueue(out / "queue")
    executor = queue_module.Executor(repo_root=ROOT, queue=queue, policy=native.load_policy(ROOT / "policy/standing-approvals.yaml"),
                                    parallel=plan["config"]["concurrency"], max_transient_retries=0,
                                    selfhosted_warmup_seconds=1.0, selfhosted_probe_timeout_seconds=PROBE_SECONDS)
    rows = []
    # A native-only lock does not steal the serving endpoint from CheatBench.
    with budget.path.with_suffix(".dispatcher.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal("the parent native envelope already has an active dispatcher") from exc
        with helpers.process_upstream(window["upstream"]):
            stop = False
            width = plan["config"]["concurrency"]
            for offset in range(0, CELLS, width):
                wave = list(zip(plan["cells"][offset:offset + width], specs[offset:offset + width], strict=True))
                if stop:
                    break
                charges, dispatch_error, dispatched = {}, None, 0
                try:
                    window = checked_window(serving_window, envelope_path, plan, expected=window["receipt"])
                    checked_envelope(envelope_path, plan_path, plan)
                    checked_plan(plan_path)
                    for cell, spec in wave:
                        native.validate_request(queue_module.Executor.prepare_request(spec, repo_root=ROOT), repo_root=ROOT)
                        charges[cell["cell_id"]] = budget.charge(cell, window["receipt"])
                        submitted, decision = executor.submit(spec)
                        same(queue.load(submitted).spec_id, spec.spec_id, "native queue changed a cell identity")
                        require(not decision.admitted and decision.reason_code == "paid_run_unauthorized", "only exact parent per-spec approval can authorize this paid native request")
                        queue.approve(spec.spec_id, actor=envelope["approval"])
                    checked_window(serving_window, envelope_path, plan, expected=window["receipt"])
                    checked_envelope(envelope_path, plan_path, plan)
                    append_json(out / "dispatch-journal.jsonl", {"at": now(), "event": "native_tick_requested", "spec_ids": [spec.spec_id for _, spec in wave], "envelope": ref(envelope_path)})
                    dispatched = executor.tick(parallel=width, spec_ids=[spec.spec_id for _, spec in wave])
                except (Exception, KeyboardInterrupt) as exc:
                    dispatch_error = failure(exc)
                for cell, spec in wave:
                    try:
                        queued = queue.locate(spec.spec_id)
                        if queued.parent.name in {"pending", "proposed", "waiting", "approved"}:
                            queue.reject(spec.spec_id, actor=envelope["approval"], message="single-shot fresh dispatch ended; no later implicit run is authorized")
                    except FileNotFoundError:
                        pass
                    try:
                        row = observation(cell, spec, plan, queue)
                    except Exception as exc:
                        row = {"schema": "mimo.phase2-native-execution/v1", **cell, "status": "native_observation_unavailable", "execution_id": None,
                               "native_job_id": None, "repaired_reward": None, "problems": [failure(exc)], "original_v6_grade": "not_requested_unmeasured"}
                    row.update(reservation=charges.get(cell["cell_id"]), serving_window=window["receipt"], envelope=ref(envelope_path),
                               wave_native_dispatches=dispatched, dispatch_error=dispatch_error, native_tick_reason=executor.last_tick_reason)
                    append_json(out / "executions.jsonl", row)
                    rows.append(row)
                stop = dispatch_error is not None or dispatched != len(wave)
            observed = {row["cell_id"] for row in rows}
            for cell in plan["cells"]:
                if cell["cell_id"] not in observed:
                    row = {"schema": "mimo.phase2-native-execution/v1", **cell, "status": "not_dispatched", "execution_id": None, "native_job_id": None,
                           "repaired_reward": None, "reservation": None, "native_exception": None, "original_v6_grade": "not_requested_unmeasured",
                           "problems": ["no native dispatch after the batch stopped; no trial or grade is fabricated"]}
                    append_json(out / "executions.jsonl", row)
                    rows.append(row)
    summary = {"schema": "mimo.phase2-native-run/v1", "at": now(), "plan": ref(plan_path), "envelope": ref(envelope_path), "serving_window": window["receipt"],
               "executions": ref(out / "executions.jsonl"), "planned_cells": CELLS, "actual_native_trial_endpoints": sum(row.get("execution_id") is not None for row in rows),
               "durable_native_gate": ref(budget.path), "native_charged_executions": budget.read()["executions"], "training_admitted": False}
    write_json(out / "run.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--out", type=Path, required=True)
    for command in ("admit", "run", "collect"):
        child = sub.add_parser(command)
        child.add_argument("--plan", type=Path, required=True)
        if command in {"admit", "run"}:
            child.add_argument("--envelope", type=Path, required=True)
        if command == "run":
            child.add_argument("--serving-window", type=Path, required=True)
        if command == "collect":
            child.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = create_plan(safe_path(args.config), safe_path(args.out))
            output = {"plan": ref(safe_path(args.out) / "plan.json"), "cells": len(result["cells"]), "cost_model": result["cost_model"], "provider_calls": 0}
        elif args.command == "admit":
            plan, envelope, _, _ = preflight(safe_path(args.plan), safe_path(args.envelope))
            NativeBudget(safe_path(args.envelope), envelope, envelope_sections(envelope)["native"])
            output = {"admitted": True, "plan": ref(args.plan), "envelope": ref(args.envelope), "cells": len(plan["cells"]), "provider_calls": 0, "training_admitted": False}
        elif args.command == "run":
            output = run(safe_path(args.plan), safe_path(args.envelope), safe_path(args.serving_window))
        else:
            dependencies()
            admission = load_module("phase2_fresh_native_admission", Path(__file__).with_name("fresh_native_admission.py"))
            output = admission.collect(safe_path(args.plan), safe_path(args.out), sys.modules[__name__])
        print(json.dumps(output, sort_keys=True, ensure_ascii=False))
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "refused", "error": failure(exc), "provider_calls_not_inferred": True}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
