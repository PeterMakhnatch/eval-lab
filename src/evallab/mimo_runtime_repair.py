"""Source-bound collection repair for the frozen BurnMan activities task only.

No application implementation is supplied. The missing helper belongs to the
requested reference change; application imports and model construction therefore
belong inside the existing tests, where their failures are real test failures.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

TASK_ID = "format-code-task-001246"
TASK_NAME = f"mimo-v2.6-rl/{TASK_ID}"
TRANSFORM = "mimo-001246-collection@1"
PARENT_PACKAGE = "sha256:c4d94db236a5f7a920321f495e2ddecb1398d7ebdbfff521b1ce1a39e2bcf670"
PARENT_HARBOR = "sha256:a3a88f44a5785afb0f94e6fa6a529f755bd2c6903572df83d0f3a4344dd77e35"
PARENT_VERIFIER = "sha256:8944c7cb6552fd712f93649a676c4fa2b356f2eb0063601539cf31534e4faffe"
PARENT_PATCH_SHA256 = "eb98bd6d35113101c9f2aa887b71eae2d687ace8422b0981f8b0c998ecac4a6c"
TEST_PATH = "usercase-test-coderl/test_solution_activities.py"
PARAMETER_TEST = "test_nonideal_activity_coefficients_reject_zero_temperature_boundary"
FACTORIES = ("_regular_opx_model", "_subregular_model", "_polynomial_model", "_function_model")
TRANSFORM_INPUTS = {
    "schema": "mimo.001246.collection-transform/v1",
    "parent_test_patch_sha256": f"sha256:{PARENT_PATCH_SHA256}",
    "cause": "reference-preimage-missing-activities-helper-eager-package-import",
    "collection_boundary": "application-imports-and-four-model-factories-to-existing-test-bodies",
}
RATIONALE = (
    "The retained nop solution.py and solutionmodel.py Git blobs equal the recovered "
    "reference patch preimages b370f439 and e08ec127. That patch implements the "
    "missing _ideal_activities_fct and requested activity APIs. Eager package imports "
    "and four parametrized constructors currently fail during collection. Defer only "
    "those operations into every original test body; preserve all assertions, values, "
    "model factories and parameter combinations. Do not install any reference code."
)


def test_module(patch: bytes) -> tuple[str, bytes]:
    """Read the one fixed added test file, retaining the command diff verbatim."""
    marker = b"diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
    first, command = patch.split(marker, 1)
    lines = first.decode("utf-8").splitlines(keepends=True)
    if (lines[:4] != [f"diff --git a/{TEST_PATH} b/{TEST_PATH}\n", "new file mode 100644\n",
                      "--- /dev/null\n", f"+++ b/{TEST_PATH}\n"]
            or not all(line.startswith("+") for line in lines[5:])):
        raise ValueError("unexpected bounded001246 test patch structure")
    return "".join(line[1:] for line in lines[5:]), marker + command


def collection_patch(original: bytes) -> bytes:
    """Move imports/constructors, retaining the complete original test semantics."""
    from evallab.verifier_repair import RepairTest, build_patch_appendix

    if hashlib.sha256(original).hexdigest() != PARENT_PATCH_SHA256:
        raise ValueError("collection repair requires the exact frozen001246 test patch")
    source, command = test_module(original)
    tree = ast.parse(source)
    imports = [node for node in tree.body if (
        isinstance(node, ast.Import) and any(alias.name == "burnman" for alias in node.names)
    ) or (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("burnman"))]
    tests = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
    if len(imports) != 13 or len(tests) != 11:
        raise ValueError("bounded001246 application import/test boundary changed")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def span(node: ast.expr | ast.stmt) -> tuple[int, int]:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return offsets[node.lineno - 1] + node.col_offset, offsets[node.end_lineno - 1] + node.end_col_offset

    names = [alias.asname or alias.name for node in imports for alias in node.names]
    loader = "def _load_burnman():\n    global " + ", ".join(names) + "\n"
    for node in imports:
        segment = ast.get_source_segment(source, node)
        assert segment is not None
        loader += "    " + segment + "\n"
    edits: list[tuple[int, int, str]] = []
    start, _ = span(imports[0])
    _, end = span(imports[-1])
    edits.append((start, end, loader.rstrip("\n")))
    for test in tests:
        insertion = offsets[test.body[0].lineno - 1]
        setup = "    _load_burnman()\n"
        if test.name == PARAMETER_TEST:
            decorator = test.decorator_list[0]
            assert isinstance(decorator, ast.Call)
            parameters = decorator.args[1]
            assert isinstance(parameters, ast.List)
            factory_names = []
            for pair in parameters.elts:
                assert isinstance(pair, ast.Tuple)
                call = pair.elts[0]
                assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                factory_names.append(call.func.id)
                begin, finish = span(call)
                edits.append((begin, finish, call.func.id))
            if factory_names != list(FACTORIES):
                raise ValueError("bounded001246 parameter model inventory changed")
            setup += "    model = model()\n"
        edits.append((insertion, insertion, setup))
    changed = source
    for begin, finish, replacement in sorted(edits, reverse=True):
        changed = changed[:begin] + replacement + changed[finish:]

    # Independently undo only the permitted AST moves. Every original statement,
    # assertion, value, factory order and parametrized case must remain identical.
    restored = ast.parse(changed)
    loader_node = next(node for node in restored.body if isinstance(node, ast.FunctionDef) and node.name == "_load_burnman")
    position = restored.body.index(loader_node)
    restored.body[position:position + 1] = loader_node.body[1:]
    for test in restored.body:
        if not isinstance(test, ast.FunctionDef) or not test.name.startswith("test_"):
            continue
        test.body.pop(0)
        if test.name == PARAMETER_TEST:
            test.body.pop(0)
            decorator = test.decorator_list[0]
            assert isinstance(decorator, ast.Call)
            parameters = decorator.args[1]
            assert isinstance(parameters, ast.List)
            for pair in parameters.elts:
                assert isinstance(pair, ast.Tuple)
                pair.elts[0] = ast.Call(func=pair.elts[0], args=[], keywords=[])
    if ast.dump(restored) != ast.dump(tree):
        raise ValueError("collection repair changed original test semantics")
    appendix = build_patch_appendix([RepairTest(id="bounded001246", repo_path=TEST_PATH, code=changed)])
    return appendix.encode("utf-8") + command


def file_binding(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def package_identity(path: Path) -> dict[str, str]:
    from evallab.registry import compute_task_digests
    from evallab.task_variants import harbor_task_digest

    digests = compute_task_digests(path)
    if digests.verifier is None:
        raise ValueError("runtime repair requires a concrete verifier")
    return {"package_path": str(path.resolve()), "package_digest": digests.package,
            "harbor_digest": harbor_task_digest(path), "verifier_digest": digests.verifier}


def inventory_rows(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if (payload.get("schema") not in {"envcheck.v7.input-inventory/v3", "envcheck.v7.input-inventory/v4"}
            or payload.get("training_allowed") is not False):
        raise ValueError("runtime repair requires the effective v7/v3 or v7/v4 adjudicated inventory")
    rows = []
    for key, role in (("rows", "known-wrong"), ("adjudicated_correct_controls", "prompt-valid-alternate")):
        for row in payload[key]:
            if row["task_id"].removeprefix("mimo-v2.6-rl/") == TASK_ID:
                rows.append((role, row))
    if any(row["task_id"].removeprefix("mimo-v2.6-rl/") == TASK_ID for row in payload["unspecified_controls"]):
        raise ValueError("bounded001246 has unresolved submission adjudications")
    if not any(role == "prompt-valid-alternate" for role, _ in rows):
        raise ValueError("bounded001246 adjudicated-correct control is missing")
    if len({row["finding_id"] for _, row in rows}) != len(rows):
        raise ValueError("runtime repair inventory has duplicated findings")
    return rows


def row_sha256(row: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def control_script(row: dict[str, Any], patch: bytes) -> bytes:
    from evallab.mimo_clean import build_solution_sh

    return build_solution_sh(task_id=TASK_ID, label="runtime-control", fix_commit=row["finding_id"], patch_bytes=patch)


def derive_runtime_repair(*, parent: Path, inventory: Path, evidence: dict[str, Any],
                          repo_root: Path, output_dir: Path, variants_root: Path,
                          handoff_path: Path, created_by: str) -> dict[str, Any]:
    """Derive and publish a nonqualifying plan. This API never launches controls."""
    from evallab.schemas import ExperimentMatrix
    from evallab.task_variants import (
        VariantExistsError,
        VariantRecord,
        derive_task,
        lineage_chain,
        materialize,
        resolve_record,
    )
    from evallab.training_freeze import RuntimeRepairHandoff

    repo_root, output_dir, variants_root = (path.resolve() for path in (repo_root, output_dir, variants_root))
    if not output_dir.is_relative_to(repo_root / "derived") or not variants_root.is_relative_to(repo_root / "derived"):
        raise ValueError("runtime repair artifacts must be isolated under this repository's derived directory")
    if output_dir.exists() or handoff_path.exists():
        raise ValueError("runtime repair never overwrites existing artifacts or handoffs")
    identity = package_identity(parent)
    if [identity[key] for key in ("package_digest", "harbor_digest", "verifier_digest")] != [PARENT_PACKAGE, PARENT_HARBOR, PARENT_VERIFIER]:
        raise ValueError("runtime repair parent differs from the frozen001246 package")
    parent_record = Path("library/task-variants") / TASK_NAME.replace("/", "__") / (PARENT_PACKAGE[7:19] + ".json")
    chain = lineage_chain(repo_root / parent_record, repo_root=repo_root)
    if any(not step.parent_found for step in chain):
        raise ValueError("runtime repair requires the complete frozen parent lineage")
    admitted = json.loads(inventory.read_bytes())
    rows = inventory_rows(admitted)
    # Validate all submitted bytes before deriving an immutable record.
    for _, row in rows:
        if (row["v6_package_digest"] != PARENT_PACKAGE
                or ("v6_package" in row and Path(row["v6_package"]).resolve() != parent.resolve())
                or file_binding(Path(row["reproducer_patch"]))["sha256"] != row["reproducer_sha256"]):
            raise ValueError("runtime repair inventory patch/parent binding mismatch")
    parent_patch = (parent / "tests/test.patch").read_bytes()
    repaired_patch = collection_patch(parent_patch)
    parent_source = {"kind": "variant", "record": str(parent_record)}
    try:
        record = derive_task(
            parent, changes={"tests/test.patch": repaired_patch},
            transform=TRANSFORM, rationale=RATIONALE, created_by=created_by, inputs=TRANSFORM_INPUTS,
            parent_source=parent_source, repo_root=repo_root, variants_root=variants_root,
        )
    except VariantExistsError:
        expected_change = {
            "path": "tests/test.patch",
            "before_sha256": "sha256:" + hashlib.sha256(parent_patch).hexdigest(),
            "after_sha256": "sha256:" + hashlib.sha256(repaired_patch).hexdigest(),
            "content": repaired_patch.decode(),
        }
        matches: list[VariantRecord] = []
        for path in sorted((repo_root / parent_record.parent).glob("*.json")):
            candidate = resolve_record(path, repo_root=repo_root)
            if (candidate.task_name == TASK_NAME and candidate.transform == TRANSFORM
                    and candidate.parent.digest == PARENT_PACKAGE
                    and candidate.parent.harbor_digest == PARENT_HARBOR
                    and candidate.parent.source.model_dump(exclude_none=True) == parent_source
                    and candidate.inputs == TRANSFORM_INPUTS
                    and candidate.components_changed == ["verifier"]
                    and len(candidate.files) == 1
                    and candidate.files[0].model_dump() == expected_change):
                matches.append(candidate)
        if len(matches) != 1:
            raise
        record = matches[0]
        materialize(record, parent, repo_root=repo_root, variants_root=variants_root)
    selected = package_identity(variants_root / record.task_slug / record.digest12)
    output_dir.mkdir(parents=True)
    controls = []
    runs = [{"name": "reference", "agent": "oracle", "attempts": 1, "expect_reward": 1.0},
            {"name": "nop", "agent": "nop", "attempts": 1, "expect_reward": 0.0}]
    for index, (role, row) in enumerate(rows):
        run = f"{'wrong' if role == 'known-wrong' else 'correct'}-{index:03d}"
        patch = Path(row["reproducer_patch"])
        script = output_dir / f"{run}.sh"
        script.write_bytes(control_script(row, patch.read_bytes()))
        controls.append({"run": run, "role": role, "finding_id": row["finding_id"],
                         "inventory_row_sha256": row_sha256(row), "patch": file_binding(patch), "script": file_binding(script)})
        runs.append({"name": run, "agent": "oracle", "attempts": 1,
                     "expect_reward": 0.0 if role == "known-wrong" else 1.0,
                     "solution": str(script.relative_to(repo_root))})
    matrix = ExperimentMatrix.model_validate({
        "schema_version": 2, "matrix_id": "01K7Y8BQZE0000000000001246", "name": "mimo-v7-001246-collection-controls",
        "hypothesis": "Source-bound real-case collection repair preserves reference and adjudicated controls.",
        "benchmark_family": "mimo-clean-v7", "task_id": TASK_ID,
        "task": str(Path(selected["package_path"]).relative_to(repo_root)),
        "task_package_digest": selected["package_digest"], "verifier_digest": selected["verifier_digest"],
        "environment": "docker", "jobs_dir": "runs",
        "concurrency": 1, "timeout_seconds": 1800, "runs": runs,
    })
    matrix_path = output_dir / "matrix.json"
    matrix_path.write_text(matrix.model_dump_json(indent=2) + "\n")
    source_paths = {Path(item["path"]).resolve() for item in admitted["source_inputs"]}
    source_paths.update(Path(row["adjudication_source"]).resolve() for _, row in rows if row.get("adjudication_source"))
    handoff = RuntimeRepairHandoff.model_validate({
        "schema": "mimo-v7-runtime-repair/v1", "task_id": TASK_ID, "transform": TRANSFORM,
        "status": "development_candidate", "qualified_for_v7": False, "scoring_allowed": False, "training_allowed": False,
        "parent": identity, "selected": selected, "native_record": file_binding(repo_root / record.record_relpath()),
        "inventory": file_binding(inventory), "inventory_sources": [file_binding(path) for path in sorted(source_paths)],
        "controls": controls, "matrix": file_binding(matrix_path), "causal_evidence": evidence,
        "qualification": {"status": "pending", "required": "selected-digest-native-controls-and-independent-verifier"},
    }).model_dump(mode="json", by_alias=True)
    with handoff_path.open("x") as handle:
        json.dump(handoff, handle, sort_keys=True, indent=2)
        handle.write("\n")
    return handoff


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parent", "inventory", "causal-evidence", "repo-root", "output-dir", "variants-root", "handoff"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--created-by", required=True)
    args = parser.parse_args()
    handoff = derive_runtime_repair(parent=args.parent, inventory=args.inventory,
        evidence=json.loads(args.causal_evidence.read_bytes()), repo_root=args.repo_root,
        output_dir=args.output_dir, variants_root=args.variants_root, handoff_path=args.handoff,
        created_by=args.created_by)
    print(json.dumps({"selected": handoff["selected"], "handoff": str(args.handoff), "qualified_for_v7": False}, sort_keys=True))


if __name__ == "__main__":
    main()
