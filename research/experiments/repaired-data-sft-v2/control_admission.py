"""Providerless custody audit of the fixed phase-2 repaired-training controls.

This reads retained native jobs; it never executes a task or an archived script.
A control-qualified candidate is deliberately not a validated ledger descendant.
The report is an input to the separately gated, generation-only native consumer.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib.util
import json
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evallab.counts import task_index_for
from evallab.registry import compute_subpath_digest, compute_task_digests, harbor_task_digest
from evallab.setup_fingerprint import lineage_ledger_binding, load_reference_profile
from evallab.task_variants import VariantRecord, derive_task, lineage_chain, materialize, verify

SCHEMA = "repaired_data_sft_v2.control_admission/v1"
HANDOFF_SHA256 = "ca3d4460296cdfa017741d3557f3455bccf74b8f26f24643ff4a2c1f50501e80"
REQUEST_SHA256 = "eabdefff50840b7102ac680b9f729915eb41d0e50d234e2c9680686d945f5a33"
ARCHIVE_SHA256 = "59337ad771f180f016965096ffafea879ff55125872df62a53a30b53c814bd62"
RAW_REPORTS = {
    "regular": ("mimo-v7-phase2-regular-20261017.json", "3af8e5b874206b13c7c27281e589800435720d664e130bc477f7d64722831cb6"),
    "overrides": ("mimo-v7-phase2-overrides-20261017.json", "4c3064f734fccfbaac6e1a0067ae251e469f48d07c163df96afcb4a8f2615618"),
}
STRICT_TASK_IDS = (
    "000089", "000125", "000169", "000354", "000561", "000792", "000931",
    "000945", "001205", "001267", "001269", "001750", "001804", "001809",
    "001985", "002160", "002216", "002391", "002709",
)
SELECTED_TASK_IDS = tuple(sorted((*STRICT_TASK_IDS, "002486")))
NAMESPACE = "mimo-v2.6-rl/format-code-task-"
COMPONENTS = ("environment", "instruction", "task_toml", "verifier")
LIMITS = [
    "Current providerless re-reading of retained bytes, not new native execution or fresh model rollouts.",
    "RepeatVerifier observations within one physical trial are not independent sandboxes.",
    "Reference/nop and recorded-wrong discrimination do not establish general semantic correctness.",
    "000792 reference is the retained known-honest HAR202 solution-only override, not packaged solve.sh.",
    "001269 alternate establishes only the retained synchronous prompt-valid case; its wrong control has one observation.",
    "002391 sc2/sc3 are QA-adjudicated correct alternatives and must pass, not wrong submissions.",
    "002486 remains excluded: its nop/negative evidence includes synthetic loader-only cases; HAR161 is unadjudicated.",
    "All original frozen33 IDs and answer-visible 001478 are excluded, including every descendant of those task IDs.",
    "Canonical candidate status and original local-parent source bytes are unchanged; no validated or locked-nop claim.",
    "No held-out scoring, replay authorization, dataset validation, token-match relaxation or training permission is granted.",
]


def file_binding(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"path": str(path.resolve()), "sha256": "sha256:" + digest, "bytes": path.stat().st_size}


def require_binding(path: Path, sha256: str, *, size: int | None = None) -> dict[str, Any]:
    actual = file_binding(path)
    if actual["sha256"].removeprefix("sha256:") != sha256.removeprefix("sha256:"):
        raise ValueError(f"source SHA256 mismatch: {path}")
    if size is not None and actual["bytes"] != size:
        raise ValueError(f"source size mismatch: {path}")
    return actual


def write_json(path: Path, value: Any) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return file_binding(path)


def load_current_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import current first-party reader: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_scope(request: dict[str, Any], handoff: dict[str, Any], cohort: dict[str, Any],
                   frozen: dict[str, Any]) -> dict[str, Any]:
    candidates = [row["task"] for row in request["candidates"]]
    if sorted(candidates) != [NAMESPACE + task for task in SELECTED_TASK_IDS]:
        raise ValueError("request is not the exact20 source cohort")
    if sorted(handoff["selected_task_ids"]) != list(SELECTED_TASK_IDS):
        raise ValueError("handoff is not the exact20 source cohort")
    if sorted(row["task_id"] for row in handoff["tasks"]) != list(SELECTED_TASK_IDS):
        raise ValueError("handoff task records are not the exact20 source cohort")
    blocked = {name.removeprefix("mimo-v2.6-rl/") for name in request["heldout_exclusion"]["all_parent_and_descendant_ids"]}
    if (len(blocked) != 33 or blocked != set(cohort["training_exclusion"]["parent_task_ids"])
            or blocked != set(frozen["heldout"]) or not set(cohort["task_ids"]) <= blocked):
        raise ValueError("frozen33 exclusion differs from the frozen manifest/cohort")
    explicit = {row["task"] for row in request["selection"]["explicit_training_exclusions"]}
    if NAMESPACE + "001478" not in explicit:
        raise ValueError("answer-visible001478 exclusion is missing")
    if any(task.removeprefix("mimo-v2.6-rl/") in blocked or task in explicit for task in candidates):
        raise ValueError("source candidates overlap held-out/answer-visible task IDs")
    return {
        "strict_task_ids": list(STRICT_TASK_IDS), "excluded_source_task_ids": ["002486"],
        "frozen33_task_ids": sorted(blocked), "explicit_training_exclusions": sorted(explicit),
        "fresh_K": 4, "fresh_attempts_requested": 76, "fresh_attempts_executed": 0,
        "descendants_excluded_by_original_task_id": True,
    }


def literal_override_script(recipe: bytes, patch: bytes) -> bytes:
    """Interpret only a retained solve.write_text string expression, never code."""
    tree = ast.parse(recipe)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text"
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "solve"]
    if len(calls) != 1 or not calls[0].args:
        raise ValueError("retained recipe does not have exactly one solve.write_text expression")
    payload = base64.b64encode(patch).decode("ascii")

    def string(node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(string(value) for value in node.values)
        if (isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name)
                and node.value.id == "payload" and node.conversion == -1 and node.format_spec is None):
            return payload
        raise ValueError("retained recipe is not a literal/payload-only string expression")

    return string(calls[0].args[0]).encode("utf-8")


def identity(path: Path) -> dict[str, Any]:
    components = compute_task_digests(path).model_dump(mode="json")
    return {"package_path": str(path.resolve()), "package_digest": components["package"],
            "harbor_digest": harbor_task_digest(path), "components": components,
            "solution_digest": compute_subpath_digest(path / "solution")}


def compare_identity(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    components = expected.get("components", expected)
    package = expected.get("package_digest", components.get("package"))
    harbor = expected.get("harbor_digest", components.get("harbor"))
    if actual["package_digest"] != package or actual["harbor_digest"] != harbor:
        raise ValueError("actual package/Harbor identity differs from retained binding")
    for name in COMPONENTS:
        if actual["components"][name] != components[name]:
            raise ValueError(f"actual {name} identity differs from retained binding")
    if "solution_digest" in expected and actual["solution_digest"] != expected["solution_digest"]:
        raise ValueError("actual solution identity differs from retained binding")


def canonical_blockers(status: str, locks: list[dict[str, Any]], run_digest: str | None,
                       parent_digests: list[str]) -> list[str]:
    reasons = []
    if status != "validated":
        reasons.append(f"source record is {status}; retained controls do not authorize a status promotion")
    if not locks or any(lock.get("effective") != "locked" or lock.get("mode") != "locked" for lock in locks):
        reasons.append("retained nop setup fingerprint is open/not proven locked; locked-nop validation semantics are not met")
    if run_digest is None:
        reasons.append("no real task-ledger run_digest exists for this task")
    elif run_digest not in parent_digests:
        reasons.append("declared VariantRecord ancestry terminates before the real task-ledger run_digest")
    return reasons


def audit_inventory(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["path"], []).append(row)
    audited, failures = [], []
    for path, expected_rows in sorted(grouped.items()):
        result: dict[str, Any] = {"path": path, "expected_bindings": expected_rows, "failures": []}
        try:
            result["actual"] = actual = file_binding(Path(path))
            for expected in expected_rows:
                if (actual["sha256"].removeprefix("sha256:") != expected["sha256"].removeprefix("sha256:")
                        or actual["bytes"] != expected["bytes"]):
                    result["failures"].append("retained file SHA256/size mismatch")
                for binding in expected.get("retained_bindings", []):
                    sha = binding.get("expected_sha256")
                    size = binding.get("expected_bytes")
                    if (not isinstance(sha, str) or sha.removeprefix("sha256:") != actual["sha256"].removeprefix("sha256:")
                            or (size is not None and size != actual["bytes"])):
                        result["failures"].append("retained manifest binding mismatch")
        except (OSError, ValueError) as exc:
            result["failures"].append(f"{type(exc).__name__}: {exc}")
        result["qualified"] = not result["failures"]
        audited.append(result)
        if result["failures"]:
            failures.append(result)
    return audited, failures


def source_cases(regular: dict[str, Any], overrides: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    cases = {(task, case["case"]): case for task, row in regular["tasks"].items() for case in row["cases"]}
    for task, row in overrides["tasks"].items():
        for case in row["controls"]:
            cases[(task, case["name"])] = case
        for case in row.get("pilot", {}).get("controls", []):
            cases[(task, case["name"])] = case
    return cases


def native_file_custody(job: Path, expected: set[str], published: dict[str, Any] | None,
                        indexed: set[str], failed: set[str]) -> dict[str, Any]:
    """Bind original native bytes separately from process-job's publisher metadata."""
    actual = {str(path) for path in job.rglob("*") if path.is_file()}
    relocated = {row["retained_path"] for row in (published or {}).get("native_files", [])}
    missing = expected - actual
    extras = actual - expected
    publication_names = {"processed/job.json", "provenance.json", "uncommitted.diff"}
    publication_only = {
        path for path in extras
        if published and str(published.get("route", "")).startswith("automatic-native-process-job")
        and str(Path(path).relative_to(job)) in publication_names
    }
    failures = []
    if not expected or expected - indexed or expected & failed:
        failures.append("retained native source files are unindexed or failed actual custody hashing")
    if missing - relocated:
        failures.append("retained native files are missing without an exact custody relocation")
    if extras - publication_only:
        failures.append("unexpected current native files are absent from retained custody")
    # The source's original processed/job.json may be in native-supplements;
    # the republisher's different projection is not substituted as native proof.
    return {
        "actual_files": len(actual), "retained_source_files": len(expected),
        "retained_source_paths": sorted(expected), "relocated_source_paths": sorted(missing),
        "failed_files": sorted(expected & failed), "unindexed_source_paths": sorted(expected - indexed),
        "publication_artifacts_not_native_proof": [file_binding(Path(path)) for path in sorted(publication_only)],
        "unexpected_files": sorted(extras - publication_only), "failures": failures,
    }


def expected_vector(task: str, name: str, raw: dict[str, Any]) -> list[float]:
    if "expected_vector" in raw:
        return raw["expected_vector"]
    special = {
        ("000792", "reference-known-honest"): [1.0] * 3,
        ("000792", "nop"): [0.0] * 3,
        ("000792", "wrong-retained-HAR168a1"): [0.0] * 3,
        ("001269", "reference"): [1.0] * 3,
        ("001269", "nop"): [0.0] * 3,
        ("001269", "scope-limited-alternate"): [1.0] * 3,
        ("001269", "wrong-lenient"): [0.0],
        ("002486", "reference-archived-patch"): [1.0] * 3,
        ("002486", "nop"): [0.0] * 3,
        ("002486", "wrong-real-a2"): [0.0] * 3,
        ("002486", "HAR161-unadjudicated"): [1.0] * 3,
        ("002486", "pilot-nop"): [0.0],
        ("002486", "pilot-reference"): [1.0],
        ("002486", "pilot-same-wrong-source"): [0.0],
    }
    return special[(task, name)]


def effective_identity(raw: dict[str, Any], child: dict[str, Any], root: Path,
                       work: Path) -> dict[str, Any]:
    override = raw.get("solution_override")
    if override:
        source = override["source_patch"]
        patch = Path(source["path"])
        require_binding(patch, source["sha256"], size=source["bytes"])
        recipe = Path(override["recipe_path"])
        require_binding(recipe, override["recipe_sha256"])
        script = literal_override_script(recipe.read_bytes(), patch.read_bytes())
        expected_sha = override["reconstructed_script_sha256"]
        expected = override["effective_identity"]
        changed = override["changed_files"]
    elif raw.get("override", {}).get("changed_paths"):
        override = raw["override"]
        provenance = override["provenance"]
        if "script_path" in provenance:
            script_path = Path(provenance["script_path"])
            require_binding(script_path, override["script_sha256"])
            script = script_path.read_bytes()
        else:
            patch, recipe = Path(provenance["patch_path"]), Path(provenance["recipe_path"])
            require_binding(patch, provenance["patch_sha256"])
            require_binding(recipe, provenance["recipe_sha256"])
            script = literal_override_script(recipe.read_bytes(), patch.read_bytes())
        expected_sha, expected, changed = override["script_sha256"], override["identity"], override["changed_paths"]
    else:
        expected = raw.get("effective_identity") or raw.get("override", {}).get("identity")
        if expected:
            compare_identity(child, expected)
        return child
    if changed != ["solution/solve.sh"]:
        raise ValueError("retained override changes more than solution/solve.sh")
    if hashlib.sha256(script).hexdigest() != expected_sha.removeprefix("sha256:"):
        raise ValueError("literal-only retained override reconstruction SHA256 mismatch")
    digest = expected.get("package_digest", expected.get("package", expected.get("components", {}).get("package")))
    records = work / "override-records"
    store = work / "override-packages"
    package = store / (NAMESPACE + raw.get("_task_id", "")).replace("/", "__") / str(digest).removeprefix("sha256:")[:12]
    if not package.is_dir():
        record = derive_task(
            child["package_path"], changes={"solution/solve.sh": script},
            transform="retained-control-reconstruction@1", rationale="Providerless reproduction of retained solution-only bytes; not a new native control.",
            created_by="phase2-control-admission", repo_root=root, records_dir=records, variants_root=store,
        )
        package = materialize(record, child["package_path"], repo_root=root, records_dir=records, variants_root=store)
        errors = verify(record, parent_dir=child["package_path"], repo_root=root, records_dir=records, variants_root=store)
        if errors:
            raise ValueError(f"control override VariantRecord verification failed: {errors}")
    actual = identity(package)
    compare_identity(actual, expected)
    for component in COMPONENTS:
        if actual["components"][component] != child["components"][component]:
            raise ValueError(f"solution-only override changed {component}")
    return {**actual, "changed_paths": changed, "script_sha256": expected_sha,
            "reconstruction": "literal-only retained source; first-party derive_task/materialize/verify; no script executed"}


def copy_candidate(record: VariantRecord, source: Path, root: Path) -> dict[str, Any]:
    """Stage exact original bytes; never reserialize identity/history or append status."""
    if record.status != "candidate":
        raise ValueError("source endpoint must retain its candidate status")
    target = root / record.record_relpath()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise ValueError(f"canonical record collision: {target}")
    else:
        shutil.copyfile(source, target)
    if target.read_bytes() != source.read_bytes():
        raise ValueError("canonical copy changed source record history")
    return {**file_binding(target), "path": str(record.record_relpath()), "audited_path": str(target.resolve()),
            "status": record.status, "byte_identical_to_source": True, "status_evidence_appended": False}


def audit(*, root: Path, handoff_path: Path, request_path: Path, archive_path: Path,
          output: Path) -> dict[str, Any]:
    import tarfile

    # Pin inputs before creating any output or staging records.
    sources = [require_binding(handoff_path, HANDOFF_SHA256), require_binding(request_path, REQUEST_SHA256),
               require_binding(archive_path, ARCHIVE_SHA256)]
    handoff, request = json.loads(handoff_path.read_bytes()), json.loads(request_path.read_bytes())
    reports = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for name, (member, sha) in RAW_REPORTS.items():
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"missing retained JSON archive member: {member}")
            data = handle.read()
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError(f"retained raw report SHA256 mismatch: {member}")
            reports[name] = json.loads(data)
            sources.append({"path": str(archive_path) + ":" + member, "sha256": "sha256:" + sha, "bytes": len(data)})
    regular, overrides = reports["regular"], reports["overrides"]
    manifest_path = Path(request["source_manifest"]["path"])
    sources.append(require_binding(manifest_path, request["source_manifest"]["sha256"]))
    archive_root = manifest_path.parent
    cohort_path = root / "research/experiments/repaired-data-sft-v2/m1-cohort.json"
    frozen_path = root / "research/experiments/mimo-training-20261016/heldout.json"
    sources.extend((require_binding(cohort_path, request["heldout_exclusion"]["cohort_contract"]["sha256"]),
                    require_binding(frozen_path, request["heldout_exclusion"]["frozen_manifest_sha256"])))
    scope = validate_scope(request, handoff, json.loads(cohort_path.read_bytes()), json.loads(frozen_path.read_bytes()))
    output.mkdir(parents=True, exist_ok=False)
    inventory, inventory_failures = audit_inventory(regular["evidence_files"] + overrides["file_audits"])
    inventory_ref = write_json(output / "source-file-audit.json", {"files": inventory, "failures": inventory_failures})
    indexed_paths = {row["path"] for row in inventory}
    failed_paths = {row["path"] for row in inventory_failures}
    census_path = root / "research/experiments/mimo-v6-full-fleet/census.py"
    freeze_path = root / "research/experiments/mimo-training-20261016/freeze.py"
    census = load_current_module(census_path, "phase2_current_census")
    freeze = load_current_module(freeze_path, "phase2_current_freeze")
    current_sources = [file_binding(path) for path in (Path(__file__), census_path, freeze_path,
        root / "src/evallab/task_variants.py", root / "src/evallab/setup_fingerprint.py",
        root / "src/evallab/mimo_census.py", root / "src/evallab/task_health_filter.py",
        root / "src/evallab/registry.py", root / "src/evallab/counts.py")]
    custody_path = archive_root / "custody.json"
    custody = json.loads(custody_path.read_bytes())
    sources.append(file_binding(custody_path))
    snapshots = {row["package_digest"]: row for row in custody["package_snapshots"]}
    published_jobs = {row["published"]: row for row in custody["native_jobs"]}
    record_audit, record_errors = [], []
    for row in custody["record_snapshots"]:
        item = {"source": row["original"], "archived": row["archived"], "failures": []}
        try:
            path = Path(row["archived"]["path"])
            item["actual"] = require_binding(path, row["archived"]["sha256"])
            if row["original"]["sha256"].removeprefix("sha256:") != item["actual"]["sha256"].removeprefix("sha256:"):
                raise ValueError("archived record differs from original source SHA256")
            record = VariantRecord.model_validate(json.loads(path.read_bytes()))
            item.update(task=record.task_name, package_digest=record.variant_digest, parent=record.parent.model_dump(mode="json"), status=record.status)
        except (OSError, ValueError) as exc:
            item["failures"].append(f"{type(exc).__name__}: {exc}")
            record_errors.append(item)
        record_audit.append(item)
    record_ref = write_json(output / "source-record-audit.json", {"records": record_audit, "failures": record_errors})
    children, tasks = {}, []
    candidates = {row["task"].rsplit("-", 1)[-1]: row for row in request["candidates"]}
    source_tasks = {row["task_id"]: row for row in handoff["tasks"]}
    for task in SELECTED_TASK_IDS:
        candidate = candidates[task]
        row: dict[str, Any] = {"task_id": task, "task": candidate["task"], "failures": [], "native_controls": []}
        try:
            source_record = Path(candidate["native_variant_record"]["path"])
            row["source_record"] = require_binding(source_record, candidate["native_variant_record"]["sha256"])
            record = VariantRecord.model_validate(json.loads(source_record.read_bytes()))
            row["source_record"]["status"] = record.status
            if record.task_name != candidate["task"] or record.variant_digest != candidate["repaired_package"]["sha256"]:
                raise ValueError("typed source record does not bind the requested endpoint")
            parent_digest = candidate["frozen_v6_package_digest"]
            if record.parent.digest != parent_digest or candidate["repaired_child_v6_parent_digest"] != parent_digest:
                raise ValueError("record does not bind the exact requested frozen-v6 parent")
            snapshot = snapshots[parent_digest]
            parent_path = Path(snapshot["archived_path"])
            parent = identity(parent_path)
            compare_identity(parent, {"package_digest": record.parent.digest, "harbor_digest": record.parent.harbor_digest,
                                      "components": parent["components"]})
            row["frozen_v6_parent"] = parent
            original = identity(Path(candidate["repaired_package"]["path"]))
            expected = source_tasks[task]["identity"]
            compare_identity(original, expected)
            if task in STRICT_TASK_IDS:
                row["canonical_record"] = copy_candidate(record, source_record, root)
            rebuilt = materialize(record, parent_path, repo_root=root, variants_root=output / "materialized-packages")
            errors = verify(record, parent_dir=parent_path, repo_root=root, variants_root=output / "materialized-packages")
            if errors:
                raise ValueError(f"public VariantRecord verification failed: {errors}")
            row["identity"] = actual = identity(rebuilt)
            row["repaired_package"] = {"path": actual["package_path"], "sha256": actual["package_digest"],
                                       "harbor_digest": actual["harbor_digest"]}
            compare_identity(actual, expected)
            row["source_package"] = original
            children[task] = actual
            steps = lineage_chain(record, repo_root=archive_root)
            row["lineage"] = [{"package_digest": step.record.variant_digest,
                "harbor_digest": step.record.variant_harbor_digest, "transform": step.record.transform,
                "parent": step.record.parent.model_dump(mode="json"), "parent_found": step.parent_found,
                "parent_error": step.parent_error,
                "source_record": row["source_record"] if step.depth == 0 else file_binding(step.record_path),
            } for step in steps]
            if any(step.parent_error for step in steps):
                raise ValueError("declared source lineage has missing/cyclic ancestor records")
            ledger = task_index_for(root).status_for(candidate["task"].split("/", 1)[-1], record.variant_digest)
            parents = [step.record.parent.digest for step in steps]
            row["ledger_binding"] = {"ledger": ledger, "run_digest": ledger.get("run_digest"),
                "parent_digests": parents, "ledger_root_reached": ledger.get("run_digest") is not None and ledger["run_digest"] in parents,
                "terminal_parent_source": steps[-1].record.parent.source.model_dump(mode="json"),
                "available_record_for_terminal_parent": [r["actual"] for r in record_audit if r.get("package_digest") == parents[-1]],
                "unchanged_canonical_gate": lineage_ledger_binding(root, candidate["task"].split("/", 1)[-1], record.variant_digest)}
        except (OSError, ValueError, KeyError) as exc:
            row["failures"].append(f"{type(exc).__name__}: {exc}")
        tasks.append(row)
    task_rows = {row["task_id"]: row for row in tasks}
    raw_cases = source_cases(regular, overrides)
    declared_controls = handoff["native_controls"]
    metadata_paths = {Path(case["observation"]["metadata_path"]) for case in declared_controls}
    observations = {row["execution_id"]: row for row in census.native_observations(metadata_paths)}
    if len(observations) != len(declared_controls) or len(declared_controls) != 88:
        raise ValueError("native evidence does not identify exactly88 distinct physical trials")
    native_rows, proof_sources = [], {}
    for declared in declared_controls:
        task, name = declared["task_id"], declared["case"]
        case: dict[str, Any] = {"task_id": task, "case": name, "supplemental_pilot": declared["supplemental_pilot"],
            "scope": declared["semantic_scope"], "failures": []}
        try:
            raw = raw_cases[(task, name)]
            current = observations[declared["observation"]["execution_id"]]
            case.update(execution_id=current["execution_id"], observation=current, expected_vector=expected_vector(task, name, raw))
            if current != declared["observation"]:
                case["failures"].append("current first-party observation differs from SHA-pinned handoff observation")
            if current["rewards"] != case["expected_vector"]:
                case["failures"].append("native reward vector differs from role/adjudication-bound expectation")
            if not current["scored"]:
                case["failures"].append("current first-party positive-case/native identity/process gate did not score every observation")
            if not freeze._successor_repeat_proof(current, proof_sources):
                case["failures"].append("current first-party repeat proof failed")
            trial, job = Path(current["result_path"]).parent, Path(current["metadata_path"]).parent
            raw_files = raw.get("native_file_custody", raw.get("native_inventory", {})).get("files", [])
            expected_files = {str(item["path"] if isinstance(item, dict) else item) for item in raw_files}
            case["file_custody"] = native_file_custody(
                job, expected_files, published_jobs.get(str(job)), indexed_paths, failed_paths,
            )
            case["file_custody"]["source_file_audit"] = inventory_ref
            case["failures"].extend(case["file_custody"]["failures"])
            fingerprint_path = job / "setup-fingerprint.json"
            fingerprint = json.loads(fingerprint_path.read_bytes())
            case["setup_lock"] = fingerprint.get("lock", {})
            case["proof_refs"] = [file_binding(path) for path in (Path(current["metadata_path"]), Path(current["result_path"]),
                Path(current["task_binding"]["config_path"]), Path(current["task_binding"]["lock_path"]), fingerprint_path)]
            repeat_root = trial / "verifier/repeat"
            case["case_execution"] = []
            for index, reward in enumerate(current["rewards"]):
                repeat = repeat_root / str(index) if repeat_root.is_dir() else trial / "verifier"
                refs = [file_binding(path) for path in sorted(repeat.rglob("*")) if path.is_file()]
                case["case_execution"].append({"index": index if repeat_root.is_dir() else None, "reward": reward,
                    "positive_case_execution": census.positive_case_execution(repeat), "files": refs,
                    "independent_sandbox": False})
            raw["_task_id"] = task
            if declared["supplemental_pilot"]:
                # Supplemental evidence remains hash-bound but cannot replace strict repeat3 controls.
                case["effective_identity"] = {"scope": "supplemental002486 pilot; excluded from exact19 admission"}
            else:
                effective = effective_identity(raw, children[task], root, output)
                case["effective_identity"] = effective
                if (effective["package_digest"] != current["source_package_digest"]
                        or effective["harbor_digest"] != current["executed_harbor_digest"]):
                    case["failures"].append("reconstructed effective control package differs from actual native staging/lock identity")
        except (OSError, ValueError, KeyError) as exc:
            case["failures"].append(f"{type(exc).__name__}: {exc}")
        case["qualified"] = not case["failures"]
        native_rows.append(case)
        task_rows[task]["native_controls"].append(case)
    for task in tasks:
        controls = [case for case in task["native_controls"] if not case["supplemental_pilot"]]
        task["controls_qualified"] = (not task["failures"] and not inventory_failures and not record_errors
                                     and bool(controls) and all(case["qualified"] for case in controls))
        if task["task_id"] == "002486":
            task["controls_qualified"] = False
            task["failures"].append("explicit strict-scope hold: synthetic loader-only negatives and unadjudicated HAR161")
        ledger = task.get("ledger_binding", {})
        nops = [case for case in controls if case.get("observation", {}).get("agent") == "nop"]
        reasons = canonical_blockers(task.get("source_record", {}).get("status", "missing"),
            [case.get("setup_lock", {}) for case in nops], ledger.get("run_digest"), ledger.get("parent_digests", []))
        task["canonical_registration"] = {"admitted": False, "status_evidence_appended": False, "reasons": reasons}
        task["admission_basis"] = "control_qualified" if task["controls_qualified"] else "not_admitted"
        task["strict_assertion_scope_admission"] = task["controls_qualified"] and task["task_id"] in STRICT_TASK_IDS
        task["ledger_root_reached"] = ledger.get("ledger_root_reached", False)
    strict = [task_rows[task] for task in STRICT_TASK_IDS]
    native_ref = write_json(output / "native-control-audit.json", {"controls": native_rows})
    ledger_path = root / "research/experiments/python-task-ledger/ledger.csv"
    sources.append(file_binding(ledger_path))
    generation_scope = {"schema": "repaired_data_sft_v2.generation_control_scope/v1",
        "task_ids": list(STRICT_TASK_IDS), "package_digests": [task.get("identity", {}).get("package_digest") for task in strict],
        "transforms": sorted({edge["transform"] for task in strict for edge in task.get("lineage", [])}),
        "handoff": sources[0], "request": sources[1], "native_proof_archive": sources[2],
        "source_file_audit": inventory_ref, "native_control_audit": native_ref,
        "admission_basis": "control_qualified", "ledger_root_reached": False, "record_status": "candidate",
        "all19_controls_qualified": all(task["controls_qualified"] for task in strict),
        "fresh_proof_kind_required": "fresh_native_repaired", "fresh_K": 4, "fresh_rollouts_requested": 76,
        "fresh_rollouts_executed": 0, "dispatch_allowed": False, "scoring_allowed": False,
        "scope": "Exact19 generation-only input; not canonical-ledger admission or dataset validation."}
    scope_ref = write_json(output / "generation-control-scope.json", generation_scope)
    report = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "control_qualified_candidates_not_canonical_ledger_admitted" if generation_scope["all19_controls_qualified"] else "audit_has_blockers",
        "sources": sources, "source_controls_handoff": sources[0], "source_controls_request": sources[1],
        "current_reader_runtime": {"python": sys.version, "platform": platform.platform(),
            "evallab_module": str(Path(sys.modules["evallab"].__file__).resolve()), "first_party_sources": current_sources,
            "functions": ["census.native_observations", "census.positive_case_execution", "freeze._successor_repeat_proof",
                "VariantRecord.model_validate", "task_variants.lineage_chain", "task_variants.materialize", "task_variants.verify",
                "task_variants.derive_task (solution-only byte reconstruction)", "registry.compute_task_digests",
                "registry.harbor_task_digest", "counts.task_index_for.status_for", "setup_fingerprint.lineage_ledger_binding"]},
        "scope": scope, "tasks": strict, "excluded_records": [task_rows["002486"]],
        "artifacts": {"source_file_audit": inventory_ref, "source_record_audit": record_ref,
            "native_control_audit": native_ref, "generation_control_scope": scope_ref},
        "counts": {"requested_source_tasks": 20, "strict_tasks_audited": 19,
            "strict_control_qualified": sum(task["controls_qualified"] for task in strict),
            "canonical_ledger_admitted": 0, "candidate_records_staged": sum("canonical_record" in task for task in strict),
            "native_physical_trials_read": len(native_rows),
            "retained_verifier_observations_read": sum(len(row["observation"]["rewards"]) for row in native_rows if "observation" in row),
            "strict_native_physical_trials_read": sum(len(task["native_controls"]) for task in strict),
            "native_controls_current_reader_qualified": sum(row["qualified"] for row in native_rows),
            "source_unique_files_hashed": len(inventory), "source_file_hash_failures": len(inventory_failures),
            "publisher_artifacts_observed_not_native_source": sum(
                len(case.get("file_custody", {}).get("publication_artifacts_not_native_proof", [])) for case in native_rows),
            "archived_typed_records_audited": len(record_audit), "archived_record_failures": len(record_errors),
            "new_native_executions": 0, "fresh_rollouts": 0, "model_calls": 0, "provider_calls": 0, "new_cost_usd": 0},
        "failed_records": [{"task_id": task["task_id"], "failures": task["failures"],
            "failed_controls": [{"case": case["case"], "failures": case["failures"]} for case in task["native_controls"] if not case["qualified"]]}
            for task in tasks if task["failures"] or any(not case["qualified"] for case in task["native_controls"])],
        "limits": LIMITS, "dispatch_allowed": False, "scoring_allowed": False,
        "training_admission": False, "dataset_validated": False}
    report_ref = write_json(output / "control-admission.json", report)
    return {"report": report_ref, "counts": report["counts"], "status": report["status"], "generation_scope": scope_ref}


def write_generation_profile(root: Path, report_ref: dict[str, Any], target: Path) -> dict[str, Any]:
    """Rebind the owned generation profile to an actual completed audit receipt."""
    name = "xiaomi-mimo-phase2-train-20261018"
    if target.name != name + ".yaml":
        raise ValueError("profile output must name the exact generation-only profile")
    report_path = Path(report_ref["path"])
    require_binding(report_path, report_ref["sha256"])
    report = json.loads(report_path.read_bytes())
    tasks = report["tasks"]
    if (report["schema"] != SCHEMA or [task["task_id"] for task in tasks] != list(STRICT_TASK_IDS)
            or not all(task["controls_qualified"] and task["strict_assertion_scope_admission"] for task in tasks)):
        raise ValueError("profile requires a complete exact19 current control audit")
    profile = load_reference_profile(root, "xiaomi-mimo-rl")
    profile["name"] = name
    profile["description"] = (
        "Exact19 repaired train candidates for fresh native MiMo-9B generation only. "
        "Native fields retain xiaomi-mimo-rl's actual source citations. Retained controls are "
        "not new executions, locked-nop validation, general semantic certification or ledger admission. "
        "No held-out scoring, replay or dataset validation is authorized."
    )
    profile["campaign_lineage"] = {"headline": {
        "transforms": sorted({edge["transform"] for task in tasks for edge in task["lineage"]}),
        "package_digests": [task["identity"]["package_digest"] for task in tasks],
    }}
    profile["campaign_native_controls"] = {"audit": {"path": str(report_path.resolve()), "sha256": report_ref["sha256"]},
                                           "purpose": "fresh_native_repaired"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return file_binding(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--native-proof-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New, owned immutable audit directory")
    parser.add_argument("--profile-output", type=Path,
                        help="Explicitly generate/rebind the exact19 generation-only profile after a complete audit")
    args = parser.parse_args()
    result = audit(root=args.repo_root.resolve(), handoff_path=args.handoff.resolve(), request_path=args.request.resolve(),
                   archive_path=args.native_proof_archive.resolve(), output=args.output.resolve())
    if args.profile_output is not None:
        result["profile"] = write_generation_profile(args.repo_root.resolve(), result["report"], args.profile_output.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["counts"]["strict_control_qualified"] == len(STRICT_TASK_IDS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
