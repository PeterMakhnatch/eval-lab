#!/usr/bin/env python3
"""Plan, or explicitly postgrade ONE actual heldout native B correction.

    python research/experiments/test-repair-performance-20261017/postgrade_evaluation.py \
        --requests REQUESTS_JSONL --executions EXECUTIONS_JSONL --execution NATIVE_UUID \
        --out runs/envcheck-20261017/m3/evaluation-postgrade-v1

The default is local preparation only. Paid dispatch additionally requires
--execute --budget EXISTING_SHARED_GATE --max-usd 0.04 --reserve-usd 0.04
--approval PARENT_APPROVAL --global-ledger EXISTING_GLOBAL_LEDGER. The ledger's
native_evaluation_correction_grade reservation must name that exact gate. Neither
the global $4 authority nor any prior reservation is reset or replaced here.

Only the new correction's retained textual patch is replayed, once, through
public native cf.replay and the real run_matrix callback. The request's ACTUAL
prior native nop=0/reference=1 controls are checked and explicitly reused, not
rerun. The new student UUID is never replaced with its original a1 UUID, and an
oracle patch replay never becomes a new model execution. Original-grader reward
is weak evidence, not the repaired result. Missing/infra observations remain
unmeasured. No snapshot-regrade fallback or result cache; paid replays use native
Eval Lab callbacks, never direct provider/model transport.
"""
from __future__ import annotations

import argparse
import contextlib
import math
import os
import re
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

import replay_passes as replay

ROOT = Path(__file__).resolve().parents[3]
LANE = ROOT / "runs/envcheck-20261017"
OWNED_ROOT = LANE / "m3"
SCHEMA = "envcheck.native-evaluation-postgrade/v1"
CORRECTION_SCHEMA = "envcheck.actual-evaluation-correction/v1"
CAP_USD = Decimal("0.04")
NOTICE = (
    "Actual native same-B reset-context correction; its original-grader score is not repaired success. "
    "The 180s correction versus 1800s baseline is an explicit runtime/protocol confound. "
    "Prior native controls are reused, not fresh controls. Feedback and final grading reuse the "
    "same repaired-grader endpoint; independent semantic evaluation remains unmeasured. "
    "A model-free patch replay is not another student/model execution. Reservations are not invoices."
)


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def same(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise ValueError(message)


def safe_path(path: Path) -> Path:
    raw = path.expanduser().absolute()
    require(".." not in raw.parts, "path traversal is forbidden")
    require(not any(part.is_symlink() for part in (raw, *raw.parents)), "symlink paths are forbidden")
    return raw.resolve()


def modules() -> tuple[Any, Any, Any, Any, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    import correction_data as c
    import evaluation_corrections as evaluation
    import native_completion as completion
    import partition as p

    from evallab.qa import counterfactual as cf

    for module in (c, evaluation, completion, p):
        same(Path(module.__file__).resolve().parent, Path(__file__).resolve().parent,
             "correction helpers must come from this checkout")
    require(Path(cf.__file__).resolve().is_relative_to(ROOT / "src/evallab"),
            "native counterfactual implementation comes from another checkout")
    return p, c, evaluation, completion, cf


def checked_ref(ref: dict[str, Any], p: Any) -> dict[str, Any]:
    path = safe_path(Path(ref["path"]))
    actual = p.file_ref(path)
    same(actual, {key: ref[key] for key in ("path", "sha256")}, "retained artifact bytes/path changed")
    return actual


def bound_row(binding: dict[str, Any], p: Any) -> dict[str, Any]:
    checked_ref(binding, p)
    rows = p.read_jsonl(Path(binding["path"]))
    index = binding["row_index"]
    require(type(index) is int and 0 <= index < len(rows), "bound JSONL selector is outside its immutable input")
    row = rows[index]
    same(p.sha256(p.canonical_bytes(row)), binding["row_sha256"], "bound JSONL row bytes changed")
    return row


def request_binding(path: Path, execution: dict[str, Any], request_id: str | None, p: Any) -> dict[str, Any]:
    binding = p.file_ref(safe_path(path))
    require(request_id is None or request_id == execution["request_id"], "request selector differs from actual execution")
    matches = [(index, row) for index, row in enumerate(p.read_jsonl(path))
               if row.get("request_id") == execution["request_id"] and row.get("request") == execution["request"]]
    require(len(matches) == 1, "requires exactly one untouched request row matching this actual execution")
    index, row = matches[0]
    return {**binding, "row_index": index, "row_sha256": p.sha256(p.canonical_bytes(row))}


def patch_text(ref: dict[str, Any], cf: Any, p: Any) -> str:
    from evallab.heldout_verifier import scan_agent_diff

    checked_ref(ref, p)
    data = Path(ref["path"]).read_bytes()
    text = data.decode("utf-8")
    same(text.encode("utf-8"), data, "native patch decoding changed bytes")
    require(bool(text.strip()) and "\x00" not in text, "requires a nonempty actual textual corrected patch")
    replay.unchanged_diff(cf, text, "actual corrected patch")
    problems, targets = scan_agent_diff(ref["path"])
    require(not problems and targets > 0, "actual corrected patch is unsafe or unreplayable: " + repr(problems))
    return text


def native_replay_artifacts(
    row: dict[str, Any], native_ref: dict[str, Any], grader: Any, *, source_id: str,
    recorded: float, text: str, p: Any, cf: Any,
) -> dict[str, Any]:
    """Check native matrix, real trial and retained input, including archived jobs."""
    from evallab.registry import compute_task_digests
    from evallab.schemas import ExperimentMatrix

    checked_ref(native_ref, p)
    same(p.read_json(Path(native_ref["path"])), row, "native CF row differs from its untouched retained output")
    require(replay.successful_native(row, p), "native replay is missing/refused/infra; it is not a zero")
    same(row.get("environment"), "modal", "native replay backend changed")
    same(row.get("dropped_binary_artifacts"), [], "native replay dropped binary input")
    same(row.get("grader"), grader.label, "native replay grader identity changed")
    same(p.read_json(Path(row["trial"]) / "result.json").get("id"), source_id,
         "native replay belongs to another original/corrected student UUID")
    same(p.number(row.get("recorded")), recorded, "native replay recorded weak/original reward changed")
    matrix_path = safe_path(Path(row["matrix_receipt"]))
    require(matrix_path.is_relative_to(ROOT / "runs/.executor"), "native matrix receipt is outside the owned executor")
    document = p.read_json(matrix_path)
    matrix = ExperimentMatrix.model_validate(document["matrix"])
    same(matrix.environment, "modal", "native matrix backend changed")
    same(matrix.concurrency, 1, "native matrix concurrency changed")
    same(len(matrix.runs), 1, "native replay must have exactly one model-free run")
    for key, value in replay.MODAL.items():
        same(getattr(matrix, key), value, "native matrix lost bounded Modal fields")
    run = matrix.runs[0]
    require(run.agent == "oracle" and run.model is None and run.attempts == 1
            and not run.allow_billable and bool(run.solution), "native replay contains a model or extra execution")
    same(matrix.name, Path(row["job_dir"]).name, "native matrix job identity changed")
    same(run.name, matrix.name, "native matrix run identity changed")
    package = safe_path(Path(matrix.task))
    expected = compute_task_digests(grader.package)
    actual = compute_task_digests(package)
    same(actual.package, expected.package, "native matrix replayed another repaired package")
    same(matrix.task_package_digest, expected.package, "native matrix package digest differs")
    same(matrix.verifier_digest, expected.verifier, "native matrix verifier digest differs")
    replay.resource_metadata(package)
    results = document.get("results")
    require(isinstance(results, list) and len(results) == 1, "native matrix has no unique actual execution result")
    result = results[0]
    require(result.get("status") == "ok" and not result.get("error"), "native matrix failed; never interpret infra as zero")
    same(result.get("name"), run.name, "native matrix result belongs to another run")
    same(result.get("rewards"), [row["regraded"]], "native matrix and CF rewards differ")
    solution = Path(run.solution)
    require(not solution.is_absolute() and ".." not in solution.parts, "native solution path is unsafe")
    solution = safe_path(ROOT / solution)
    require(solution.is_relative_to(ROOT / "runs/qa-trace-regrade/scripts"), "native replay bypassed the CF script path")
    solution_ref = p.file_ref(solution)
    same(solution_ref["sha256"], p.digest(row["script_sha256"]), "native replay script bytes changed")
    same(p.digest(result["solution_sha256"]), solution_ref["sha256"], "matrix did not execute the retained CF script")
    workdir = tomllib.loads((grader.package / "task.toml").read_text(encoding="utf-8"))["environment"]["workdir"]
    # Pure native encoder; execution itself always uses the public replay front door.
    same(solution.read_bytes(), cf._patch_script(text, workdir).encode("utf-8"),
         "native replay script does not apply the exact bound textual input")
    checked_diff = Path(native_ref["path"]).parent / "work/agent.replay.diff"
    same(checked_diff.read_bytes(), text.encode("utf-8"), "native CF retained another checked patch")
    trials = p.receipt_trials(row)
    require(len(trials) == 1, "native replay lacks exactly one real retained trial")
    trial = trials[0]
    raw = p.read_json(trial / "result.json")
    require(not raw.get("exception_info") and bool(raw.get("finished_at")), "native replay trial is incomplete/infra")
    same(p.score(raw), row["regraded"], "real verifier result disagrees with canonical replay")
    same(p.parent_task(raw["task_name"]), grader.task_name, "native control task changed")
    config = raw["config"]
    require(config["agent"].get("name") == "oracle" and config["agent"].get("model_name") is None,
            "native replay trial invoked a model")
    require(not config.get("source_trial") and not config.get("extra_instruction_paths")
            and not config.get("extra_instructions"), "native replay trial added unrelated model inputs")
    environment = config["environment"]
    same(environment.get("import_path"), "evallab.harbor_modal:TaskModalEnvironment", "native replay used an alternate environment")
    for key, value in {"app_name": replay.MODAL["modal_app_name"], "sandbox_timeout_secs": 300,
                       "cpu_enforcement_policy": "limit", "memory_enforcement_policy": "limit"}.items():
        same(environment["kwargs"].get(key), value, "actual native replay Modal bounds changed")
    require(environment.get("override_cpus") in (None, 2)
            and environment.get("override_memory_mb") in (None, 8192),
            "native replay resource overrides differ from the 2 CPU/8 GiB envelope")
    require(environment.get("override_gpus") in (None, 0), "native replay requested a GPU")
    lock = p.read_json(trial / "lock.json")
    same(lock.get("agent", {}).get("name"), "oracle", "native replay lock is not model-free")
    same(lock.get("verifier", {}).get("environment_mode"), "separate", "native verifier isolation changed")
    fingerprint = p.read_json(trial / "setup-fingerprint.json")
    same(fingerprint["task"]["digest"], result["staged_task_digest"], "actual native replay fingerprint differs from staging")
    require(not (trial.parent / "process-job-error.txt").exists(), "official native postprocessing failed")
    processed = trial.parent / "processed"
    require((processed / "job.json").is_file() and (processed / f"trial-{trial.name}.json").is_file(),
            "official native ingestion/postprocessing artifacts are missing")
    paths = {"matrix_receipt": matrix_path, "solution": solution, "checked_patch": checked_diff,
             "result": trial / "result.json", "config": trial / "config.json", "lock": trial / "lock.json",
             "fingerprint": trial / "setup-fingerprint.json", "processed_job": processed / "job.json",
             "processed_trial": processed / f"trial-{trial.name}.json"}
    for name, relative in (("verifier_stdout", "verifier/test_output.log"), ("reward", "verifier/reward.json")):
        if (trial / relative).is_file():
            paths[name] = trial / relative
    invocations = matrix_path.with_name(matrix_path.name.removesuffix(".json") + ".invocations.jsonl")
    require(invocations.is_file(), "native matrix invocation evidence is missing")
    paths["invocations"] = invocations
    return {"native": checked_ref(native_ref, p), "row_sha256": p.sha256(p.canonical_bytes(row)),
            "trial_uuid": str(UUID(raw["id"])), "job_uuid": config["job_id"],
            "artifacts": {name: p.file_ref(safe_path(path)) for name, path in paths.items()}}


def reused_controls(request: dict[str, Any], grader: Any, grader_custody: dict[str, Any], p: Any, c: Any, cf: Any) -> dict[str, Any]:
    selected = request["source_partition"]["provenance"]["selected_receipt"]
    c.checked_evidence(selected)
    require(selected.get("valid") is True and not selected.get("problems"), "prior baseline selected receipt is invalid")
    baseline = selected["row"]
    same(baseline.get("execution_id"), request["source_execution_id"], "prior controls came from another source a1")
    same(baseline.get("engine"), cf.ENGINE_VERSION, "prior controls use another native engine")
    same(baseline.get("grader_digest"), grader.variant_digest, "prior control package digest changed")
    require(baseline.get("valid_grader") is True and baseline.get("controls") == {"nop": 0.0, "reference": 1.0},
            "requires actual admitted native nop=0/reference=1 controls")
    reference = Path(baseline["control_receipt"])
    receipt_ref = p.file_ref(safe_path(reference))
    receipt = p.read_json(reference)
    same(receipt.get("engine"), cf.ENGINE_VERSION, "prior native control receipt engine changed")
    same(receipt.get("grader_source"), baseline["grader_source"], "prior control receipt grader identity changed")
    require(receipt.get("valid") is True and receipt.get("nop") == 0.0 and receipt.get("reference") == 1.0,
            "prior native controls are not the actual zero/one pair")
    identity = receipt["grader_source"]
    same(identity["task_name"], request["task"], "prior control task changed")
    same(identity["variant_digest"], grader.variant_digest, "prior control repaired digest changed")
    same(identity["transform"], grader.transform, "prior control transformation changed")
    same(p.file_ref(safe_path(Path(identity["record"]))), grader_custody["record"], "prior native lineage record changed")
    references = {}
    for role, expected in (("nop", 0.0), ("reference", 1.0)):
        native_ref = receipt["receipts"][role]
        row = receipt["rows"][role]
        same(row.get("regraded"), expected, "prior native control is not the required actual observation")
        same(native_ref["observation"]["role"], role, "prior control receipt role differs")
        same(native_ref["observation"]["canonical_result"], {key: native_ref[key] for key in ("path", "sha256")},
             "prior control observation does not bind its raw native output")
        text = "" if role == "nop" else Path(grader_custody["reference_file"]["path"]).read_bytes().decode("utf-8")
        replay.unchanged_diff(cf, text, "retained prior control")
        references[role] = native_replay_artifacts(row, native_ref, grader,
            source_id=request["source_execution_id"], recorded=request["source_partition"]["original_reward"],
            text=text, p=p, cf=cf)
    return {"status": "cached_controls_reused", "fresh_controls_this_invocation": 0,
            "nop": 0.0, "reference": 1.0, "selected_receipt": selected,
            "control_receipt": receipt_ref, "native_controls": references,
            "package_digest": grader.variant_digest, "verifier_digest": grader_custody["verifier_digest"]}


@dataclass
class Context:
    request: dict[str, Any]
    request_input: dict[str, Any]
    execution_binding: dict[str, Any]
    completion: dict[str, Any]
    native: dict[str, Any]
    pin: dict[str, Any]
    grader: Any
    audit: dict[str, Any]
    text: str


def checked_context(request_input: dict[str, Any], execution_binding: dict[str, Any]) -> Context:
    p, c, evaluation, completion_helper, cf = modules()
    from run_corrections import RUNTIME
    from study_plan import MODEL, native_source

    from evallab.registry import compute_task_digests
    from evallab.regrade import verifier_identity
    from evallab.task_variants import task_directory_digest

    request = bound_row(request_input, p)
    evaluation.checked_request(request)
    plan, plan_ref, _, _ = evaluation.checked_plan()
    tasks = [task for task in plan["tasks"] if task["task"] == request["task"]
             and task["candidates"][0]["native"]["execution_id"] == request["source_execution_id"]]
    require(len(tasks) == 1, "correction does not map to the fixed plan's exact original B a1")
    task = tasks[0]
    base = task["candidates"][0]["native"]
    same(request["model"], MODEL, "postgrade accepts only the frozen B model/checkpoint")
    completed = completion_helper.checked_completion(request, execution_binding)
    # Heldout membership comes from the checked all33 request, not from a field
    # invented in the immutable native execution row.
    completed = {**completed, "source": {**completed["source"], "heldout": request["heldout"]}}
    execution = completed["execution"]
    require(execution.get("purpose") == "evaluation" and execution.get("heldout") is True
            and execution.get("training_allowed") is False, "native completion is not evaluation-only")
    same(execution.get("status"), "completed_pending_separate_repaired_evaluation", "native student is incomplete/stopped or already a different kind of grade")
    candidate_ids = {item["native"]["execution_id"] for cell in plan["tasks"] for item in cell["candidates"] if item["native"]}
    require(execution["execution_id"] not in candidate_ids, "a scheduled independent candidate is not a new correction")
    require(completed["native_reward"] in (0.0, 1.0), "actual correction original-grade reward is missing, not zero")
    native = native_source(Path(execution["source_trial"]))
    require(native["completion"] == "clean" and not native["infra_error"] and not native["stop_reason"],
            "native correction carries a stop/infra flag; it is not a clean corrected output")
    same(native["execution_id"], execution["execution_id"], "native student UUID changed")
    same(native["model"], MODEL, "native corrected output is not the exact same B model")
    same(native["task_digest"], task["source_task_digest"], "native correction executed another original package")
    same(native["native_reward"], completed["native_reward"], "actual weak original-grade observations disagree")
    spec = p.read_json(Path(native["sources"]["experiment_spec"]["path"]))
    for key, expected in RUNTIME.items():
        same(spec.get(key), expected, "actual native correction differs from the declared reduced runtime")
    meta = request["provenance"]["correction"]
    same(completed["executed_package"], meta["task_package_snapshot"], "evaluation correction did not execute the original request snapshot")
    repaired = meta["repaired_task_package_snapshot"]
    package = safe_path(Path(repaired["path"]))
    require(package.is_relative_to(OWNED_ROOT) and not any(path.is_symlink() for path in package.rglob("*")),
            "repaired request snapshot is outside its owned evaluation packages or contains symlinks")
    same(task_directory_digest(package), task["repaired_digest"], "bound repaired snapshot changed")
    pin = request["source_partition"]["variant"]
    candidate, _, custody = replay.grader_custody(pin, Path(pin["manifest"]["path"]), [], p, cf)
    same(compute_task_digests(package), compute_task_digests(candidate.package), "repaired snapshot and native admitted candidate differ")
    for role in ("task", "verifier"):
        resources = custody["resources"][role]
        require(resources["cpus"] == 2 and resources["memory_mb"] == 8192 and resources["gpus"] == 0,
                "final native replay requires the exact 2 CPU/8 GiB/no-GPU task and verifier")
    same(verifier_identity(package).digest, verifier_identity(candidate.package).digest, "snapshot/candidate verifier identity changed")
    grader = cf.Grader(candidate.task_name, candidate.transform, candidate.variant_digest, package, candidate.record)
    controls = reused_controls(request, grader, custody, p, c, cf)
    text = patch_text(completed["patch"], cf, p)
    source = completed["source"]
    same(source["execution_id"], execution["execution_id"], "actual corrected inventory UUID changed")
    same(source.get("heldout"), True, "actual correction lost all33 heldout lineage")
    flags = source["flags"]
    require(not any(flags.get(key) for key in ("teacher", "copied_fix", "alias_conflicts", "attempt_conflict")),
            "teacher/copied/conflicting outputs cannot become corrected student observations")
    runtime = {"baseline_runtime_matched": False, "baseline_timeout_seconds": base["settings"]["fingerprint"]["budgets"]["timeout_seconds"],
               "correction_timeout_seconds": spec["timeout_seconds"], "baseline_settings_digest": base["settings_digest"],
               "correction_settings_digest": native["settings_digest"], "declared_runtime_changes": execution["declared_runtime_changes"],
               "same_grader_endpoint_reuse": True, "independent_semantic_evaluation": "unmeasured", "notice": NOTICE}
    same(runtime["baseline_timeout_seconds"], 1800, "fixed baseline is no longer the declared 1800s protocol")
    audit = {"schema": SCHEMA, "plan": plan_ref, "request_input": request_input, "request": request["request"],
             "execution_binding": execution_binding, "source_execution_id": request["source_execution_id"],
             "corrected_execution_id": native["execution_id"], "native_trajectory": native["sources"]["native_trajectory"],
             "corrected_patch": completed["patch"], "patch_custody": completed["patch_custody"],
             "executed_package": completed["executed_package"], "repaired_package": repaired,
             "repaired_verifier": verifier_identity(package).model_dump(mode="json"),
             "grader_custody": custody, "controls_reused": controls, "runtime_confound": runtime}
    return Context(request, request_input, execution_binding, completed, native, pin, grader, audit, text)


def money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid reservation amount") from exc
    require(result.is_finite() and result >= 0, "reservation amount must be finite and nonnegative")
    return result


class EvaluationBudget(replay.Gate):
    """One durable native charge under an existing, SHA-bound global allocation."""

    def __init__(self, args: argparse.Namespace, p: Any):
        from evallab.regrade import CloudSpendGate

        self.p = p
        self.ledger_ref = p.file_ref(args.global_ledger)
        self.reservation_id = args.reservation_id
        self.check_ledger(args.budget, args.approval)
        super().__init__(CloudSpendGate(args.approval, args.max_usd, args.reserve_usd), args.budget, args.out, execute=args.execute)
        self.initial = self.to_json()

    def check_ledger(self, budget: Path, approval: str) -> None:
        checked_ref(self.ledger_ref, self.p)
        ledger = self.p.read_json(Path(self.ledger_ref["path"]))
        same(ledger.get("schema"), "envcheck.global-exposure-ledger/v1", "requires the parent's existing global exposure ledger")
        same(ledger.get("approval"), approval, "global ledger approval differs from explicit parent approval")
        same(money(ledger.get("cap_usd")), Decimal("4"), "global lane cap changed")
        rows = ledger.get("reservations")
        require(isinstance(rows, list) and bool(rows), "global exposure ledger lacks actual reservations")
        by_id = {row["reservation_id"]: row for row in rows}
        same(len(by_id), len(rows), "global exposure ledger repeats reservation IDs")
        total = sum((money(row["reserved_usd"]) for row in rows), Decimal(0))
        same(money(ledger.get("total_exposure_usd")), total, "global reservation sum changed")
        require(total <= Decimal("4"), "global reservations exceed the existing $4 authority")
        allocated = by_id[self.reservation_id]
        same(allocated.get("kind"), "native_evaluation_correction_grade", "global reservation is not for this final evaluation grade")
        same(money(allocated.get("reserved_usd")), CAP_USD, "postgrade must retain its existing $0.04 suballocation")
        same(safe_path(Path(allocated["budget_path"])), budget, "global reservation names another budget gate")
        matching = [row for row in rows if row.get("budget_path") and safe_path(Path(row["budget_path"])) == budget]
        same(len(matching), 1, "global ledger ambiguously allocates this postgrade gate")

    def read_state(self) -> dict[str, Any]:
        require(self.path.is_file(), "parent must create the existing shared postgrade gate; no replacement allowance is created")
        self.check_ledger(self.path, self.native.approval)
        state = super().read_state()
        require(type(state["executions"]) is int and 0 <= state["executions"] <= 1, "postgrade gate contains an invalid cumulative count")
        same(len(state.get("charges", [])), state["executions"], "postgrade reservation journal/count mismatch")
        return state

    def before_dispatch(self, matrix_id: str) -> None:
        self.check_ledger(self.path, self.native.approval)
        super().before_dispatch(matrix_id)


@contextlib.contextmanager
def owned_ingestion(output: Path) -> Iterator[Path]:
    require(os.environ.get("EVALLAB_NO_PROCESS_JOB") != "1", "official native ingestion/postprocessing must not be disabled")
    root = safe_path(output / "derived/parquet")
    root.mkdir(parents=True, exist_ok=False)
    previous = os.environ.get("EVALLAB_DERIVED_ROOT")
    os.environ["EVALLAB_DERIVED_ROOT"] = str(root)
    try:
        yield root
    finally:
        if previous is None:
            os.environ.pop("EVALLAB_DERIVED_ROOT", None)
        else:
            os.environ["EVALLAB_DERIVED_ROOT"] = previous


def execute_once(context: Context, output: Path, gate: EvaluationBudget) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    p, _, _, _, cf = modules()
    from evallab import runner
    from evallab.registry import compute_task_digests
    from evallab.schemas import ExperimentMatrix

    require(Path(runner.__file__).resolve().is_relative_to(ROOT / "src/evallab"), "native matrix runner comes from another checkout")
    directory = output / "source"
    directory.mkdir()
    observation: dict[str, Any] = {"role": "corrected_source", "started_at": replay.now(), "dispatches": []}

    def unchanged() -> None:
        current = checked_context(context.request_input, context.execution_binding)
        same(current.audit, context.audit, "request/student/patch/control/package custody changed before dispatch")
        same(current.text, context.text, "corrected patch bytes changed before dispatch")

    def bounded_matrix(matrix: Any, *, root: Path) -> Any:
        same(root.resolve(), ROOT, "native matrix checkout changed")
        require(not gate.dispatched, "only one actual native source replay is authorized")
        require(not (set(replay.MODAL) - set(ExperimentMatrix.model_fields)), "native matrix lacks typed bounded Modal fields")
        same(matrix.environment, "modal", "native replay backend changed")
        same(matrix.concurrency, 1, "native replay concurrency changed")
        same(len(matrix.runs), 1, "only one native source replay is authorized")
        run = matrix.runs[0]
        require(run.agent == "oracle" and run.model is None and run.attempts == 1
                and not run.allow_billable and bool(run.solution), "only model-free native oracle-script replay is authorized")
        same(safe_path(Path(matrix.task)), context.grader.package, "native replay changed the repaired snapshot")
        digests = compute_task_digests(context.grader.package)
        same(matrix.task_package_digest, digests.package, "native matrix package changed")
        same(matrix.verifier_digest, digests.verifier, "native matrix verifier changed")
        unchanged()
        bounded = ExperimentMatrix.model_validate(matrix.model_copy(update=replay.MODAL).model_dump(mode="json"))
        request = runner.request_from_matrix(bounded, bounded.runs[0], repo_root=ROOT)
        for key, expected in replay.MODAL.items():
            same(getattr(request, key, None), expected, "native request lost bounded Modal fields")
        same(os.environ.get("EVALLAB_DERIVED_ROOT"), str(output / "derived/parquet"), "native ingestion escaped its owned absolute derived root")
        require(os.environ.get("EVALLAB_NO_PROCESS_JOB") != "1", "official postprocessing was disabled")
        job = request.jobs_dir / request.name
        receipt = request.jobs_dir / ".executor" / f"{matrix.matrix_id}.matrix.json"
        require(not job.exists() and not receipt.exists(), "native result reuse is forbidden; job/receipt already exists")
        replay.write_json(directory / "matrix.input.json", matrix.model_dump(mode="json"), exclusive=True)
        replay.write_json(directory / "matrix.dispatched.json", bounded.model_dump(mode="json"), exclusive=True)
        gate.before_dispatch(matrix.matrix_id)
        try:
            execution = runner.run_matrix(bounded, root=ROOT, reuse_existing=False)
        except BaseException as exc:
            replay.append_json(gate.journal, {"event": "native_matrix_failed", "at": replay.now(), "matrix_id": matrix.matrix_id,
                "matrix_receipt_path": str(receipt), "job_dir": str(job), "error_type": type(exc).__name__})
            raise
        dispatch = {"matrix_id": matrix.matrix_id, "receipt": p.file_ref(execution.receipt_path),
                    "invocations": p.file_ref(execution.invocation_path), "exit_code": execution.exit_code,
                    "results": execution.results}
        observation["dispatches"].append(dispatch)
        replay.append_json(gate.journal, {"event": "native_matrix_completed", "at": replay.now(), **dispatch})
        replay.write_json(directory / "matrix-observation.json", observation, exclusive=True)
        return execution

    unchanged()
    cf.check_backend("modal", gate)
    context_row = {"role": "corrected_source", "execution_id": context.native["execution_id"],
                   "source_execution_id": context.request["source_execution_id"], "request_id": context.request["request_id"],
                   "grader_digest": context.grader.variant_digest, "global_ledger": gate.ledger_ref,
                   "global_reservation_id": gate.reservation_id}
    with owned_ingestion(output), gate.execution(context_row):
        # Explicit actual text makes the native snapshot fallback unreachable, even
        # when this genuine completion retained kept.diff instead of agent.diff.
        native = cf.replay(Path(context.native["source_trial"]), context.grader, directory / "work",
            diff_text=context.text, repo_root=ROOT, environment="modal", spend_gate=gate, matrix_runner=bounded_matrix)
    replay.write_json(directory / "native.json", native, exclusive=True)
    native_ref = p.file_ref(directory / "native.json")
    observation.update(finished_at=replay.now(), canonical_result=native_ref)
    replay.write_json(directory / "observation.json", observation, exclusive=True)
    require(gate.charges_this_run == 1 and gate.dispatches_this_run == 1, "requires one real reservation and native matrix dispatch")
    artifacts = native_replay_artifacts(native, native_ref, context.grader,
        source_id=context.native["execution_id"], recorded=context.completion["native_reward"], text=context.text, p=p, cf=cf)
    unchanged()
    return native, native_ref, artifacts


def envelope(context: Context, native: dict[str, Any], native_ref: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    _, _, _, _, cf = modules()
    source = context.completion["source"]
    return {**native, "engine": cf.ENGINE_VERSION, "execution_id": context.native["execution_id"],
            "source_execution_id": context.request["source_execution_id"], "task": context.request["task"],
            "model": context.request["model"], "agent": source["agent"], "attempt": source.get("attempt"),
            "heldout": True, "training_allowed": False, "evaluation_only": True, "training_exported": False,
            "source_digest": source["provenance"]["aliases"][0]["source_digest"],
            "grader_digest": context.grader.variant_digest, "grader_source": context.grader.to_json(),
            "transform": context.grader.transform, "valid_grader": True, "controls": {"nop": 0.0, "reference": 1.0},
            "control_receipt": context.audit["controls_reused"]["control_receipt"]["path"],
            "control_status": "cached_controls_reused", "new_control_executions": 0,
            "new_model_execution": False, "measurement_scope": "actual_native_evaluation_correction_repaired_postgrade",
            "flags": source["flags"], "original_source": source, "variant": context.pin,
            "native_result": native_ref, "native_replay_artifacts": artifacts,
            "native_evaluation_postgrade": context.audit, "notice": NOTICE}


def joined_correction(context: Context, summary: Path) -> dict[str, Any]:
    p, _, _, _, _ = modules()
    row = p.join_one(context.completion["source"], context.pin, p.receipt_rows([summary]))
    require(row["status"] == "measured" and row["repaired_reward"] in (0.0, 1.0),
            "actual native replay did not produce a measured canonical repaired observation")
    row.update(schema=CORRECTION_SCHEMA, purpose="evaluation", heldout=True, training_allowed=False, training_exported=False)
    row["provenance"].update(correction=context.request["provenance"]["correction"], native_evaluation_postgrade=context.audit)
    return row


def checked_analysis_row(row: dict[str, Any], plan: dict[str, Any], task: dict[str, Any], evidence: Any) -> dict[str, Any]:
    """Offline analyzer adapter; never dispatch or excuse an original-plan guard."""
    p, c, evaluation, _, _ = modules()
    same(row.get("schema"), CORRECTION_SCHEMA, "unknown actual-correction adapter schema")
    audit = row["provenance"]["native_evaluation_postgrade"]
    same(audit.get("schema"), SCHEMA, "unknown actual native postgrade provenance")
    context = checked_context(audit["request_input"], audit["execution_binding"])
    same(context.audit, audit, "actual correction audit differs from retained native/request/control evidence")
    same(p.read_json(evaluation.FIXED_PLAN), plan, "actual correction belongs to another fixed schedule")
    same(task["task"], context.request["task"], "actual correction maps to another scheduled task")
    selected = row["provenance"]["selected_receipt"]
    c.checked_evidence(selected)
    raw = selected["row"]
    native_ref = raw["native_result"]
    native = p.read_json(Path(native_ref["path"]))
    artifacts = native_replay_artifacts(native, native_ref, context.grader,
        source_id=context.native["execution_id"], recorded=context.completion["native_reward"],
        text=context.text, p=p, cf=modules()[-1])
    same(raw, envelope(context, native, native_ref, artifacts), "canonical row is not the actual bound native correction replay")
    same(row, joined_correction(context, Path(selected["path"])), "actual corrected partition differs from its canonical native join")
    for ref in (audit["request_input"], audit["request"], audit["execution_binding"], native_ref,
                audit["corrected_patch"], audit["native_trajectory"], *artifacts["artifacts"].values()):
        evidence.check(ref)
    evidence.check({"path": str(Path(__file__).resolve()), "sha256": p.file_ref(Path(__file__))["sha256"]})
    return context.native


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests", type=Path, required=True, help="untouched prepared evaluation request JSONL")
    parser.add_argument("--request-id", help="optional exact selector; must match the actual execution's request")
    parser.add_argument("--executions", type=Path, required=True, help="actual run_corrections.py executions.jsonl")
    parser.add_argument("--execution", required=True, help="actual NEW native correction UUID, never its original a1 UUID")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--budget", type=Path, help="parent's existing shared $0.04 final-grade gate")
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--reserve-usd", type=float)
    parser.add_argument("--approval")
    parser.add_argument("--global-ledger", type=Path, help="parent's existing SHA-bound total-$4 exposure ledger")
    parser.add_argument("--reservation-id", default="evaluation-correction-final-grade")
    args = parser.parse_args()
    try:
        args.out = safe_path(args.out)
        require(args.out.parent == OWNED_ROOT and re.fullmatch(r"evaluation-postgrade-v[1-9][0-9]*", args.out.name),
                "output must be a new runs/envcheck-20261017/m3/evaluation-postgrade-vN directory")
        require(not args.out.exists(), "output already exists; implicit replay/result cache is forbidden")
        args.execution = str(UUID(args.execution))
        args.requests, args.executions = safe_path(args.requests), safe_path(args.executions)
        for name in ("budget", "global_ledger"):
            path = getattr(args, name)
            if path is not None:
                path = safe_path(path)
                require(path.is_relative_to(LANE) and not path.is_relative_to(args.out), "parent budget/ledger must stay outside the new output in this lane")
                setattr(args, name, path)
        for name in ("max_usd", "reserve_usd"):
            value = getattr(args, name)
            if value is not None:
                require(math.isfinite(value) and money(value) == CAP_USD, "--max-usd and --reserve-usd must each be exactly 0.04")
        if args.execute:
            require(args.budget is not None and args.global_ledger is not None and args.max_usd is not None
                    and args.reserve_usd is not None and bool((args.approval or "").strip()),
                    "paid execution requires --execute --budget --max-usd 0.04 --reserve-usd 0.04 --approval --global-ledger")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = arguments()
    args.out.mkdir(parents=True, exist_ok=False)
    plan: dict[str, Any] = {"schema": SCHEMA, "created_at": replay.now(), "execute": args.execute,
        "notice": NOTICE, "purpose": "evaluation", "heldout": True, "training_allowed": False,
        "new_model_executions": 0, "provider_calls": 0, "new_control_executions": 0,
        "selected_execution_id": args.execution, "planned_native_source_replays": 1, "modal": replay.MODAL,
        "resources": {"cpus_max": 2, "memory_mb_max": 8192, "gpus": 0}, "status": "checking_actual_prerequisites"}
    context = gate = None
    problems = []
    native_ref = None
    measured = False
    try:
        p, _, _, helper, cf = modules()
        require(float(CAP_USD) >= cf.CLOUD_USD_PER_EXECUTION,
                "fixed postgrade reservation is below the native CF execution floor")
        binding = helper.execution_binding(args.executions, args.execution)
        actual = bound_row(binding, p)
        request_input = request_binding(args.requests, actual, args.request_id, p)
        context = checked_context(request_input, binding)
        plan.update(status="ready_not_executed", inputs=context.audit, observed_student_executions=1,
                    observed_model_requests=context.completion["execution"].get("observed_model_requests"),
                    recorded_weak_original_reward=context.completion["native_reward"], repaired_reward=None)
        grant = args.budget is not None and args.global_ledger is not None and args.max_usd is not None \
            and args.reserve_usd is not None and bool((args.approval or "").strip())
        if grant:
            gate = EvaluationBudget(args, p)
            require(gate.native.executions == 0, "the single postgrade reservation was already consumed; no implicit retry/replay is authorized")
            plan["budget"] = {"path": str(gate.path), "initial_state": gate.initial,
                              "global_ledger": gate.ledger_ref, "reservation_id": gate.reservation_id,
                              "additional_reservation_usd": 0.04}
        require(os.environ.get("EVALLAB_NO_PROCESS_JOB") != "1", "official native processing is disabled by the environment")
    except (Exception, KeyboardInterrupt) as exc:
        problems.append({"type": type(exc).__name__, "message": str(exc)})
        plan.update(status="refused_actual_prerequisite", problems=problems)
    replay.write_json(args.out / "plan.json", plan, exclusive=True)
    if args.execute and not problems:
        try:
            require(context is not None and gate is not None, "missing actual bound correction or parent budget")
            native, native_ref, artifacts = execute_once(context, args.out, gate)
            canonical = envelope(context, native, native_ref, artifacts)
            replay.write_json(args.out / "summary.json", {"schema": SCHEMA, "rows": [canonical], "notice": NOTICE}, exclusive=True)
            row = joined_correction(context, args.out / "summary.json")
            with (args.out / "correction-rows.jsonl").open("xb") as stream:
                stream.write(p.canonical_bytes(row) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            measured = True
        except (Exception, KeyboardInterrupt) as exc:
            problems.append({"type": type(exc).__name__, "message": str(exc)})
    retained_native = args.out / "source/native.json"
    if native_ref is None and retained_native.is_file():
        native_ref = {"path": str(retained_native), "sha256": replay.sha256(retained_native.read_bytes())}
    receipt = {"schema": SCHEMA, "finished_at": replay.now(), "notice": NOTICE,
        "status": "measured_actual_repaired_grade" if measured else "native_replay_unmeasured" if args.execute and gate and gate.charges_this_run
                  else "refused_actual_prerequisite" if problems else "plan_only_not_executed",
        "plan": {"path": str(args.out / "plan.json"), "sha256": replay.sha256((args.out / "plan.json").read_bytes())},
        "new_model_executions_this_invocation": 0, "provider_calls_this_invocation": 0,
        "actual_student_execution_id": context.native["execution_id"] if context else None,
        "original_a1_source_execution_id": context.request["source_execution_id"] if context else None,
        "recorded_weak_original_reward": context.completion["native_reward"] if context else None,
        "repaired_reward": row["repaired_reward"] if measured else None,
        "new_native_replay_reservations": gate.charges_this_run if gate else 0,
        "native_matrix_dispatch_attempts": gate.dispatches_this_run if gate else 0,
        "native_result": native_ref, "new_nop_reference_executions": 0,
        "cached_controls_reused": context.audit["controls_reused"] if context else None,
        "training_exports": 0, "problems": problems}
    if gate is not None:
        receipt["budget"] = {"path": str(gate.path), "initial_state": gate.initial,
                             "charge_journal": str(gate.journal) if gate.execute else None,
                             "reservations_are_not_invoices": True}
    replay.write_json(args.out / "receipt.json", receipt, exclusive=True)
    print(replay.json_bytes({key: receipt[key] for key in ("status", "actual_student_execution_id", "original_a1_source_execution_id",
        "recorded_weak_original_reward", "repaired_reward", "new_model_executions_this_invocation", "native_matrix_dispatch_attempts", "problems")}).decode())
    return 0 if measured or not args.execute and not problems else 3


if __name__ == "__main__":
    raise SystemExit(main())
