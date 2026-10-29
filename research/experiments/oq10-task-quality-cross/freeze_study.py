"""Two-level OQ10 custody: freeze code/metadata, then private case inputs.

Importing this module never imports an adapter, loads labels, or calls a model.
All corpus preparation is explicit and follows a positive role-local route proof.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import study

SCHEMA_CODE = "oq10.code-policy-freeze/v1"
SCHEMA_INPUTS = "oq10.opaque-input-freeze/v1"
ROLES = {
    "sonnet": "anthropic/claude-sonnet-5-5",
    "haiku": "anthropic/claude-haiku-5-5",
    "kimi": "kimi-code/k3.1-pro",
}
SEED = "oq10-cross-v1-20261028"
CORPORA = ("automationbench-verified", "paichecker")
TELEMETRY_PREFIXES = (
    "LAMINAR", "LMNR", "DOCENT", "OTEL", "OPENLIT", "LANGSMITH",
    "LANGCHAIN_TRACING", "PHOENIX", "HELICONE", "BRAINTRUST", "TRACERLOOP", "ARIZE",
)


def environment_guard() -> None:
    if os.environ.get("EVALLAB_DISABLE_LOCAL_DOCKER") != "1":
        raise ValueError("local Docker must remain disabled")
    if any(name.upper().startswith(TELEMETRY_PREFIXES) for name in os.environ):
        raise ValueError("third-party telemetry environment name; values not inspected")
    if any(name.upper().startswith(TELEMETRY_PREFIXES) for name in sys.modules):
        raise ValueError("third-party telemetry module loaded")


def load_module(name: str, value: Mapping[str, Any]) -> Any:
    path = study.verify_pin(value)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("frozen Python module has no loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def snapshot(source: Path, destination: Path) -> dict[str, Any]:
    if source.is_symlink():
        raise ValueError("frozen code/metadata source must be a regular file")
    original = study.pin(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(source.read_bytes())
    copied = study.pin(destination)
    if copied["sha256"] != original["sha256"]:
        raise ValueError("snapshot changed input bytes")
    destination.chmod(0o444)
    return {**copied, "original_path": original["path"]}


def regular_relative(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if name.is_absolute() or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("unsafe staged source path")
    path = root
    for part in name.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("staged source symlink is not admitted")
    if not path.is_file():
        raise ValueError("required staged source is unavailable")
    return path


def bundle_manifest(bundle: Path) -> dict[str, Any]:
    """Read staging metadata only; never open a listed task or label file here."""
    path = bundle / "STAGE-MANIFEST.json"
    value = json.loads(path.read_bytes())
    rows = list(value["files"])
    sources = value.get("sources", ())
    if isinstance(sources, list):
        for source in sources:
            rows.extend(source.get("files", ()))
    files: dict[str, str] = {}
    for row in rows:
        relative, digest = row["path"], row["sha256"]
        if not isinstance(relative, str) or not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("staging manifest lacks an exact source checksum")
        if relative in files and files[relative] != digest:
            raise ValueError("conflicting source checksums in staging manifest")
        files[relative] = digest
    return {"bundle": str(bundle.resolve()), "manifest": study.pin(path),
            "revision": value["revision"], "pins": {key: item for key, item in value.get("pins", {}).items() if key != "zip_members"},
            "source_revisions": [{"repo": row["repo"], "revision": row["revision"]} for row in sources] if isinstance(sources, list) else [],
            "files": files}


def verify_bundle(value: Mapping[str, Any]) -> None:
    study.verify_pin(value["manifest"])
    root = Path(value["bundle"])
    for relative, digest in value["files"].items():
        with regular_relative(root, relative).open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != digest:
            raise ValueError("staged bundle differs from the prospectively pinned manifest")


def code_freeze(config: Mapping[str, Any], *, own_source: Path, output: Path) -> dict[str, Any]:
    """Close code/policy before ANY real population, source or label hydration."""
    environment_guard()
    if (output / "code-freeze.json").exists():
        raise ValueError("code freeze already exists; never overwrite a registered version")
    output.mkdir(parents=True, exist_ok=True)
    own = {file.name: snapshot(file, output / "frozen-code" / file.name)
           for file in sorted(own_source.glob("*.py"))}
    required = {"study.py", "freeze_study.py", "run_study.py", "native_route.py", "automation_source.py", "pai_source.py", "scoring.py", "report_metrics.py"}
    required.add("authority.py")
    if not required.issubset(own):
        raise ValueError("code freeze requires the complete actual driver, not a scaffold")
    library_root = output / "frozen-library"
    library = snapshot(Path(config["library"]), library_root / "score_task_qa_gate.py")
    registry = snapshot(Path(config["cross_registry"]), library_root / "cross-test-registry.json")
    adapters = {}
    for corpus in CORPORA:
        relative = config["adapters"][corpus]
        adapters[corpus] = snapshot(Path(config["worktree"]) / relative, output / "frozen-eval-lab" / relative)
    retained = {name: study.pin(Path(path)) for name, path in config["retained"].items()}
    runtime = Path(config["runtime_root"])
    environment = {
        "python": sys.version, "prefix": str(Path(sys.prefix).resolve()),
        "python_binary": study.pin(Path(sys.executable).resolve()),
        "omp_binary": study.pin(Path(shutil.which("omp")).resolve()),
        "distributions": {name: importlib.metadata.version(name) for name in ("numpy", "pyarrow", "pydantic")},
    }
    core = [study.pin(path) for path in sorted((runtime / "src" / "evallab").rglob("*.py"))]
    authority_root = Path(config["registry_authority_root"]).resolve()
    projection_path = authority_root / "projection-receipt.json"
    projection = json.loads(projection_path.read_bytes())
    if projection["schema"] != "oq10.full-metadata-registry-projection/v1" or projection["root"] != str(authority_root):
        raise ValueError("registry root is not the prospectively created private metadata projection")
    if projection["projector"]["sha256"] != own["authority.py"]["sha256"]:
        raise ValueError("registry metadata came from another projector implementation")
    consumer = study.pin(runtime / "src/evallab/production/corpus_queue.py")
    if projection["registry_consumer"]["sha256"] != consumer["sha256"]:
        raise ValueError("registry metadata was validated by another policy implementation")
    expected_files = {
        "research/answer-keys/splits/assignments.csv",
        "research/answer-keys/tb4-audit-v1/tasks.jsonl",
        "research/experiments/mimo-defect-map-v1/inventory.json",
        *(f"research/answer-keys/{name}/items.jsonl" for name in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1")),
    }
    if {str(Path(item["path"]).relative_to(authority_root)) for item in projection["projection_files"]} != expected_files:
        raise ValueError("metadata projection does not bind exactly the full six identity files")
    authority = [study.pin(projection_path), *projection["projection_files"]]
    if {str(Path(path).resolve()) for path in config["authority_files"]} != {item["path"] for item in authority}:
        raise ValueError("configured registry files differ from the metadata projection")
    for item in authority:
        study.verify_pin(item)
    canonical_ledger = snapshot(Path(config["canonical_ledger"]), output / "authority-inputs/assignments.csv")
    provenance = [snapshot(Path(path), output / "provenance" / f"{index:02d}-{Path(path).name}")
                  for index, path in enumerate(config["provenance_files"])]
    if study.sha(Path(config["registry_authority_root"]).joinpath("research/answer-keys/splits/assignments.csv").read_bytes()) != canonical_ledger["sha256"]:
        raise ValueError("retrieval authority is not bound to the current full split ledger")
    bundles = {corpus: bundle_manifest(Path(config["bundles"][corpus])) for corpus in CORPORA}
    native = load_module("oq10_freeze_native", own["native_route.py"])
    generated = native.prepare_runtime(
        Path(config["retained"]["quota"]), retained["quota"]["sha256"],
        Path(config["retained"]["transport"]), retained["transport"]["sha256"], output / "native-runtime",
    )
    study.dump_new(output / "native-runtime.json", generated)
    value = {
        "schema": SCHEMA_CODE, "config": dict(config), "own": own, "library": library, "registry": registry,
        "adapters": adapters, "retained": retained, "core": core, "authority": authority,
        "provenance": provenance, "frozen_at": dt.datetime.now(dt.UTC).isoformat(),
        "environment": environment,
        "canonical_ledger": canonical_ledger, "bundles": bundles,
        "native_runtime": study.pin(output / "native-runtime.json"),
        "roles": ROLES, "seed": SEED, "planned_tasks": 180, "planned_role_slots": 540,
        "sampling": {"automationbench-verified": "30 verified + 30 unchanged, exactly domain-matched, all seven positive domains where available",
                     "swe_bench_multilingual": "30 human misaligned + 30 aligned, repo-stratified",
                     "swe_gym": "30 human misaligned + 30 aligned, repo-stratified"},
        "condition": "unchanged OQ6 B; selected on Sonnet DEV causal-key recall only, not cross results",
        "role_execution": "independent route-local proof/admission; Haiku ranking never drops tasks or enters detector prompts; Sonnet and Kimi independently judge all 180",
        "order": "label-independent round-robin corpus/subcorpus bands, retaining each hash sampler's within-band order; Haiku ranking reported separately and never a detector dependency",
        "gates": list(study.GATES), "source_bounds": {"per_file": 70000, "extraction": 1000000, "package": 180000, "historical_context": 16000},
        "thinking": "high", "tokenizers": study.TOKENIZERS,
        "missingness": "unsent quota/provider-wait remains pending; sent errors/refusals/wrong identity/parse failures are terminal missing; no consumed role replay",
        "cash": "native subscriptions only; USD not applicable; paid transport not enabled",
        "storage": "private local case inputs/native receipts only; no CorpusRegistry/live CloudDB/evidence-CAS/retrieval/shared trace-vendor insertion",
        "new_body_or_label_rows_read_at_freeze": 0, "study_calls_at_freeze": 0,
    }
    study.dump_new(output / "code-freeze.json", value)
    return study.pin(output / "code-freeze.json")


def load_code(path: Path, expected_sha256: str) -> dict[str, Any]:
    environment_guard()
    value = json.loads(study.verify_pin({"path": str(path), "sha256": expected_sha256}).read_bytes())
    if value["schema"] != SCHEMA_CODE or value["roles"] != ROLES or value["planned_tasks"] != 180:
        raise ValueError("unexpected frozen OQ10 policy")
    for name, module in (("freeze_study.py", sys.modules[__name__]), ("study.py", study)):
        if Path(module.__file__).resolve() != Path(value["own"][name]["path"]).resolve():
            raise ValueError("execute frozen private custody modules, never mutable checkout imports")
    environment = value["environment"]
    if environment["python"] != sys.version or environment["prefix"] != str(Path(sys.prefix).resolve()):
        raise ValueError("Python runtime differs from its prospective freeze")
    if str(Path(shutil.which("omp")).resolve()) != environment["omp_binary"]["path"]:
        raise ValueError("native executable resolution drifted")
    if any(importlib.metadata.version(name) != version for name, version in environment["distributions"].items()):
        raise ValueError("scoring/parser dependency version drifted")
    pins = list(value["own"].values()) + list(value["adapters"].values()) + list(value["retained"].values())
    pins += value["core"] + value["authority"] + [value["canonical_ledger"], value["library"], value["registry"], value["native_runtime"]]
    pins += value["provenance"]
    pins += [environment["python_binary"], environment["omp_binary"]]
    for item in pins:
        study.verify_pin(item)
    return value


def positive_route_proof(code: Mapping[str, Any], *, registration: Path, result: Path) -> str:
    """Re-attest a consumed synthetic slot only; this function cannot send a probe."""
    registered = json.loads(registration.read_bytes())
    observed = json.loads(result.read_bytes())
    route = registered["requested_route"]
    if route not in code["roles"].values() or observed.get("sent") is not True or observed.get("disposition") != "answered":
        raise ValueError("planned route has no positive consumed synthetic proof")
    directory = result.parent.parent / "call"
    if not (directory / "call-consumed.json").is_file() or not (directory / "receipt.json").is_file():
        raise ValueError("synthetic proof has no actual consumed native receipt")
    native = load_module("oq10_proof_native", code["own"]["native_route.py"])
    proof_manifest = json.loads(study.verify_pin(registered["runtime_manifest"]).read_bytes())
    frozen_manifest = json.loads(Path(code["native_runtime"]["path"]).read_bytes())
    if proof_manifest["controlled_config"]["sha256"] != frozen_manifest["controlled_config"]["sha256"]:
        raise ValueError("synthetic proof used a different effective model profile")
    for group in ("originals", "generated", "dependencies"):
        proof_digests = {key: item["sha256"] for key, item in proof_manifest[group].items()}
        frozen_digests = {key: item["sha256"] for key, item in frozen_manifest[group].items()}
        if proof_digests != frozen_digests:
            raise ValueError("synthetic proof used different native transport/attestation/guard bytes")
    if registered["producer"]["sha256"] != code["own"]["native_route.py"]["sha256"]:
        raise ValueError("synthetic proof used another native isolation implementation")
    if registered["native_binary"]["sha256"] != code["environment"]["omp_binary"]["sha256"]:
        raise ValueError("synthetic proof used another native executable")
    # Loading the original proof producer preserves its own producer/schema pins.
    proof_native = load_module("oq10_consumed_proof_native", registered["producer"])
    replay = proof_native.run_native(
        directory, route=route, prompt=registered["prompt"], system=registered["system"],
        runtime_manifest=proof_manifest, timeout_seconds=180,
    )
    if replay["disposition"] != "answered" or replay["evidence"].get("served_identities") != [route] or replay["evidence"].get("native_tool_inventory_proven_empty") is not True:
        raise ValueError("synthetic native serving/isolation attestation failed")
    if proof_manifest["route_context_tokens"][route] != frozen_manifest["route_context_tokens"][route] or native.ROUTE_CONTEXT_TOKENS[route] != frozen_manifest["route_context_tokens"][route]:
        raise ValueError("route window drifted from its native catalog freeze")
    return route


def prepare_inputs(code_path: Path, code_sha256: str, *, proof_registration: Path, proof_result: Path) -> dict[str, Any]:
    code = load_code(code_path, code_sha256)
    positive_route_proof(code, registration=proof_registration, result=proof_result)
    output, config = code_path.parent, code["config"]
    if (output / "inputs-freeze.json").exists():
        raise ValueError("opaque cohort already frozen; never resample or replace")
    for bundle in code["bundles"].values():
        verify_bundle(bundle)
    import evallab
    from evallab.production.cloud_trace_db import (
        CloudTraceDatabase,
        CloudTraceRetriever,
        admitted_cloud_task_ids,
    )
    from evallab.production.corpus_queue import CorpusRegistry
    from evallab.production.tq_context import assemble_audit_context

    if Path(evallab.__file__).resolve() != Path(config["runtime_root"]) / "src/evallab/__init__.py":
        raise ValueError("actual production import root differs from the frozen runtime")
    retained = load_module("oq10_retained_preparation", code["retained"]["prepare_inputs"])
    retained.ROOT = output / "dev-context"
    retained.ROOT.mkdir(exist_ok=False)
    registry = CorpusRegistry(repo_root=config["registry_authority_root"])
    if registry.split_assignments_digest != "sha256:" + code["canonical_ledger"]["sha256"]:
        raise ValueError("actual all-key registry did not load the frozen canonical ledger")
    fixture = study.verify_pin(code["retained"]["fixture"])
    if code["retained"]["fixture"]["sha256"] != retained.FIXTURE_SHA:
        raise ValueError("fixed DEV context does not bind the unchanged eight-task fixture")
    with sqlite3.connect(f"file:{fixture}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        identities = [dict(row) for row in connection.execute(
            "SELECT task_id,dataset_id,split,json_extract(metadata_json,'$.item_id') AS item_id,"
            "json_type(metadata_json,'$.item_id') AS item_type,original_grader_digest,repaired_grader_digest FROM corpus_tasks"
        )]
    admitted = admitted_cloud_task_ids(identities, registry=registry)
    if not admitted or any(row["task_id"] not in admitted for row in identities):
        raise ValueError("fixed DEV fixture contains an unadmitted identity; no content query issued")
    # clone_fixture computes logical hashes of issue/trace columns. It must run
    # only AFTER metadata-only CURRENT-ledger admission, not merely before prompts.
    working = retained.clone_fixture()
    study.dump_new(output / "dev-context-admission.json", {
        "schema": "oq10.every-key-dev-fixture-admission/v1", "fixture_sha256": retained.FIXTURE_SHA,
        "canonical_ledger_sha256": code["canonical_ledger"]["sha256"],
        "identity_count": len(identities), "admitted_count": len(admitted),
        "admitted_task_id_sha256": sorted(study.sha(task.encode()) for task in admitted),
        "current_all_key_test_precedence": True, "new_cross_records_inserted": 0,
    })
    automation = load_module("oq10_automation_source", code["own"]["automation_source.py"])
    pai = load_module("oq10_pai_source", code["own"]["pai_source.py"])
    adapters = {key: load_module("oq10_adapter_" + str(index), value) for index, (key, value) in enumerate(code["adapters"].items())}
    selected = {}
    for corpus, helper in ((CORPORA[0], automation), (CORPORA[1], pai)):
        population = helper.metadata_population(adapters[corpus], Path(config["bundles"][corpus]))
        selected[corpus] = helper.select_cases(population, seed=SEED, positives=30, negatives=30)
    ordered = study.ordered_selection(selected)
    if len(ordered) != 180:
        raise ValueError("fixed 180-task cohort unavailable; no silent narrowing")
    study.dump_new(output / "scorer-only" / "selection.json", {"cases": ordered, "seed": SEED})
    public, sealed = [], []
    with CloudTraceDatabase(working) as database:
        retriever = CloudTraceRetriever(database, registry=registry)
        for corpus, helper in ((CORPORA[0], automation), (CORPORA[1], pai)):
            mapping = {row["task_id"]: row for row in ordered if row["corpus"] == corpus}
            for record in helper.iter_selected(adapters[corpus], Path(config["bundles"][corpus]), selected[corpus]):
                selected_row = mapping[record["task_id"]]
                native_identity = record["identity"]
                record_binding = study.sha(json.dumps({
                    "corpus": corpus, "task_id": record["task_id"], "identity": native_identity,
                    "source_record_sha256": record.get("source_record_sha256"),
                    "source_bundle_manifest_sha256": code["bundles"][corpus]["manifest"]["sha256"],
                }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
                record = {**record, "record_binding_sha256": record_binding}
                target = native_identity.get("instance_id", native_identity.get("task_name", record["task_id"]))
                context = assemble_audit_context(retriever, task_id=target, query="test", source_digest=retained.FIXTURE_SHA)
                if len(context["related_issues"]) != 3 or any(row["repair_diff"] is None for row in context["related_issues"]):
                    raise ValueError("unchanged three-diff DEV context is incomplete")
                display = retained.blinded_context(context)
                case = study.prepare_case(record, case_id=selected_row["case_id"], output=output,
                                          reader=retained.reader, render=retained.prompt_renderer.render,
                                          display_context=display, context_provenance=context)
                case["corpus"] = corpus
                public.append(case)
                sealed.append({"case_id": case["case_id"], "corpus": corpus,
                               "subcorpus": selected_row["subcorpus"], "task_id": record["task_id"],
                               "identity": native_identity, "source_record_sha256": record.get("source_record_sha256"),
                               "record_binding_sha256": record_binding})
    scoring = load_module("oq10_frozen_scoring", code["own"]["scoring.py"])
    scoring.bind_inputs(public, sealed)
    study.dump_new(output / "public-cases.json", {"cases": public})
    study.dump_new(output / "scorer-only" / "identities.json", {"cases": sealed})
    value = {"schema": SCHEMA_INPUTS, "code_freeze": study.pin(code_path),
             "frozen_at": dt.datetime.now(dt.UTC).isoformat(),
             "public_cases": study.pin(output / "public-cases.json"),
             "sealed_cases": study.pin(output / "scorer-only/identities.json"),
             "selection": study.pin(output / "scorer-only/selection.json"),
             "dev_admission": study.pin(output / "dev-context-admission.json"),
             "prepared_counts": dict(Counter(row["preparation_status"] for row in public)),
             "planned_tasks": 180, "planned_role_slots": 540, "model_calls_before_input_freeze": 0,
             "proof_registration": study.pin(proof_registration), "proof_result": study.pin(proof_result)}
    study.dump_new(output / "inputs-freeze.json", value)
    return study.pin(output / "inputs-freeze.json")
