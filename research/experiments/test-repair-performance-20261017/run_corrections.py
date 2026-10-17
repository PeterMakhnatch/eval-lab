#!/usr/bin/env python3
"""Plan ($0), or explicitly dispatch single-shot corrections through the native queue.

From the worktree root, a free plan needs only --requests and a NEW --out::

    uv run --no-sync python research/experiments/test-repair-performance-20261017/run_corrections.py \
      --requests runs/envcheck-20261017/m2/correction-inputs-v1/requests.jsonl \
      --request-id REQUEST_ID --out runs/envcheck-20261017/correction-plan-v1

An actual parent-authorized invocation additionally needs ALL of::

    keys run -- uv run --no-sync python research/experiments/test-repair-performance-20261017/run_corrections.py \
      --requests REQUESTS_JSONL --request-id REQUEST_ID \
      --execute --budget runs/envcheck-20261017/model-native-reservations-v2.json \
      --max-usd 0.30 --reserve-usd 0.10 --approval 'PARENT_RECORDED_APPROVAL' \
      --serving-window runs/envcheck-20261017/model-window-v4/window.json \
      --out runs/envcheck-20261017/correction-execution-v1

--evaluation-only admits ONLY evaluation_corrections.py's explicit heldout
requests; otherwise ONLY correction_data.py's nonheldout training requests are
admitted. Repeat --requests/--request-id to select disjoint inputs. Executing
more than one selection requires --batch. There is no retry/resume/reuse switch.
A reservation permanently consumes this lane's one correction per source+grader,
including a refused dispatch or an incomplete native run. Never reset its gate.

The reduced correction budget is 180s, 30 requests and 120000 cumulative input
tokens. Native Xiaomi deliberately sends no output cap: each omitted max_tokens
reserves the served 262144-token context, never a clamped per-request cap.
The cumulative output/total reservations therefore cover all 30 native requests;
the unchanged time/request fences and separately guarded Modal allocation bound
compute. Self-hosted token prices are zero; the $0.10 proxy cost ceiling is not a
GPU allowance. This is NOT the 1800s baseline or a matched-runtime comparison.
Native Xiaomi sampling/context/parsers are unchanged. The native sandbox app is
mimo-clean-census-envcheck-20261017, CPU/RAM enforcement is limit, and TTL is
360s (the native watchdog must independently admit it). TTL can interrupt setup
or grading; neither a timeout nor the original weak grader is repaired success.

The parent must separately record an ACTUAL envcheck.modal-serving-window/v1
JSON receipt. This driver never manufactures it, deploys/stops an app, probes an
API directly, changes global routes, or records credentials. Required fields:

* schema, window_id, owner, state='active', app_name='envcheck-correction9b-20261017',
  app_id, starts_at, expires_at (explicit UTC, maximum 650s), upstream (HTTPS
  Modal origin without /v1, authentication, query or fragment), allowed_models
  (a nonempty subset of the exact BASE_MODEL/B_MODEL constants below).
* deployment: {path, sha256}, the actual parent deployment evidence.
* stop_guard: {armed:true, app_id, deadline:<expires_at>, evidence:{path,sha256}},
  binding the parent's independently armed owned-app stop guard.
* readiness: {authenticated:true, method:'GET', path:'/v1/models',
  status_code:200, checked_at, upstream, response:{path,sha256}}. response is the
  actual JSON response body, WITHOUT request headers/key; its data[].id must
  include the native base model ID. The native executor still performs its own
  fresh, bounded readiness probe; its serving exposure belongs to this window.
* server: {model_revision, sglang_image, context_length:262144,
  tool_call_parser:'qwen3_coder', reasoning_parser:'mimo',
  sampling:{temperature:1.0,top_p:0.95,top_k:20}}. These must equal the existing
  native setup, not guessed defaults. Deployment evidence, not /v1/models alone,
  owns the declared server/checkpoint mapping; this is not remote weight attestation.
* model_bindings: BASE_MODEL -> {model_revision}; B_MODEL ->
  {adapter:'rds1-b-v4e4/adapter', base_revision, adapter_model_sha256,
  training_receipt:{path,sha256[,bytes]}}. Include every allowed selector. B must
  match the fixed M3 plan's retained training checkpoint exactly; never base/B
  substitution. /v1/models need not advertise every mounted LoRA alias.
* budget: {path, approval, cap_usd:0.30, usd_per_execution:0.10,
  global_ledger:{path,sha256}, serving_reservation_id, native_reservation_id}.
  path is the existing shared native CloudSpendGate JSON, not a new allowance.
  Its executions/reserved_usd/charges are updated durably before each native
  submission/tick. The GPU reservation NEVER becomes a fake native execution.

The parent's SHA-bound global ledger has schema=envcheck.global-exposure-ledger/v1,
approval, cap_usd=4, total_exposure_usd, and reservations[]. Each reservation has
reservation_id, kind, reserved_usd (nonnegative, no refund/reset entries). Their
sum must equal total_exposure_usd <=4. The referenced serving row has
kind='modal_serving_window', reserved_usd=0.60, window_id, app_name, app_id. The
referenced native row has kind='native_corrections', reserved_usd=0.30,
budget_path=<the exact shared native gate>. Other rows retain the parent's
historical/image/drain/grading exposure. The window, ledger and evidence remain
SHA-bound throughout dispatch; changed or expired evidence fails closed.

Only plan.json and real planned ExperimentSpecs are written by a free plan.
Execution additionally retains native queue events/specs, an fsynced local
charge journal, executions.jsonl (observed dispositions, never canonical regrade
receipts), and execution-summary.json. Actual job/trial IDs, result/trajectory
refs and input hashes are read from native evidence, never synthesized. Training
jobs run the repaired package and bind its actual final native result; heldout
jobs run the ORIGINAL package and explicitly await a separate real repaired
regrade by the parent. This is not a new executor, canonical-receipt generator,
or training exporter: use correction_data.py export on actual eligible native
trajectories and its required real repaired receipts. An empty export is not
created or called delivery here.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
LANE = ROOT / "runs/envcheck-20261017"
BASE_MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
B_MODEL = BASE_MODEL + ":rds1-b"
ALLOWED_MODELS = frozenset({BASE_MODEL, B_MODEL})
SERVING_APP = "envcheck-correction9b-20261017"
MODAL = {"modal_app_name": "mimo-clean-census-envcheck-20261017",
         "modal_resource_policy": "limit", "modal_sandbox_timeout_seconds": 360}
# Match the real native-no-cap proxy's context-sized reservation on every call.
# A 6000-token cumulative ceiling refuses the FIRST request before any generation.
RUNTIME = {"timeout_seconds": 180, "max_requests": 30, "max_input_tokens": 120000,
           "max_output_tokens": 30 * 262144, "max_total_tokens": 120000 + 30 * 262144,
           "cost_limit_usd": 0.10}
TRAINING_REFERENCE_PROFILE = "xiaomi-mimo-envcheck-feedback-20261017"
NATIVE_CAP_USD = 0.30
NATIVE_RESERVE_USD = 0.10
SERVING_RESERVE_USD = 0.60
WINDOW_MAX_SECONDS = 650
PROBE_TIMEOUT_SECONDS = 10.0
NOTICE = ("Reduced 180s reset-context correction, not matched to the 1800s baseline. "
          "Native incomplete/infra/model-budget stops are not repaired success. "
          "Heldout original-grader scores are not final repaired-grader scores. "
          "Reservations are exposure estimates, not invoices or an account-dollar kill switch.")


class Refusal(ValueError):
    """A local, credential-free fail-closed diagnostic."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise Refusal(message)


def same(actual: Any, expected: Any, message: str) -> None:
    require(actual == expected, message)


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def ref(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": digest(path.read_bytes())}


def checked_ref(value: dict[str, Any]) -> None:
    same(ref(Path(value["path"])), {key: value[key] for key in ("path", "sha256")},
         "SHA-bound file changed or disappeared")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, value: Any, *, replace: bool = False) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".correction-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    fsync_directory(path.parent)


def append_json(path: Path, value: Any) -> None:
    with path.open("ab") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def failure(exc: BaseException) -> dict[str, str]:
    # Provider/native exceptions can contain bodies or headers. Never copy them.
    result = {"type": type(exc).__name__}
    if isinstance(exc, Refusal):
        result["reason"] = str(exc)
    code = getattr(exc, "reason_code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_:.-]+", code):
        result["reason_code"] = code
    return result


def safe_path(path: Path) -> Path:
    raw = path.expanduser().absolute()
    require(not raw.is_symlink() and not any(parent.is_symlink() for parent in raw.parents),
            "owned operational paths cannot contain symlinks")
    return raw.resolve()


def utc_instant(value: Any) -> datetime:
    require(isinstance(value, str), "window timestamps must be explicit UTC strings")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refusal("invalid window UTC timestamp") from exc
    require(result.tzinfo is not None and result.utcoffset() == timedelta(0),
            "window timestamps must carry the UTC offset")
    return result.astimezone(UTC)


def money(value: Any) -> float:
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            "exposure entries must be finite nonnegative USD amounts")
    return float(value)


def equal_money(actual: Any, expected: float, message: str) -> None:
    require(math.isclose(money(actual), expected, rel_tol=0, abs_tol=1e-9), message)


def dependencies() -> tuple[Any, Any, Any, Any, Any, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    import correction_data as correction
    import evaluation_corrections as evaluation
    import partition

    from evallab import execution_contracts, queue, schemas

    same(partition.REPO, ROOT, "request helpers loaded from a different worktree")
    for module in (execution_contracts, queue, schemas):
        require(Path(module.__file__).resolve().is_relative_to(ROOT / "src/evallab"),
                "native modules loaded from a different checkout")
    missing = sorted(set(MODAL) - set(schemas.ExperimentSpec.model_fields))
    require(not missing, "native ExperimentSpec does not declare all three Modal operational fields")
    return partition, correction, evaluation, execution_contracts, queue, schemas


@dataclass
class Selection:
    request: dict[str, Any]
    input: dict[str, Any]
    row_index: int
    row_sha256: str

    def check(self) -> None:
        checked_ref(self.input)
        same(digest(canonical(self.request)), self.row_sha256, "selected request row changed")


def selections(args: argparse.Namespace) -> list[Selection]:
    selected = []
    ids = set(args.request_id or [])
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for supplied in args.requests:
        path = safe_path(supplied)
        require(path.is_relative_to(ROOT / "runs"), "request inputs must be retained under this worktree's runs/")
        require(not args.out.is_relative_to(path.parent), "new output cannot be inside immutable request inputs")
        binding = ref(path)
        if path.suffix == ".json":
            rows = [json.loads(path.read_text(encoding="utf-8"))]
            if isinstance(rows[0], dict) and "request" not in rows[0]:
                rows[0] = {**rows[0], "request": binding}
        else:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        checked_ref(binding)
        for index, request in enumerate(rows):
            require(isinstance(request, dict), "request input must contain JSON objects")
            ident = request.get("request_id")
            if ids and ident not in ids:
                continue
            require(isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{64}", ident), "invalid correction request ID")
            require(ident not in seen_ids, "duplicate selected request ID; aliases are not more corrections")
            pair = (str(UUID(request["source_execution_id"])), request["source_partition"]["variant"]["repaired_digest"])
            require(pair not in seen_pairs, "at most one correction per source execution and repaired grader")
            seen_ids.add(ident)
            seen_pairs.add(pair)
            selected.append(Selection(request, binding, index, digest(canonical(request))))
    require(bool(selected), "no selected correction requests; no empty execution/export is delivery")
    require(not ids - seen_ids, "a selected request ID is absent from the supplied immutable inputs")
    require(not args.execute or len(selected) == 1 or args.batch,
            "executing multiple requests requires explicit --batch")
    return selected


def checked_context(selection: Selection, args: argparse.Namespace, p: Any, c: Any, e: Any) -> dict[str, Any]:
    """Use the existing request validators, both before generation and dispatch."""
    from evallab.regrade import verifier_identity
    from evallab.task_variants import task_directory_digest

    selection.check()
    request = selection.request
    same(request.get("schema"), "envcheck.correction-request/v1", "unsupported correction request schema")
    same(request.get("protocol"), c.PROTOCOL, "only bounded reset-context feedback is admitted")
    require(request.get("model") in ALLOWED_MODELS, "student selector must be exact base9B or exact rds1-b; no substitute")
    heldout, heldout_ref = p.frozen_heldout(args.heldout_manifest)
    if args.evaluation_only:
        same(request.get("purpose"), "evaluation", "--evaluation-only cannot execute training requests")
        e.checked_request(request)
        same(request.get("heldout"), True, "heldout evaluation flag changed")
        same(request.get("training_allowed"), False, "heldout evaluation must explicitly forbid training")
    else:
        c.checked_request(request, heldout, heldout_ref)
        same(request.get("heldout"), False, "training request cannot carry a heldout flag")
        require(p.parent_task(request["task"]) not in heldout, "all33 original parents are excluded from training")
    same(request["heldout_manifest"], heldout_ref, "request changed frozen all33 authority")
    source = c.checked_source(request["source_partition"])
    same(source["execution_id"], request["source_execution_id"], "request/source execution mismatch")
    same(source["model"], request["model"], "correction must keep the source's exact model selector")
    same(source["task"], request["task"], "correction must keep the original canonical parent task")
    flags = request["source_partition"].get("flags") or {}
    require(not any(flags.get(key) for key in ("teacher", "copied_fix", "alias_conflicts", "attempt_conflict")),
            "teacher/copied/conflicting source flags forbid correction generation; flags remain evidence")
    pin = request["source_partition"]["variant"]
    require(p.mechanical_grader_admitted(pin), "repaired grader is not mechanically admitted")
    if not args.evaluation_only:
        row = request["source_partition"]
        require(row.get("partition") == "false_pass" and row.get("status") == "measured"
                and row.get("original_reward") == 1.0 and row.get("repaired_reward") == 0.0,
                "training corrections require an actually measured original1/repaired0 false pass")
        manifest_row = pin["manifest_row"]
        require(not any(manifest_row.get(key) for key in ("training_excluded", "training_exclusion", "excluded_from_training"))
                and manifest_row.get("training_eligible") is not False, "manifest excludes this source from training")
        evidence = row["provenance"]["selected_receipt"]
        c.checked_evidence(evidence)
        require(not p.validate_receipt(row, pin, evidence["row"]), "training baseline repaired receipt no longer validates")
    meta = request["provenance"]["correction"]
    same(meta.get("allowed_model_inputs"), ["task_prompt", "source_patch", "repaired_failure_stdout"],
         "correction changed the bounded model-input policy")
    for name in ("hidden_test_source_included", "reference_source_included", "full_historical_context_included"):
        same(meta.get(name), False, "correction must explicitly exclude hidden/reference source and historical context")
    snapshot = meta["task_package_snapshot"]
    repaired = meta["repaired_task_package_snapshot"] if args.evaluation_only else snapshot
    for package in (snapshot, repaired):
        directory = safe_path(Path(package["path"]))
        require(directory.is_relative_to(ROOT / "runs"), "native model task snapshot must stay under this worktree's runs/")
        require(not any(path.is_symlink() for path in directory.rglob("*")), "task snapshot contains symlinks")
        same(task_directory_digest(directory), package["sha256"], "exact request package snapshot changed")
    same(repaired["sha256"], pin["repaired_digest"], "final repaired grader snapshot differs from the request pin")
    fields_bound = {
        "task": request["task"], "task_path": Path(snapshot["path"]).relative_to(ROOT).as_posix(),
        "task_package_digest": snapshot["sha256"], "agent": "mimoagent", "model": request["model"],
        "extra_instruction_path": Path(meta["extra_instruction"]["path"]).relative_to(ROOT).as_posix(),
        "extra_instruction_sha256": meta["extra_instruction"]["sha256"],
    }
    for key, value in fields_bound.items():
        same(request["native_spec_fields"].get(key), value, "native fields lost exact task/model/input-byte custody")
    if not args.evaluation_only:
        same(request["native_spec_fields"], fields_bound, "training request contains unrecognized native spec overrides")
    same((Path(snapshot["path"]) / "instruction.md").read_bytes(), Path(meta["task_prompt"]["path"]).read_bytes(),
         "snapshot instruction bytes differ from the bound original task input")
    original = {"source_execution_id": source["execution_id"], "source_trial": source["source_trial"],
                "package_digest": source["alias"].get("executed_task_digest"),
                "task_checksum": source["alias"].get("task_checksum"),
                "native_files": source["alias"]["files"], "identity_basis": "retained native original-grader evidence"}
    if args.evaluation_only:
        original.update(package=snapshot, verifier=verifier_identity(Path(snapshot["path"])).model_dump(mode="json"))
    return {"source": source, "original_grader": original,
            "repaired_grader": {"package": repaired,
                "verifier": verifier_identity(Path(repaired["path"])).model_dump(mode="json"),
                "variant": pin}, "executed_package": snapshot}


@dataclass
class Prepared:
    selection: Selection
    context: dict[str, Any]
    spec: Any
    fingerprint: dict[str, Any]
    changes: list[dict[str, Any]]

    def plan(self) -> dict[str, Any]:
        request = self.selection.request
        return {"request_id": request["request_id"], "request": request["request"],
                "input_file": self.selection.input, "row_index": self.selection.row_index,
                "row_sha256": self.selection.row_sha256, "source_execution_id": request["source_execution_id"],
                "task": request["task"], "model": request["model"], "purpose": request["purpose"],
                "heldout": request["heldout"], "training_allowed": not request["heldout"],
                "heldout_manifest": request["heldout_manifest"],
                "spec_id": self.spec.spec_id, "job_name": self.spec.name,
                "native_spec_fields": request["native_spec_fields"],
                "original_grader": self.context["original_grader"], "repaired_grader": self.context["repaired_grader"],
                "executed_package": self.context["executed_package"], "source_flags": request["source_partition"].get("flags"),
                "correction": request["provenance"]["correction"],
                "required_native_settings": request.get("required_native_settings"),
                "required_native_settings_digest": request.get("required_native_settings_digest"),
                "declared_runtime_changes": self.changes, "baseline_runtime_matched": False,
                "intended_fingerprint": self.fingerprint, "execution_status": "planned_not_executed"}


def prepare(selection: Selection, args: argparse.Namespace, modules: tuple[Any, ...]) -> Prepared:
    p, c, e, native, queue, schemas = modules
    from evallab.mimoagent_worker import WRAPPER_ADDITIONS
    from evallab.registry import compute_task_digests
    from evallab.setup_fingerprint import build_intended_fingerprint

    context = checked_context(selection, args, p, c, e)
    request = selection.request
    inherited = dict(request["native_spec_fields"])
    require(inherited.get("reference_profile") in (None, "xiaomi-mimo-rl"), "source requires an unsupported named setup profile")
    identifier = native.new_ulid()
    values = {**inherited, **RUNTIME, **MODAL, "spec_id": identifier,
              "name": "envcheck-correction-" + identifier.lower(),
              "hypothesis": "Single same-model source-free reset-context correction; reduced 180s runtime, not a matched 1800s baseline.",
              "purpose": "elicitation", "question_ref": request["request"]["path"],
              "reference_profile": "xiaomi-mimo-rl" if args.evaluation_only else TRAINING_REFERENCE_PROFILE,
              "environment": "modal", "egress_lock": True,
              "jobs_dir": "runs", "attempts": 1, "concurrency": 1, "diff_sources": [],
              "submitted_by": "envcheck-correction-parent", "est_cost_usd": NATIVE_RESERVE_USD,
              "task_id": p.parent_task(request["task"]).removeprefix("mimo-v2.6-rl/"),
              "verifier_digest": compute_task_digests(Path(context["executed_package"]["path"])).verifier}
    draft = schemas.ExperimentSpec.model_validate(values)
    fingerprint = build_intended_fingerprint(spec=draft, task_dir=Path(context["executed_package"]["path"]),
        model=draft.model, agent=draft.agent, environment=draft.environment, repo_root=ROOT)
    additions = {"extra_instruction": draft.extra_instruction_path, **WRAPPER_ADDITIONS}
    same(fingerprint["harness"]["additions"], additions, "native harness has unexpected prompt/tool additions")
    deviations = [item for item in inherited.get("deviations", [])
                  if item["field"] not in {"harness.additions", "budgets.max_requests"}]
    deviations.extend([
        {"field": "harness.additions", "value": additions,
         "reason": "Existing native wrapper additions plus this SHA-bound source-free reset-context extra instruction; no restored history."},
        {"field": "budgets.max_requests", "value": 30,
         "reason": "Parent explicitly authorizes the reduced correction budget: 30 requests and 180s, not the 1800s baseline."},
    ])
    values["deviations"] = deviations
    spec = schemas.ExperimentSpec.model_validate(values)
    native_request = queue.Executor.prepare_request(spec, repo_root=ROOT)
    declared = {field.name for field in fields(native_request)}
    require(set(MODAL).issubset(declared), "native RunRequest does not declare the Modal operational fields")
    for key, value in {**MODAL, **RUNTIME}.items():
        same(getattr(native_request, key, None), value, "typed native request propagation lost operational/runtime bounds")
    same(native_request.model, request["model"], "native queue silently changed the exact model selector")
    same(native_request.task.resolve(), Path(context["executed_package"]["path"]).resolve(), "native queue changed the task package")
    same(ref(native_request.extra_instruction_path), request["provenance"]["correction"]["extra_instruction"],
         "native queue changed extra-instruction bytes")
    require(MODAL["modal_sandbox_timeout_seconds"] <= native_request.trial_watchdog_seconds,
            "provider TTL exceeds the real native trial watchdog")
    native.validate_request(native_request, repo_root=ROOT)
    fingerprint = build_intended_fingerprint(spec=spec, task_dir=native_request.task,
        model=spec.model, agent=spec.agent, environment=spec.environment, repo_root=ROOT, request=native_request)
    same(fingerprint["sampling"]["temperature"], 1.0, "native Xiaomi temperature changed")
    same(fingerprint["sampling"]["top_p"], 0.95, "native Xiaomi top_p changed")
    same(fingerprint["sampling"]["top_k"], 20, "native Xiaomi top_k changed")
    same(fingerprint["server"]["context_length"], 262144, "native Xiaomi context changed")
    same(fingerprint["server"]["tool_call_parser"], "qwen3_coder", "native Xiaomi tool parser changed")
    same(fingerprint["server"]["reasoning_parser"], "mimo", "native Xiaomi reasoning parser changed")
    changes = [{"field": key, "request_value": inherited.get(key), "correction_value": value,
                "reason": "parent-explicit reduced native correction protocol, not matched baseline runtime"}
               for key, value in {**RUNTIME, **MODAL, "environment": "modal", "egress_lock": True}.items()
               if inherited.get(key) != value]
    require(not (ROOT / "runs" / spec.name).exists(), "native job output already exists; implicit reuse is forbidden")
    return Prepared(selection, context, spec, fingerprint, changes)


class NativeBudget:
    """The parent's existing fixed-price native subgate, never the GPU allowance."""

    def __init__(self, args: argparse.Namespace):
        from evallab.regrade import CloudSpendGate

        self.path = args.budget
        self.native = CloudSpendGate(args.approval, args.max_usd, args.reserve_usd)
        self.journal = args.out / "charge-journal.jsonl"
        self.charged = 0
        self.initial = self.read()

    def closed_zero_completion_ordinals(self, state: dict[str, Any]) -> set[int]:
        """Honor only parent-explicit, SHA-bound pre-generation failure closures.

        The reservation remains charged. This does not retry anything or discard
        a sampled answer: a real assistant turn or nonzero native usage refuses
        closure. The parent must fund the additional cumulative dispatch first.
        """
        closed: set[int] = set()
        charges = state.get("charges", [])
        for binding in state.get("closed_zero_completion_reservations", []):
            ordinal = binding["ordinal"]
            require(type(ordinal) is int and 1 <= ordinal <= len(charges) and ordinal not in closed,
                    "invalid or repeated explicit zero-completion reservation closure")
            checked_ref(binding["execution"])
            rows = [line for line in Path(binding["execution"]["path"]).read_text(encoding="utf-8").splitlines() if line.strip()]
            require(len(rows) == 1, "explicit recovery must bind one immutable native disposition")
            execution = json.loads(rows[0])
            event = charges[ordinal - 1]
            same(execution.get("reservation"), event, "closed native reservation differs from its actual disposition")
            for key in ("source_execution_id", "repaired_digest", "model", "purpose"):
                same(execution.get(key), event.get(key), "closed disposition belongs to another native request")
            same(execution.get("status"), "native_model_budget_stop", "only an actual pre-generation budget refusal may close")
            same(execution.get("native_exception_type"), "TrialBudgetExhaustedError", "closed attempt is not a native proxy budget refusal")
            require(bool(execution.get("native_finished_at")) and execution.get("corrected_patch") is None,
                    "closed attempt is unfinished or contains a candidate patch")
            same(execution.get("observed_model_requests"), 1, "closure must contain exactly one proxy-rejected request")
            checked_ref(binding["model_calls"])
            calls = [json.loads(line) for line in Path(binding["model_calls"]["path"]).read_text(encoding="utf-8").splitlines() if line.strip()]
            require(len(calls) == 1 and calls[0].get("response_status") == 429
                    and calls[0].get("usage") is None
                    and calls[0].get("proxy_budget_reason") == "trial budget exhausted",
                    "only the recorded first-request pre-upstream budget 429 may close")
            native = execution["native_files"]["native_trajectory"]
            checked_ref(native)
            doc = json.loads(Path(native["path"]).read_text(encoding="utf-8"))
            stats = doc["info"]["model_stats"]
            require(all(type(stats.get(key)) is int and stats[key] == 0 for key in
                        ("api_calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "total_tokens")),
                    "sampled model usage cannot be closed or erased")
            require(not any(message.get("role") == "assistant"
                            for trajectory in doc["trajs"].values() for message in trajectory["messages"]),
                    "a sampled assistant completion cannot become an infrastructure recovery")
            closed.add(ordinal)
        return closed

    def read(self) -> dict[str, Any]:
        require(self.path.is_file(), "parent must create the shared native budget; driver never starts a replacement cap")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        same(state.get("approval"), self.native.approval, "shared native gate approval differs from explicit parent approval")
        equal_money(state.get("cap_usd"), self.native.cap_usd, "shared native subcap changed")
        equal_money(state.get("usd_per_execution"), self.native.usd_per_execution, "shared native reservation unit changed")
        count = state.get("executions")
        require(type(count) is int and 0 <= count <= 3, "shared native gate has an invalid cumulative execution count")
        equal_money(state.get("reserved_usd"), round(count * self.native.usd_per_execution, 4), "shared native reservations disagree with its count")
        require(state["reserved_usd"] <= self.native.cap_usd + 1e-9, "shared native gate already exceeds its subcap")
        charges = state.get("charges", [])
        require(isinstance(charges, list) and len(charges) == count, "shared native charge journal is incomplete or inconsistent")
        closed = self.closed_zero_completion_ordinals(state)
        seen = set()
        for event in charges:
            require(isinstance(event, dict) and event.get("kind") == "native_correction",
                    "dedicated shared native subgate contains an unrecognized execution kind")
            if event.get("ordinal") in closed:
                continue
            pair = (event["source_execution_id"], event["repaired_digest"])
            require(pair not in seen, "shared journal already contains repeated source+grader corrections")
            seen.add(pair)
        self.native.executions = count
        return state

    def check(self, jobs: list[Prepared]) -> dict[str, Any]:
        state = self.read()
        closed = self.closed_zero_completion_ordinals(state)
        seen = {(event["source_execution_id"], event["repaired_digest"])
                for event in state.get("charges", [])
                if event.get("kind") == "native_correction" and event.get("ordinal") not in closed}
        for job in jobs:
            request = job.selection.request
            require((request["source_execution_id"], request["source_partition"]["variant"]["repaired_digest"]) not in seen,
                    "this source+grader already consumed its one native correction; no implicit retry")
        require((self.native.executions + len(jobs)) * self.native.usd_per_execution <= self.native.cap_usd + 1e-9,
                "selected native jobs do not fit the remaining shared subcap")
        return state

    def charge(self, job: Prepared, window: dict[str, Any]) -> dict[str, Any]:
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.check([job])
            self.native.charge()
            request = job.selection.request
            event = {"kind": "native_correction", "event": "reserved_before_native_submission",
                     "at": now(), "out": str(self.journal.parent), "ordinal": self.native.executions,
                     "source_execution_id": request["source_execution_id"],
                     "repaired_digest": request["source_partition"]["variant"]["repaired_digest"],
                     "request": request["request"], "request_id": request["request_id"],
                     "spec_id": job.spec.spec_id, "job_name": job.spec.name, "model": job.spec.model,
                     "purpose": request["purpose"], "window_id": window["window_id"],
                     "window_receipt": window["receipt"], "global_ledger": window["budget"]["global_ledger"],
                     "serving_reservation_id": window["budget"]["serving_reservation_id"],
                     "native_reservation_id": window["budget"]["native_reservation_id"],
                     "reserved_usd": self.native.usd_per_execution, "gate": self.native.to_json()}
            # Shared durable authority first. A crash here consumes the reservation;
            # it must never dispatch an unjournalled job or refund/retry implicitly.
            write_json(self.path, {**state, **self.native.to_json(), "charges": [*state.get("charges", []), event]}, replace=True)
            append_json(self.journal, event)
            self.charged += 1
            return event


def checked_window(path: Path, jobs: list[Prepared], budget: NativeBudget, e: Any,
                   expected: dict[str, Any] | None = None) -> dict[str, Any]:
    binding = ref(path)
    if expected is not None:
        same(binding, expected, "serving-window receipt changed during the single-shot invocation")
    window = json.loads(path.read_text(encoding="utf-8"))
    checked_ref(binding)
    same(window.get("schema"), "envcheck.modal-serving-window/v1", "unsupported parent serving-window receipt")
    for key in ("window_id", "owner", "app_id"):
        require(isinstance(window.get(key), str) and bool(window[key].strip()), "serving window lacks explicit identity/ownership")
    same(window.get("state"), "active", "parent has not recorded an active serving window")
    same(window.get("app_name"), SERVING_APP, "serving app is not the parent's exact owned correction app")
    start, end = utc_instant(window["starts_at"]), utc_instant(window["expires_at"])
    current = datetime.now(UTC)
    require(0 < (end - start).total_seconds() <= WINDOW_MAX_SECONDS, "serving window exceeds the parent's 650s fence")
    require(start <= current < end, "owned serving window has not started or has expired")
    require((end - current).total_seconds() >= RUNTIME["timeout_seconds"] + PROBE_TIMEOUT_SECONDS,
            "serving window lacks even one full reduced model-agent budget plus readiness time")
    upstream = window.get("upstream")
    require(isinstance(upstream, str), "serving window lacks its explicit process-scoped upstream")
    parsed = urlsplit(upstream)
    require(parsed.scheme == "https" and bool(parsed.hostname) and parsed.hostname.endswith((".modal.run", ".modal.direct"))
            and parsed.port is None and parsed.username is None and parsed.password is None
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
            "serving upstream must be an authenticated-by-key owned HTTPS Modal origin, never shared/local/fallback")
    require(upstream == upstream.rstrip("/").strip(), "record the canonical Modal origin without a trailing slash")
    allowed = window.get("allowed_models")
    require(isinstance(allowed, list) and bool(allowed) and len(set(allowed)) == len(allowed)
            and set(allowed).issubset(ALLOWED_MODELS), "serving allowlist contains unknown/substitute model selectors")
    require({job.spec.model for job in jobs}.issubset(allowed), "selected exact model is absent from this serving window")
    checked_ref(window["deployment"])
    guard = window["stop_guard"]
    same(guard.get("armed"), True, "parent's independent serving stop guard is not armed")
    same(guard.get("app_id"), window["app_id"], "stop guard targets another app")
    same(utc_instant(guard["deadline"]), end, "stop guard is not bound to this serving-window expiry")
    checked_ref(guard["evidence"])
    ready = window["readiness"]
    same(ready.get("authenticated"), True, "readiness was not authenticated by the parent")
    same((ready.get("method"), ready.get("path"), ready.get("status_code")), ("GET", "/v1/models", 200),
         "parent must retain actual successful authenticated /v1/models readiness")
    same(ready.get("upstream"), upstream, "readiness came from a different upstream")
    require(start <= utc_instant(ready["checked_at"]) <= current, "readiness is outside the actual active window")
    checked_ref(ready["response"])
    response = json.loads(Path(ready["response"]["path"]).read_text(encoding="utf-8"))
    require(isinstance(response, dict) and isinstance(response.get("data"), list), "readiness response is not an actual models-list object")
    require(BASE_MODEL.removeprefix("selfhosted/") in {row.get("id") for row in response["data"] if isinstance(row, dict)},
            "authenticated models response does not identify the actual native base9B")
    server = window["server"]
    for job in jobs:
        fingerprint = job.fingerprint
        for key in ("model_revision", "sglang_image", "context_length", "tool_call_parser", "reasoning_parser"):
            same(server.get(key), fingerprint["server"][key], "parent's declared serving setup differs from the real native setup")
        same(server.get("sampling"), {key: fingerprint["sampling"][key] for key in ("temperature", "top_p", "top_k")},
             "parent's serving recipe changed Xiaomi reference sampling")
    bindings = window["model_bindings"]
    require(set(bindings) == set(allowed), "each allowed selector needs one explicit checkpoint binding")
    if BASE_MODEL in allowed:
        same(bindings[BASE_MODEL].get("model_revision"), server["model_revision"], "base checkpoint binding changed")
    if B_MODEL in allowed:
        plan, _, _, _ = e.checked_plan()
        checkpoint = plan["model_checkpoint"]
        model = bindings[B_MODEL]
        for key in ("adapter", "base_revision", "adapter_model_sha256"):
            same(model.get(key), checkpoint[key], "rds1-b binding differs from the fixed retained B checkpoint")
        same({key: model["training_receipt"][key] for key in ("path", "sha256")},
             {key: checkpoint["training_receipt"][key] for key in ("path", "sha256")}, "B training-receipt custody changed")
        checked_ref(model["training_receipt"])
        same(model["base_revision"], server["model_revision"], "B adapter and served base revisions differ")
    grant = window["budget"]
    same(safe_path(Path(grant["path"])), budget.path, "window attempts to use another/new native subcap")
    same(grant.get("approval"), budget.native.approval, "window and shared native gate approvals differ")
    equal_money(grant.get("cap_usd"), NATIVE_CAP_USD, "window native subcap must be exactly $0.30")
    equal_money(grant.get("usd_per_execution"), NATIVE_RESERVE_USD, "window native reservation must be exactly $0.10")
    checked_ref(grant["global_ledger"])
    ledger = json.loads(Path(grant["global_ledger"]["path"]).read_text(encoding="utf-8"))
    same(ledger.get("schema"), "envcheck.global-exposure-ledger/v1", "parent's global exposure ledger has the wrong schema")
    same(ledger.get("approval"), budget.native.approval, "global ledger is not the same parent approval")
    equal_money(ledger.get("cap_usd"), 4.0, "global lane approval must retain its original total $4 cap")
    rows = ledger.get("reservations")
    require(isinstance(rows, list) and bool(rows) and all(isinstance(row, dict) for row in rows), "global exposure ledger has no real reservation rows")
    by_id = {row["reservation_id"]: row for row in rows}
    require(len(by_id) == len(rows), "global exposure ledger repeats reservation IDs")
    total = sum(money(row["reserved_usd"]) for row in rows)
    equal_money(ledger.get("total_exposure_usd"), total, "global ledger exposure disagrees with its retained reservations")
    require(total <= 4.0 + 1e-9, "global exposure already exceeds the parent total $4 lane")
    serving = by_id[grant["serving_reservation_id"]]
    same(serving.get("kind"), "modal_serving_window", "GPU reservation is not a separately recorded serving window")
    equal_money(serving.get("reserved_usd"), SERVING_RESERVE_USD, "parent must separately retain the $0.60 GPU/image/drain allowance")
    for key in ("window_id", "app_name", "app_id"):
        same(serving.get(key), window[key], "global serving reservation belongs to another window/app")
    native_row = by_id[grant["native_reservation_id"]]
    same(native_row.get("kind"), "native_corrections", "native reservation has the wrong exposure kind")
    equal_money(native_row.get("reserved_usd"), NATIVE_CAP_USD, "native corrections lost the parent $0.30 suballocation")
    same(safe_path(Path(native_row["budget_path"])), budget.path, "global native reservation names another/new gate")
    return {**window, "receipt": binding}


@contextlib.contextmanager
def process_upstream(upstream: str) -> Iterator[None]:
    name = "EVALLAB_MIMO_SELFHOSTED_UPSTREAM"
    previous = os.environ.get(name)
    os.environ[name] = upstream
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def native_observation(job: Prepared, queue: Any, p: Any, c: Any) -> dict[str, Any]:
    """Read only native artifacts; never create a result, patch or trajectory."""
    from evallab.registry import harbor_task_digest

    request = job.selection.request
    spec = job.spec
    queued_path = queue.locate(spec.spec_id)
    directory = ROOT / spec.jobs_dir / spec.name
    record = {"schema": "envcheck.native-correction-execution/v1", "observed_at": now(),
              "request_id": request["request_id"], "request": request["request"],
              "input_file": job.selection.input, "input_row_sha256": job.selection.row_sha256,
              "source_execution_id": request["source_execution_id"], "source_original_reward": request["source_partition"]["original_reward"],
              "task": request["task"], "model": request["model"], "purpose": request["purpose"],
              "heldout": request["heldout"], "training_allowed": not request["heldout"], "teacher": False,
              "heldout_manifest": request["heldout_manifest"],
              "protocol": request["protocol"], "partition_scope": request["partition_scope"],
              "repaired_digest": request["source_partition"]["variant"]["repaired_digest"],
              "source_flags": request["source_partition"].get("flags"), "spec_id": spec.spec_id,
              "queue_state": queued_path.parent.name, "queued_spec": ref(queued_path),
              "job_name": spec.name, "job_dir": str(directory), "native_files": {},
              "original_grader": job.context["original_grader"], "repaired_grader": job.context["repaired_grader"],
              "declared_runtime_changes": job.changes, "baseline_runtime_matched": False, "notice": NOTICE,
              "required_native_settings_digest": request.get("required_native_settings_digest"),
              "provenance": {"correction": request["provenance"]["correction"], "request": request["request"]},
              "repaired_reward": None, "final_repaired_result": None,
              "status": "native_no_terminal_trial", "problems": [], "training_exported": False}
    events = [event for event in queue.authorizations().values() if event.spec_id == spec.spec_id]
    record["native_authorization_recorded"] = bool(events)
    for name in ("result.json", "experiment-spec.json", "lab-metadata.json", "provenance.json", "setup-fingerprint.json"):
        path = directory / name
        if path.is_file():
            record["native_files"][name] = ref(path)
    results = sorted(directory.glob("*/result.json")) if directory.is_dir() else []
    if len(results) != 1:
        record["problems"].append("native job did not retain exactly one actual trial result")
        return record
    trial = results[0].parent
    raw = json.loads(results[0].read_text(encoding="utf-8"))
    record.update(execution_id=str(UUID(raw["id"])), native_trial_id=str(UUID(raw["id"])), source_trial=str(trial.resolve()),
                  native_job_id=(raw.get("config") or {}).get("job_id"), native_result=ref(results[0]))
    metadata = ((raw.get("agent_result") or {}).get("metadata") or {})
    exception = raw.get("exception_info") or {}
    record.update(native_exit_status=metadata.get("native_exit_status"), native_stop_reason=metadata.get("stop_reason"),
                  native_exception_type=exception.get("exception_type"), native_infra_error=bool(metadata.get("infra_error")),
                  observed_model_requests=metadata.get("model_requests"), native_finished_at=raw.get("finished_at"),
                  original_reward=p.score(raw))
    for key, relative in {"native_trajectory": "agent/mimoagent/native-trajectory.json", "trajectory": "agent/trajectory.json",
                          "corrected_patch": "verifier/agent.diff", "kept_patch": "verifier/agent.kept.diff",
                          "artifact_manifest": "artifacts/manifest.json", "lock": "lock.json",
                          "fingerprint": "setup-fingerprint.json", "verifier_stdout": "verifier/test_output.log"}.items():
        path = trial / relative
        if path.is_file():
            record["native_files"][key] = ref(path)
    try:
        source = p.source_record(trial)
        record.update(flags=source["flags"], native_provenance=source["alias"],
                      teacher=source["flags"].get("teacher"), copied_fix=source["flags"].get("copied_fix"))
        require(not source["flags"].get("teacher") and not source["flags"].get("copied_fix"),
                "actual native correction carries a teacher/copied-fix flag; preserve it, never count it as student success")
        same(source["task"], request["task"], "actual native correction changed original parent task")
        same(source["model"], request["model"], "actual native correction changed model selector")
        native_ref = source["alias"]["files"].get("native")
        require(native_ref is not None, "missing actual native correction conversation")
        messages, _ = p.native_messages(Path(native_ref["path"]))
        matched, boundary = c.match_request(source, [request], messages)
        same(matched["request_id"], request["request_id"], "actual native input does not match its correction request")
        same(source["alias"].get("executed_task_digest"), harbor_task_digest(Path(job.context["executed_package"]["path"])),
             "actual native lock does not bind the exact requested package")
        actual_spec_path = directory / "experiment-spec.json"
        require(actual_spec_path.is_file(), "native job did not retain its actual ExperimentSpec")
        actual_spec = json.loads(actual_spec_path.read_text(encoding="utf-8"))
        for key in ("spec_id", "task", "task_path", "task_package_digest", "extra_instruction_path", "extra_instruction_sha256",
                    "agent", "model", "reference_profile", "diff_sources", "attempts", "concurrency", "environment", "egress_lock", *RUNTIME, *MODAL):
            same(actual_spec.get(key), getattr(spec, key), "actual native ExperimentSpec differs from its declared input/runtime bounds")
        require("fingerprint" in source["alias"]["files"], "native trial did not retain its actual setup fingerprint")
        actual_setup = json.loads(Path(source["alias"]["files"]["fingerprint"]["path"]).read_text(encoding="utf-8"))
        same(actual_setup["task"]["digest"], job.context["executed_package"]["sha256"], "actual fingerprint task digest changed")
        for key in ("sampling", "server", "harness", "budgets", "reference_profile"):
            same(actual_setup[key], job.fingerprint[key], "actual native behavioral setup differs from its planned setup")
        record.update(flags=source["flags"], native_provenance=source["alias"], initial_user_message_index=boundary,
                      native_trajectory=native_ref, corrected_patch=source["alias"]["files"].get("patch"))
        record["provenance"]["native_trajectory"] = native_ref
    except Exception as exc:
        record["problems"].append({"actual_native_custody": failure(exc)})
    budget_stop = (exception.get("exception_type") == "TrialBudgetExhaustedError"
                   or metadata.get("stop_reason") == "trial_budget_exhausted")
    context_stop = metadata.get("stop_reason") == "context_exhausted" or metadata.get("native_exit_status") == "ContextExhausted"
    if budget_stop:
        record["status"] = "native_model_budget_stop"
    elif context_stop:
        record["status"] = "native_context_budget_stop"
    elif record["problems"]:
        record["status"] = "native_evidence_incomplete"
    elif exception or metadata.get("infra_error") or metadata.get("native_exit_status") != "Idle" or not raw.get("finished_at"):
        record["status"] = "native_infra_or_incomplete"
    elif record["queue_state"] != "done":
        record["status"] = "native_settlement_failed"
    elif p.score(raw) not in (0.0, 1.0):
        record["status"] = "native_grade_missing_or_nonbinary"
    elif request["purpose"] == "evaluation":
        record["status"] = "completed_pending_separate_repaired_evaluation"
        record["original_grader_observation"] = {"result": ref(results[0]), "reward": p.score(raw),
                                                  "package": job.context["executed_package"], "is_final_repaired_evaluation": False}
    else:
        record["repaired_reward"] = p.score(raw)
        record["final_repaired_result"] = {"result": ref(results[0]), "execution_id": record["execution_id"],
            "job_id": record["native_job_id"], "reward": p.score(raw), "grader": job.context["repaired_grader"],
            "fingerprint": record["native_files"]["fingerprint"], "lock": record["native_files"]["lock"],
            "identity_basis": "actual final native verifier result on the exact repaired request package"}
        record["status"] = "completed_repaired_pass" if p.score(raw) == 1.0 else "completed_repaired_fail"
    return record


def execute(jobs: list[Prepared], args: argparse.Namespace, modules: tuple[Any, ...],
            budget: NativeBudget, window: dict[str, Any]) -> list[dict[str, Any]]:
    p, c, e, native, queue_module, _ = modules
    require(bool(os.environ.get("MIMO_SELFHOSTED_API_KEY"))
            and os.environ.get("MIMO_SELFHOSTED_API_KEY") != native.MIMO_SELFHOSTED_PROXY_TOKEN,
            "MIMO_SELFHOSTED_API_KEY must be supplied by the parent environment/keys run; no fallback credential file")
    require(os.environ.get("EVALLAB_NO_PROCESS_JOB") != "1", "native postprocessing/ingestion cannot be bypassed")
    require(os.environ.get("EVALLAB_MODEL_CAPTURE") != "1", "this owned window does not authorize a substitute capture upstream")
    queue = queue_module.DirectoryQueue(args.out / "queue")
    executor = queue_module.Executor(repo_root=ROOT, queue=queue,
        policy=native.load_policy(ROOT / "policy/standing-approvals.yaml"), parallel=1, max_transient_retries=0,
        selfhosted_warmup_seconds=1.0, selfhosted_probe_timeout_seconds=PROBE_TIMEOUT_SECONDS)
    # No injected runner/ingester/compliance/readiness result, and no teardown
    # hook: all native front-door gates and settlement remain enabled.
    rows = []
    with args.serving_window.with_suffix(".execution.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal("the owned serving window already has an active correction dispatcher") from exc
        with process_upstream(window["upstream"]):
            for job in jobs:
                charge = None
                dispatches = 0
                dispatch_error = None
                try:
                    same(checked_context(job.selection, args, p, c, e), job.context,
                         "request/source/grader custody changed between planning and dispatch")
                    native_request = queue_module.Executor.prepare_request(job.spec, repo_root=ROOT)
                    for key, value in {**MODAL, **RUNTIME}.items():
                        same(getattr(native_request, key, None), value, "typed queue propagation changed before dispatch")
                    native.validate_request(native_request, repo_root=ROOT)
                    window = checked_window(args.serving_window, [job], budget, e, expected=window["receipt"])
                    charge = budget.charge(job, window)
                    submitted, decision = executor.submit(job.spec)
                    same(queue.load(submitted).spec_id, job.spec.spec_id, "native queue returned another spec ID")
                    require(not decision.admitted and decision.reason_code == "paid_run_unauthorized",
                            "native submission requires its exact per-spec parent approval; another native policy refusal is not overridable")
                    queue.approve(job.spec.spec_id, actor=args.approval)
                    # Recheck time/authority after local policy/catalog work, before
                    # the native readiness probe can make even its one-token call.
                    checked_window(args.serving_window, [job], budget, e, expected=window["receipt"])
                    append_json(budget.journal, {"event": "native_tick_requested", "at": now(),
                        "spec_id": job.spec.spec_id, "job_name": job.spec.name, "reservation_ordinal": charge["ordinal"]})
                    dispatches = executor.tick(parallel=1, spec_ids=[job.spec.spec_id])
                except (Exception, KeyboardInterrupt) as exc:
                    dispatch_error = failure(exc)
                if charge is None:
                    # No queue/model/provider work was authorized. Do not fabricate
                    # an execution receipt for a pre-dispatch local refusal.
                    raise Refusal("single-shot dispatch preflight refused before a native reservation")
                try:
                    queued = queue.locate(job.spec.spec_id)
                    if queued.parent.name in {"pending", "proposed", "waiting", "approved"}:
                        queue.reject(job.spec.spec_id, actor=args.approval,
                                     message="single-shot correction dispatch ended; no later implicit execution is authorized")
                    record = native_observation(job, queue, p, c)
                    try:
                        same(checked_context(job.selection, args, p, c, e), job.context,
                             "source/request/grader snapshot bytes changed during native execution")
                    except Exception as exc:
                        record["problems"].append({"post_execution_input_custody": failure(exc)})
                        record.update(status="native_evidence_incomplete", repaired_reward=None, final_repaired_result=None)
                except Exception as exc:
                    record = {**job.plan(), "schema": "envcheck.native-correction-execution/v1",
                        "observed_at": now(), "protocol": job.selection.request["protocol"],
                        "repaired_digest": job.selection.request["source_partition"]["variant"]["repaired_digest"],
                        "provenance": {"correction": job.selection.request["provenance"]["correction"],
                                       "request": job.selection.request["request"]},
                        "execution_status": "native_observation_unavailable", "status": "native_observation_unavailable",
                        "problems": [failure(exc)], "repaired_reward": None, "final_repaired_result": None, "training_exported": False}
                record.update(native_tick_dispatches=dispatches, native_tick_reason=executor.last_tick_reason,
                              dispatch_error=dispatch_error, reservation=charge, serving_window=window["receipt"])
                append_json(args.out / "executions.jsonl", record)
                rows.append(record)
                if (dispatch_error is not None or dispatches != 1
                        or record["status"] not in {"completed_repaired_pass", "completed_repaired_fail", "completed_pending_separate_repaired_evaluation"}):
                    break
    return rows


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests", type=Path, action="append", required=True, help="existing immutable requests.jsonl or individual request.json; repeatable")
    parser.add_argument("--request-id", action="append", help="exact prepared request ID; default all supplied requests")
    parser.add_argument("--heldout-manifest", type=Path, help="exact frozen all33 authority; defaults to partition.py's canonical path")
    parser.add_argument("--evaluation-only", action="store_true", help="explicit heldout evaluation only; never training")
    parser.add_argument("--batch", action="store_true", help="explicitly authorize selecting more than one single-shot correction")
    parser.add_argument("--out", type=Path, required=True, help="NEW *-vN directory under this worktree's runs/envcheck-20261017/")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--budget", type=Path, help="parent's existing SHARED fixed-price native subgate, never a per-invocation cap")
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--reserve-usd", type=float)
    parser.add_argument("--approval", help="parent's currently recorded lane authorization, no default")
    parser.add_argument("--serving-window", type=Path, help="parent's actual owned unexpired serving-window receipt; never created here")
    args = parser.parse_args()
    try:
        args.out = safe_path(args.out)
        require(args.out.is_relative_to(LANE) and re.search(r"-v[1-9][0-9]*$", args.out.name), "--out must be a new versioned directory in the owned envcheck lane")
        require(not args.out.exists(), "--out already exists; never overwrite or implicitly resume evidence")
        require(len(set(args.request_id or [])) == len(args.request_id or []), "duplicate --request-id selections")
        if args.budget is not None:
            args.budget = safe_path(args.budget)
            require(args.budget.is_relative_to(LANE) and not args.budget.is_relative_to(args.out), "shared native budget must remain outside the new output in the same lane")
        if args.serving_window is not None:
            args.serving_window = safe_path(args.serving_window)
            require(args.serving_window.is_relative_to(LANE) and not args.serving_window.is_relative_to(args.out), "parent serving-window receipt must remain outside the new output in the same lane")
        if args.max_usd is not None:
            equal_money(args.max_usd, NATIVE_CAP_USD, "native --max-usd must match the parent's separate $0.30 suballocation, not a new $4 grant")
        if args.reserve_usd is not None:
            equal_money(args.reserve_usd, NATIVE_RESERVE_USD, "each native correction must reserve exactly $0.10, separate from serving")
        if args.execute:
            require(args.budget is not None and args.serving_window is not None and args.max_usd is not None
                    and args.reserve_usd is not None and bool((args.approval or "").strip()),
                    "execution requires explicit --execute --budget --max-usd --reserve-usd --approval --serving-window")
    except Refusal as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = arguments()
    args.out.mkdir(parents=True, exist_ok=False)
    started = now()
    jobs: list[Prepared] = []
    problems: list[dict[str, Any]] = []
    budget = None
    window = None
    rows = []
    plan = {"schema": "envcheck.native-correction-plan/v1", "created_at": started, "root": str(ROOT),
            "execute_requested": args.execute, "evaluation_only": args.evaluation_only, "notice": NOTICE,
            "runtime": RUNTIME, "modal": MODAL, "allowed_student_models": sorted(ALLOWED_MODELS),
            "native_gate_subcap_usd": NATIVE_CAP_USD, "native_reservation_per_job_usd": NATIVE_RESERVE_USD,
            "separate_serving_reservation_usd": SERVING_RESERVE_USD, "global_lane_cap_usd": 4.0,
            "front_door": "ExperimentSpec -> DirectoryQueue.submit/approve -> Executor.tick(spec_ids=[id])",
            "native_transient_retries": 0, "native_concurrency": 1, "implicit_reuse": False,
            "provider_or_model_calls_made_by_plan": 0, "training_exported": False, "jobs": []}
    try:
        modules = dependencies()
        p, _, e, _, _, _ = modules
        args.heldout_manifest = args.heldout_manifest or p.DEFAULT_HELDOUT
        chosen = selections(args)
        for selection in chosen:
            job = prepare(selection, args, modules)
            jobs.append(job)
            write_json(args.out / "specs" / (job.selection.request["request_id"] + ".json"), job.spec.model_dump(mode="json", exclude_none=True))
        grant_flags = (args.budget is not None and args.max_usd is not None and args.reserve_usd is not None
                       and bool((args.approval or "").strip()))
        if grant_flags:
            budget = NativeBudget(args)
            budget.check(jobs)
        if args.serving_window is not None:
            require(budget is not None, "checking a paid serving receipt also requires the explicit shared native gate/approval flags")
            window = checked_window(args.serving_window, jobs, budget, e)
        plan.update(jobs=[job.plan() for job in jobs], selected_requests=len(chosen),
                    status="planned_with_bound_parent_window" if window is not None else "free_plan_not_execution_authorized",
                    budget={"path": str(args.budget) if args.budget else None,
                            "initial_state": budget.initial if budget else None},
                    serving_window=window["receipt"] if window else None,
                    execution_prerequisites=["environment MIMO_SELFHOSTED_API_KEY via keys run", "normal native catalog/ingestion readiness"]
                        + ([] if window else ["all explicit execution/budget flags", "parent-owned active serving-window receipt and authenticated readiness"]))
    except (Exception, KeyboardInterrupt) as exc:
        problems.append(failure(exc))
        plan.update(status="refused_before_provider", jobs=[job.plan() for job in jobs], problems=problems)
    write_json(args.out / "plan.json", plan)
    if args.execute and not problems:
        try:
            rows = execute(jobs, args, modules, budget, window)
        except (Exception, KeyboardInterrupt) as exc:
            problems.append(failure(exc))
        # Only dispatch attempts create execution artifacts. A failed local
        # preflight remains a plan/refusal, not a fabricated native result.
        if budget.charged:
            if not rows and (args.out / "executions.jsonl").is_file():
                rows = [json.loads(line) for line in (args.out / "executions.jsonl").read_text(encoding="utf-8").splitlines()]
            write_json(args.out / "execution-summary.json", {
                "schema": "envcheck.native-correction-run/v1", "started_at": started, "finished_at": now(),
                "notice": NOTICE, "plan": ref(args.out / "plan.json"), "serving_window": window["receipt"],
                "shared_native_budget": {"path": str(budget.path), "current_state": budget.read(),
                    "new_reservations": budget.charged, "charge_journal": ref(budget.journal)},
                "recorded_dispositions": len(rows), "native_tick_dispatches": sum(row.get("native_tick_dispatches", 0) for row in rows),
                "statuses": [row["status"] for row in rows], "problems": problems,
                "training_exported": False, "canonical_repaired_receipts_created": 0,
                "separate_repaired_evaluation_required": args.evaluation_only})
    success = not problems and (not args.execute or (len(rows) == len(jobs) and all(
        row["status"] in {"completed_repaired_pass", "completed_repaired_fail", "completed_pending_separate_repaired_evaluation"} for row in rows)))
    print(json.dumps({"plan": ref(args.out / "plan.json"), "status": "completed_native_dispatch" if args.execute and success else
        "planned_not_executed" if not args.execute and success else "refused_or_incomplete", "problems": problems,
        "selected_native_specs": len(jobs), "recorded_native_dispositions": len(rows),
        "native_reservations_this_invocation": budget.charged if budget else 0, "training_exported": False}, sort_keys=True))
    return 0 if success else 3


if __name__ == "__main__":
    raise SystemExit(main())
