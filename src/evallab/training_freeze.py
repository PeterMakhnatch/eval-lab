"""Closed, development-only intake for the bounded001246 runtime repair.

Native facts are deliberately consumed by the manifest's existing custody reader,
not by this schema or by a producer's claimed outcomes. Nothing here opens scoring.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class FileBinding(ClosedModel):
    path: str
    sha256: Digest
    bytes: Annotated[int, Field(ge=0)]

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("runtime repair file bindings must be absolute")
        return value


class PackageIdentity(ClosedModel):
    package_path: str
    package_digest: Digest
    harbor_digest: Digest
    verifier_digest: Digest

    @field_validator("package_path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        return FileBinding.absolute_path(value)


class SourcePreimage(ClosedModel):
    path: Literal["burnman/classes/solution.py", "burnman/classes/solutionmodel.py"]
    sha256: Digest
    git_blob: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    reference_preimage: Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$")]


class CausalEvidence(ClosedModel):
    native_execution_id: Literal["bf0d13f9-ff9a-446f-87a1-7cd1231edd6e"]
    snapshot: FileBinding
    reference_patch: FileBinding
    test_output: FileBinding
    setup_log: FileBinding
    source_preimages: list[SourcePreimage]


class SubmissionControl(ClosedModel):
    run: Annotated[str, Field(pattern=r"^(wrong|correct)-[0-9]{3}$")]
    role: Literal["known-wrong", "prompt-valid-alternate"]
    finding_id: str
    inventory_row_sha256: Digest
    patch: FileBinding
    script: FileBinding


class PendingQualification(ClosedModel):
    status: Literal["pending"]
    required: Literal["selected-digest-native-controls-and-independent-verifier"]


class RecordedQualification(ClosedModel):
    status: Literal["recorded"]
    receipt: FileBinding


class RuntimeRepairHandoff(ClosedModel):
    schema_name: Literal["mimo-v7-runtime-repair/v1"] = Field(alias="schema")
    task_id: Literal["format-code-task-001246"]
    transform: Literal["mimo-001246-collection@1"]
    status: Literal["development_candidate"]
    qualified_for_v7: Literal[False]
    scoring_allowed: Literal[False]
    training_allowed: Literal[False]
    parent: PackageIdentity
    selected: PackageIdentity
    native_record: FileBinding
    inventory: FileBinding
    inventory_sources: list[FileBinding]
    controls: list[SubmissionControl]
    matrix: FileBinding
    causal_evidence: CausalEvidence
    qualification: Annotated[PendingQualification | RecordedQualification, Field(discriminator="status")]

    @field_validator("qualified_for_v7", "scoring_allowed", "training_allowed", mode="before")
    @classmethod
    def exactly_false(cls, value: Any) -> bool:
        if value is not False:
            raise ValueError("runtime repair intake cannot qualify, train or open scoring")
        return value


class NativeControlCheck(ClosedModel):
    run: str
    role: Literal["reference", "nop", "known-wrong", "prompt-valid-alternate"]
    status: Literal["ok"]
    expected_reward: Literal[0, 1]
    matrix_sha256: Digest
    job_dir: str
    repeats: list[float] | None
    solution_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    staged_task_digest: Digest | None = None

    @field_validator("job_dir")
    @classmethod
    def absolute_job_path(cls, value: str) -> str:
        return FileBinding.absolute_path(value)


class RuntimeQualificationReceipt(ClosedModel):
    schema_name: Literal["mimo-v7-runtime-controls/v1"] = Field(alias="schema")
    task_id: Literal["format-code-task-001246"]
    package_digest: Digest
    harbor_digest: Digest
    verifier_digest: Digest
    matrix_sha256: Digest
    inventory_sha256: Digest
    checks: list[NativeControlCheck]


def read_binding(binding: FileBinding, sources: dict[str, dict[str, Any]]) -> bytes:
    path = Path(binding.path).resolve()
    data = path.read_bytes()
    actual = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if actual["sha256"] != binding.sha256[7:] or actual["bytes"] != binding.bytes:
        raise ValueError(f"runtime repair source bytes changed: {path}")
    previous = sources.get(str(path))
    if previous is not None and previous != actual:
        raise ValueError(f"runtime repair source changed while consuming: {path}")
    sources[str(path)] = actual
    return data


def verify_causal_evidence(evidence: CausalEvidence, sources: dict[str, dict[str, Any]]) -> None:
    import ast

    for binding in (evidence.snapshot, evidence.reference_patch, evidence.test_output, evidence.setup_log):
        read_binding(binding, sources)
    patch = Path(evidence.reference_patch.path).read_text()
    output = Path(evidence.test_output.path).read_text()
    setup = Path(evidence.setup_log.path).read_text()
    if ("_ideal_activities_fct" not in output or "ImportError" not in output
            or not re.search(r"collected 0 items|collecting.*collected 0 items", output)
            or "_ideal_activities_fct" not in setup or "matched 0" not in setup):
        raise ValueError("runtime repair cause lacks the retained zero-collection/pre-import evidence")
    if {item.path for item in evidence.source_preimages} != {
        "burnman/classes/solution.py", "burnman/classes/solutionmodel.py",
    } or len(evidence.source_preimages) != 2:
        raise ValueError("runtime repair must bind both complete reference preimages")
    with tarfile.open(evidence.snapshot.path) as archive:
        for item in evidence.source_preimages:
            stream = archive.extractfile(item.path)
            if stream is None:
                raise ValueError("runtime repair snapshot lacks a reference preimage")
            data = stream.read()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            header = re.search(r"diff --git a/" + re.escape(item.path) + r" b/[^\n]+\nindex ([0-9a-f]+)\.\.", patch)
            if (hashlib.sha256(data).hexdigest() != item.sha256[7:] or blob != item.git_blob
                    or not blob.startswith(item.reference_preimage) or header is None
                    or header[1] != item.reference_preimage):
                raise ValueError("runtime repair snapshot differs from the actual reference preimage")
            tree = ast.parse(data)
            if item.path.endswith("solutionmodel.py") and any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_ideal_activities_fct"
                for node in ast.walk(tree)
            ):
                raise ValueError("runtime repair evidence already contains the missing implementation")
        elastic = archive.extractfile("burnman/classes/elasticsolutionmodel.py")
        classes = archive.extractfile("burnman/classes/__init__.py")
        if (elastic is None or classes is None or b"import _ideal_activities_fct" not in elastic.read()
                or b"import elasticsolutionmodel" not in classes.read()
                or "+def _ideal_activities_fct(" not in patch):
            raise ValueError("runtime repair cause is not the reference-change eager import boundary")


def validate_runtime_repair(handoff: RuntimeRepairHandoff, parent: dict[str, Any], *,
                            repo_root: Path, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Replay only the allowed delta and bind the complete submission inventory."""
    from evallab import mimo_runtime_repair as producer
    from evallab.task_variants import VariantRecord, materialize

    expected_parent = {key: parent[key] for key in PackageIdentity.model_fields}
    if (handoff.parent.model_dump() != expected_parent
            or [expected_parent[key] for key in ("package_digest", "harbor_digest", "verifier_digest")]
            != [producer.PARENT_PACKAGE, producer.PARENT_HARBOR, producer.PARENT_VERIFIER]):
        raise ValueError("runtime repair does not extend the exact frozen001246 parent")
    record = VariantRecord.model_validate(json.loads(read_binding(handoff.native_record, sources)))
    before, after = Path(handoff.parent.package_path), Path(handoff.selected.package_path)
    if (record.task_name != producer.TASK_NAME or record.transform != producer.TRANSFORM
            or record.parent.digest != handoff.parent.package_digest
            or record.parent.harbor_digest != handoff.parent.harbor_digest
            or record.parent.source.kind != "variant"
            or record.parent.source.record != str(Path("library/task-variants") / producer.TASK_NAME.replace("/", "__") / (producer.PARENT_PACKAGE[7:19] + ".json"))
            or record.variant_digest != handoff.selected.package_digest
            or record.variant_harbor_digest != handoff.selected.harbor_digest
            or set(record.components_changed) != {"verifier"}
            or [item.path for item in record.files] != ["tests/test.patch"]
            or record.inputs != producer.TRANSFORM_INPUTS
            or (after / "tests/test.patch").read_bytes() != producer.collection_patch((before / "tests/test.patch").read_bytes())
            or producer.package_identity(after) != handoff.selected.model_dump()):
        raise ValueError("runtime repair is not the deterministic allowed collection-only variant")
    with tempfile.TemporaryDirectory(prefix="mimo-001246-replay-") as scratch:
        replay = materialize(record, before, repo_root=repo_root, variants_root=Path(scratch))
        replay_identity = producer.package_identity(replay)
        if any(replay_identity[key] != getattr(handoff.selected, key) for key in (
            "package_digest", "harbor_digest", "verifier_digest",
        )):
            raise ValueError("runtime repair independent record replay differs from selected bytes")
    verify_causal_evidence(handoff.causal_evidence, sources)
    inventory = json.loads(read_binding(handoff.inventory, sources))
    rows = producer.inventory_rows(inventory)
    source_bindings = {str(Path(item.path).resolve()): item for item in handoff.inventory_sources}
    expected_sources = {str(Path(item["path"]).resolve()): item["sha256"] for item in inventory["source_inputs"]}
    for _, row in rows:
        if row.get("adjudication_source"):
            expected_sources[str(Path(row["adjudication_source"]).resolve())] = row["adjudication_source_sha256"]
    if len(source_bindings) != len(handoff.inventory_sources) or set(source_bindings) != set(expected_sources):
        raise ValueError("runtime repair omits or duplicates authoritative inventory sources")
    authoritative_payloads = []
    for path, expected in expected_sources.items():
        if source_bindings[path].sha256 != expected:
            raise ValueError("runtime repair inventory authority hash mismatch")
        authoritative_payloads.append(json.loads(read_binding(source_bindings[path], sources)))

    def objects(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child)

    authoritative_rows = [item for payload in authoritative_payloads for item in objects(payload)]
    controls = {item.finding_id: item for item in handoff.controls}
    if len(controls) != len(handoff.controls) or set(controls) != {row["finding_id"] for _, row in rows}:
        raise ValueError("runtime repair omits or duplicates an admitted wrong/correct control")
    for role, row in rows:
        control = controls[row["finding_id"]]
        matching_sources = [
            item for item in authoritative_rows
            if item.get("task_id") == row["task_id"]
            and item.get("reproducer_sha256", "").removeprefix("sha256:") == row["reproducer_sha256"].removeprefix("sha256:")
            and (item.get("finding_id") == row["finding_id"] or item.get("probe_id") == row["probe_id"])
        ]
        corrections = [
            item["authoritative_verdict"] for item in authoritative_rows
            if item.get("task_id") == row["task_id"] and item.get("reproducer_id") == row["probe_id"]
            and "authoritative_verdict" in item
        ]
        verdicts = corrections or [(item.get("adjudication") or {}).get("verdict") for item in matching_sources]
        allowed = {"wrong"} if role == "known-wrong" else {"equivalent", "correct", "prompt-valid"}
        if not matching_sources or not verdicts or any(verdict not in allowed for verdict in verdicts):
            raise ValueError("runtime repair control contradicts its actual authoritative adjudication")
        patch = read_binding(control.patch, sources)
        script = read_binding(control.script, sources)
        if (control.role != role or control.inventory_row_sha256 != producer.row_sha256(row)
                or row["v6_package_digest"] != handoff.parent.package_digest
                or control.patch.sha256 != row["reproducer_sha256"]
                or Path(control.patch.path).resolve() != Path(row["reproducer_patch"]).resolve()
                or script != producer.control_script(row, patch)):
            raise ValueError("runtime repair changed a submitted payload, adjudication or control script")
    if len({item.run for item in handoff.controls}) != len(handoff.controls):
        raise ValueError("runtime repair has duplicate control runs")
    read_binding(handoff.matrix, sources)
    qualification = None
    if isinstance(handoff.qualification, RecordedQualification):
        qualification = RuntimeQualificationReceipt.model_validate(json.loads(read_binding(handoff.qualification.receipt, sources)))
        if (qualification.task_id != handoff.task_id or qualification.package_digest != handoff.selected.package_digest
                or qualification.harbor_digest != handoff.selected.harbor_digest
                or qualification.verifier_digest != handoff.selected.verifier_digest
                or qualification.matrix_sha256 != handoff.matrix.sha256
                or qualification.inventory_sha256 != handoff.inventory.sha256):
            raise ValueError("runtime repair native receipt binds a different package, matrix or inventory")
    return {"record": record, "qualification": qualification}
