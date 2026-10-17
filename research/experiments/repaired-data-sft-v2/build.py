"""Offline repaired-data SFT v2; no execution, inference, downloads or catalog writes.

Run in the locked root QA environment with the isolated SFT client's pinned overlay:
  HF_HUB_OFFLINE=1 uv run --locked --with transformers==5.12.1 --with modal==1.5.5 \
    --with huggingface-hub==1.9.2 --with jinja2==3.1.6 python \
    research/experiments/repaired-data-sft-v2/build.py \
    --inputs inputs.json --tokenizer-dir <local pinned snapshot> --epochs 1 --out <new dir>

inputs.json (repaired-data-sft-v2.inputs/1) binds every input as
{"path": <absolute or inputs-relative path>, "sha256": <full sha256, optional prefix>}:
  inventory: repaired-data-sft-v2.inventory/1, rows with result/trajectory refs and
             optional ancestry refs (native VariantRecords, not asserted parent labels).
  heldout: the frozen mimo.heldout/v1 document containing ALL 33 parents.
  v6_manifest: the existing mimo-clean-v6 CSV.
  repairs: [existing evallab.task_variant/v1 refs]; only verifier-repair@1 children
           of the exact v6 digest qualify, never a similarly named raw repair.
  controls: [existing qa-trace-counterfactual/3 controls.json refs].
  envcheck_manifest: optional existing repaired-v6-variants-manifest.json ref;
                     reads its actual retained Harbor repeated-control jobs.
  regrades: [existing QA counterfactual summary JSON or JSONL refs].
  audits: [existing QA full-prompt audit refs], optional.
  exclusions: [canonical training-excluded task IDs], optional additional exclusions.
  corrections: optional list of custody links, NOT a second EnvCheck export:
    {source: [native job UUID, trial UUID], corrected: [job UUID, trial UUID],
     request: <hash-bound EnvCheck envcheck.correction-request/v1 request.json>,
     failed_regrade: {document: <ref>, row: <zero-based index>},
     passed_regrade: {document: <ref>, row: <zero-based index>}}
D consumes EnvCheck's reset-context-feedback protocol, NOT restored source history.
Its native initial user input must be the original task prompt plus the actual
incomplete pre-hidden-test agent.diff and source-free-outcome-projection/v1 feedback.
The raw failing stdout is retained and hash-bound, NEVER forwarded to the model.
Genuine same-model native completion and accepted native replay 0 -> 1 on the SAME
repaired digest are required; producer labels are not independent semantic truth.
Both executions retain native ancestry/controls and separate verifier environments;
the corrected job's hash-bound task staging pins the exact repaired package.
No teacher, copied patch, restored history, hidden source, or empty input becomes D.

B requires >=51 distinct full-conversation, nonzero-loss rows surviving BOTH exact
v6 and its verifier-repair@1 child with genuine native controls. C is a seeded-
permutation feasible subset of the same original-pass eligibility pool, NOT a
uniform subset draw, and must differ from B. B/C use common positive INTEGER
full epochs, loss=all, no packing, no max_steps and no truncation; B/C supervised
assistant-token presentations are exactly equal. The manifest is the schedule
contract; a later partial-epoch/early-stopped run cannot claim this match.
B/C's common original-pass frame is the actual built globally hardened v6 manifest
task domain, minus all frozen heldout parents/descendants and explicit exclusions.
Grader controls and observations bind the full (task_name, variant_digest) physical
identity. Selection aliases retain native manifest/record ancestry, not independent
executions; only proven exact-v6 and test-only exact-v6-child routes qualify B.

--plan writes token accounting/qualification refusals before applying the B minimum
and does not export trainable arms. Normal build fails closed without >=51 B rows
or exact C; a missing D is explicit in the receipt (or fatal with --require-d).
D trains the last real main-agent assistant target of each native correction with
loss=last and its exact recorded prefix, reasoning and tools. Earlier correction
turns stay context; original false-pass history is NOT restored or relabeled as D.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
MINIMUM_B = 51
SCHEMA = "repaired-data-sft-v2.inputs/1"
FAULTS = {"native_faulty", "previously_reviewed_faulty", "static_fault_suspected", "unclear"}
V6_CHAIN = {
    "strip-future-history@1",
    "purge-build-caches@4",
    "mtime-normalize@2",
    "separate-verifier@10",
    "agent-network-none@1",
}
CORRECTION_PROTOCOL = "reset-context-feedback"
FEEDBACK_POLICY = "source-free-outcome-projection/v1"
FEEDBACK_EXCEPTIONS = frozenset({
    "AssertionError", "TypeError", "ValueError", "KeyError", "IndexError", "AttributeError",
    "RuntimeError", "NotImplementedError", "ImportError", "ModuleNotFoundError", "NameError",
    "ZeroDivisionError", "OverflowError", "StopIteration", "RecursionError", "SyntaxError",
    "IndentationError", "FileNotFoundError", "PermissionError", "TimeoutError", "OSError",
})
# Observed bytes of the immutable model revision used by sft.py and v1. A local
# directory with a familiar name is not proof of tokenizer/template identity.
TOKENIZER_SHA256 = {
    "tokenizer.json": "06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523",
    "tokenizer_config.json": "792fa3f0cb88b111e54ef3134c873531008c4df471d108da17903426e308aa7b",
    "chat_template.jinja": "59a64ebb4df6d1489d09a91267cf3ceb106162d4a893c4f84833cfb8c897ff63",
    "vocab.json": "ce99b4cb2983d118806ce0a8b777a35b093e2000a503ebde25853284c9dfa003",
    "merges.txt": "a9d356d7bdf1ef4949e3e748e95b8e10ad9d4e2e838eddc38a0a7b6b94d1db8d",
}
FROZEN_HELDOUT = frozenset(
    "mimo-v2.6-rl/format-code-task-" + number
    for number in (
        "001248",
        "000161",
        "000142",
        "000783",
        "001399",
        "001914",
        "002871",
        "002335",
        "001590",
        "001480",
        "001371",
        "001246",
        "001948",
        "001425",
        "002395",
        "002092",
        "000144",
        "002915",
        "000143",
        "000149",
        "002989",
        "001199",
        "000878",
        "001220",
        "000905",
        "002371",
        "001891",
        "002451",
        "002304",
        "000582",
        "000657",
        "000371",
        "001686",
    )
)


class BuildError(ValueError):
    """Missing or contradictory source evidence; never a clean observation."""


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_task(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildError("missing task ancestry identity")
    task = value if "/" in value else "mimo-v2.6-rl/" + value
    if not re.fullmatch(r"mimo-v2\.6-rl/[A-Za-z0-9][A-Za-z0-9_.-]*", task):
        raise BuildError(f"unsupported task identity: {value!r}")
    return task


def grader_identity(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise BuildError("missing full physical grader identity")
    task, expected = value.get("task_name"), value.get("variant_digest")
    if canonical_task(task) != task:
        raise BuildError("physical grader identity requires the full canonical task")
    if not isinstance(expected, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise BuildError("physical grader identity lacks its full exact package digest")
    return task, expected


class Evidence:
    """Resolve hash-bound local files and retain every byte dependency in the receipt."""

    def __init__(self, base: Path):
        self.base = base
        self.files: dict[str, dict[str, Any]] = {}

    def path(self, path: str | Path, *, relative_to: Path | None = None) -> Path:
        p = Path(path).expanduser()
        return (p if p.is_absolute() else (relative_to or self.base) / p).resolve()

    def capture(self, path: str | Path, *, relative_to: Path | None = None) -> dict[str, Any]:
        p = self.path(path, relative_to=relative_to)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise BuildError(f"missing native evidence {p}: {exc}") from exc
        ref = {"path": str(p), "sha256": digest(data), "bytes": len(data)}
        old = self.files.get(str(p))
        if old is not None and old != ref:
            raise BuildError(f"evidence changed during build: {p}")
        self.files[str(p)] = ref
        return ref

    def bound(self, ref: dict[str, Any]) -> Path:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise BuildError(
                "evidence must be a path/full-sha256 reference, not a label or boolean"
            )
        expected = str(ref.get("sha256", "")).removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise BuildError(f"missing full evidence hash: {ref.get('path')}")
        got = self.capture(ref["path"])
        if got["sha256"] != "sha256:" + expected:
            raise BuildError(f"evidence hash mismatch: {ref['path']}")
        return Path(got["path"])

    def json(self, ref: dict[str, Any]) -> Any:
        try:
            data = self.bound(ref).read_bytes()
            if digest(data) != "sha256:" + str(ref["sha256"]).removeprefix("sha256:"):
                raise BuildError(f"JSON evidence changed after hash binding: {ref['path']}")
            return json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BuildError(f"invalid JSON evidence: {ref.get('path')}") from exc

    def observed_json(self, path: str | Path) -> Any:
        ref = self.capture(path)
        return self.json(ref)


def rows_from_document(evidence: Evidence, ref: dict[str, Any]) -> list[dict[str, Any]]:
    path = evidence.bound(ref)
    try:
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        else:
            doc = json.loads(path.read_text())
            rows = doc if isinstance(doc, list) else doc.get("rows", [])
    except (ValueError, AttributeError) as exc:
        raise BuildError(f"invalid regrade document: {path}") from exc
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise BuildError(f"invalid regrade rows: {path}")
    return rows


def load_heldout(evidence: Evidence, ref: dict[str, Any]) -> set[str]:
    doc = evidence.json(ref)
    parents = [canonical_task(t) for t in doc.get("heldout", [])]
    if (
        doc.get("schema") != "mimo.heldout/v1"
        or len(parents) != 33
        or set(parents) != FROZEN_HELDOUT
    ):
        raise BuildError("training exclusion requires the frozen ALL33 distinct heldout parents")
    return set(parents)


def ancestry(evidence: Evidence, refs: list[dict[str, Any]]) -> set[str]:
    """Resolve actual VariantRecord parent links, including renamed descendants."""
    parents: set[str] = set()
    seen: set[str] = set()
    completed: set[str] = set()

    def visit(ref: dict[str, Any], expected: str | None = None) -> None:
        path = evidence.bound(ref)
        if str(path) in seen:
            raise BuildError(f"cyclic ancestry record: {path}")
        seen.add(str(path))
        rec = evidence.json(ref)
        if rec.get("schema") != "evallab.task_variant/v1":
            raise BuildError(f"not a native VariantRecord: {path}")
        if expected is not None and rec.get("variant_digest") != expected:
            raise BuildError(f"ancestry parent digest mismatch: {path}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(rec.get("variant_digest", ""))):
            raise BuildError("native ancestry lacks its full variant digest")
        if str(path) in completed:
            seen.remove(str(path))
            return
        parents.add(canonical_task(rec["task_name"]))
        inputs = rec.get("inputs") or {}
        if inputs.get("parent_task"):
            parents.add(canonical_task(inputs["parent_task"]))
        parent = rec.get("parent") or {}
        source = parent.get("source") or {}
        if source.get("kind") == "variant":
            target = source.get("record")
            if not target:
                raise BuildError(f"missing parent VariantRecord: {path}")
            candidate = evidence.path(target, relative_to=REPO)
            visit(evidence.capture(candidate), parent.get("digest"))
        elif source.get("path"):
            task_path = evidence.path(source["path"]) / "task.toml"
            evidence.capture(task_path)
            config = tomllib.loads(task_path.read_text())
            parent_id = (config.get("metadata") or {}).get("source_id")
            if parent_id:
                parents.add(canonical_task(parent_id))
        seen.remove(str(path))
        completed.add(str(path))

    for ref in refs:
        visit(ref)
    return parents


@dataclass
class Source:
    key: tuple[str, str]
    task: str
    result: dict[str, Any]
    trajectory: dict[str, Any]
    result_path: Path
    trajectory_path: Path
    parents: set[str]
    provenance: dict[str, Any]
    correction_messages: list[dict[str, Any]] | None = None
    correction_tools: list[Any] | None = None


def native_source(evidence: Evidence, row: dict[str, Any]) -> Source:
    result_path = evidence.bound(row["result"])
    trajectory_path = evidence.bound(row["trajectory"])
    result, trajectory = evidence.json(row["result"]), evidence.json(row["trajectory"])
    job = (result.get("config") or {}).get("job_id")
    trial = result.get("id")
    if not all(isinstance(v, str) and v and "/" not in v and "\\" not in v for v in (job, trial)):
        raise BuildError(f"missing safe native (job_id, trial_id): {result_path}")
    if any(
        row.get(k) is not None and row[k] != v
        for k, v in (("job_id", job), ("trial_id", trial), ("execution_id", trial))
    ):
        raise BuildError(f"inventory/native identity mismatch: {result_path}")
    task = canonical_task(result.get("task_name"))
    parents = {task} | ancestry(evidence, row.get("ancestry", []))
    task_path = ((result.get("config") or {}).get("task") or {}).get("path")
    if task_path and (Path(task_path) / "task.toml").is_file():
        toml_path = Path(task_path) / "task.toml"
        evidence.capture(toml_path)
        config = tomllib.loads(toml_path.read_text())
        original_id = (config.get("metadata") or {}).get("source_id")
        if original_id:
            parents.add(canonical_task(original_id))
    # Variant-like renamed code tasks cannot evade exclusion through their display name.
    if re.search(r"format-code-task-\d{6}.+", task) and len(parents) == 1:
        raise BuildError(f"renamed task lacks native parent ancestry: {task}")
    if row.get("metadata"):
        evidence.bound(row["metadata"])
    provenance = {
        k: row[k] for k in ("result", "trajectory", "metadata", "ancestry", "aliases") if k in row
    }
    code_parents = {p for p in parents if re.fullmatch(r"mimo-v2\.6-rl/format-code-task-\d{6}", p)}
    if len(code_parents) > 1:
        raise BuildError("native variant has ambiguous original task ancestry")
    if code_parents:
        provenance["native_task_name"] = task
        task = next(iter(code_parents))
    return Source(
        (job, trial), task, result, trajectory, result_path, trajectory_path, parents, provenance
    )


def deduplicate(sources: list[Source]) -> list[Source]:
    by_key: dict[tuple[str, str], Source] = {}
    for source in sources:
        old = by_key.get(source.key)
        if old is not None:
            if (
                old.task != source.task
                or old.trajectory != source.trajectory
                or old.result != source.result
                or old.parents != source.parents
            ):
                raise BuildError(f"conflicting native copies: {source.key}")
            continue
        by_key[source.key] = source
    return [by_key[k] for k in sorted(by_key)]


def original_pass_eligible(
    source: Source,
    heldout: set[str],
    excluded: set[str],
    correction_keys: set[tuple[str, str]],
    *,
    v6_tasks: set[str] | None = None,
) -> None:
    if source.parents & (heldout | excluded):
        raise BuildError("heldout/excluded task parent or descendant")
    if v6_tasks is not None and source.task not in v6_tasks:
        raise BuildError("source is outside the actual built globally hardened v6 task domain")
    if source.key in correction_keys:
        raise BuildError("self-corrected targets are D, not original-pass B/C pool")
    if reward(source.result) != 1:
        raise BuildError("not an observed original native pass")
    if source.result.get("exception_info") is not None:
        raise BuildError("original native pass has a non-null native exception")


def reward(result: dict[str, Any]) -> Any:
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    return rewards.get("reward", rewards.get("primary_reward"))


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise BuildError(f"cannot import existing conversion code: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def conversion_modules() -> tuple[Any, Any, Any]:
    sys.path.insert(0, str(REPO / "src"))
    v1 = _module("rds2_lossless", REPO / "research/experiments/repaired-data-sft-v1/to_export.py")
    qa = _module(
        "rds2_raw_messages", REPO / "research/experiments/qa-trace/training-arms/build_arms.py"
    )
    return v1, qa, v1.sft


class Qualification:
    """Read existing QA native replay/control contracts; do not execute them."""

    def __init__(self, evidence: Evidence, inputs: dict[str, Any]):
        from evallab.task_variants import VariantRecord

        self.evidence = evidence
        self.v6_ref = inputs["v6_manifest"]
        with evidence.bound(self.v6_ref).open(newline="") as stream:
            self.v6 = {canonical_task(r["task_id"]): r for r in csv.DictReader(stream)}
        self.repairs: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for ref in inputs.get("repairs", []):
            rec = evidence.json(ref)
            try:
                VariantRecord.model_validate(rec)
            except ValueError as exc:
                raise BuildError(f"invalid native repair VariantRecord: {exc}") from exc
            if (
                rec.get("schema") != "evallab.task_variant/v1"
                or rec.get("transform") != "verifier-repair@1"
            ):
                raise BuildError("repairs must be actual verifier-repair@1 VariantRecords")
            self.repairs.setdefault(canonical_task(rec["task_name"]), []).append((rec, ref))
        self.controls: dict[tuple[str, str], dict[str, Any]] = {}
        for ref in inputs.get("controls", []):
            ctrl = evidence.json(ref)
            identity = grader_identity(ctrl.get("grader_identity"))
            if identity in self.controls and self.controls[identity] != ctrl:
                raise BuildError(f"conflicting native controls for {identity}")
            self.controls[identity] = ctrl
        self.envcheck_controls: dict[tuple[str, str], tuple[dict[str, Any], Path]] = {}
        if inputs.get("envcheck_manifest"):
            path = evidence.bound(inputs["envcheck_manifest"])
            manifest = evidence.json(inputs["envcheck_manifest"])
            if not isinstance(manifest.get("variants"), list):
                raise BuildError("EnvCheck input is not the existing repaired-v6 variant manifest")
            for variant in manifest["variants"]:
                identity = grader_identity(
                    {
                        "task_name": canonical_task(variant.get("task", "")),
                        "variant_digest": variant.get("variant_digest"),
                    }
                )
                if identity in self.envcheck_controls:
                    raise BuildError("ambiguous retained EnvCheck repair/control identity")
                self.envcheck_controls[identity] = (variant, path.parent)
        self.regrades: dict[str, list[dict[str, Any]]] = {}
        for ref in inputs.get("regrades", []):
            for row in rows_from_document(evidence, ref):
                self.regrades.setdefault(str(row.get("execution_id")), []).append(row)
        self.faults: dict[str, set[str]] = {}
        for ref in inputs.get("audits", []):
            for row in evidence.json(ref).get("observations", []):
                self.faults.setdefault(str(row.get("execution_id")), set()).add(
                    row.get("evidence_stratum", "unclear")
                )
        self._packages: dict[tuple[Path, str, str], dict[str, Any]] = {}
        self._checked_controls: set[tuple[str, str]] = set()

    def package(self, path: str, expected: str, task: str) -> dict[str, Any]:
        from evallab.task_variants import task_directory_digest

        grader_identity({"task_name": task, "variant_digest": expected})
        if not isinstance(path, str) or not path:
            raise BuildError("grader selection lacks its actual package path")
        package = self.evidence.path(path, relative_to=REPO)
        key = package, task, expected
        if key not in self._packages:
            if task_directory_digest(package) != expected:
                raise BuildError(f"grader package digest mismatch: {package}")
            # Bind all consumed package bytes, including hidden files, in the receipt.
            for file in sorted(package.rglob("*")):
                if file.is_file():
                    self.evidence.capture(file)
            config = tomllib.loads((package / "task.toml").read_text())
            parent = (config.get("metadata") or {}).get("source_id")
            if canonical_task(parent or (config.get("task") or {}).get("name")) != task:
                raise BuildError(f"grader package parent mismatch: {package}")
            self._packages[key] = {"path": package, "config": config, "task": task}
        return self._packages[key]

    def selection(self, alias: Any, task: str, v6: dict[str, str]) -> tuple[set[str], set[str]]:
        """Prove a selection route; provenance alone does not grant a qualification role."""
        from evallab.task_variants import VariantRecord

        fields = {
            "label",
            "transform",
            "variant_digest",
            "package",
            "records",
            "manifest",
            "manifest_row",
            "source",
        }
        if not isinstance(alias, dict) or not fields <= alias.keys():
            raise BuildError("missing native grader selection alias provenance")
        _, expected = grader_identity(
            {"task_name": task, "variant_digest": alias["variant_digest"]}
        )
        transform, records = alias["transform"], alias["records"]
        if (
            not isinstance(transform, str)
            or alias["label"] != f"{transform}:{expected[7:19]}"
            or not isinstance(records, list)
            or not records
            or any(not isinstance(record, str) or not record for record in records)
        ):
            raise BuildError("grader selection label/record provenance mismatch")
        self.package(alias["package"], expected, task)
        roles: set[str] = set()
        claims = set(records)
        if transform == "mimo-clean-v6":
            manifest = self.evidence.bound(self.v6_ref)
            if (
                alias["source"] is not None
                or not isinstance(alias["manifest"], str)
                or self.evidence.path(alias["manifest"], relative_to=REPO) != manifest
                or alias["manifest_row"] != v6
                or expected != v6["final_digest"]
            ):
                raise BuildError("v6 alias is not bound to the exact pinned manifest package")
            for reference in records:
                path, separator, task_id = reference.rpartition("#")
                if (
                    not separator
                    or task_id != v6["task_id"]
                    or self.evidence.path(path, relative_to=REPO) != manifest
                ):
                    raise BuildError("v6 alias record does not bind its native manifest row")
            roles.add("mimo-clean-v6")
        elif transform == "verifier-repair@1":
            if (
                alias["source"] is not None
                or alias["manifest"] is not None
                or alias["manifest_row"] != {}
            ):
                raise BuildError("repair alias has unsupported manifest/source provenance")
            for reference in records:
                path = self.evidence.path(reference, relative_to=REPO)
                matching = [
                    rec
                    for rec, ref in self.repairs.get(task, [])
                    if self.evidence.bound(ref) == path
                ]
                if not matching or any(
                    grader_identity(rec) != (task, expected) for rec in matching
                ):
                    raise BuildError(
                        "repair alias lacks its exact native task/digest/record binding"
                    )
                record = matching[0]
                parent = (record.get("parent") or {}).get("digest")
                if parent == expected:
                    raise BuildError("repair alias has cyclic/no-op native ancestry")
                if parent != v6["final_digest"]:
                    # A real earlier repair can alias v6, but its original package
                    # must still exist and reproduce its native parent claim.
                    origin = record["parent"]["source"]
                    if origin.get("kind") == "local":
                        self.package(origin.get("path", ""), parent, task)
                    elif origin.get("kind") == "variant":
                        from evallab.task_variants import default_variants_root

                        parent_path = self.evidence.path(origin["record"], relative_to=REPO)
                        parent_record = self.evidence.observed_json(parent_path)
                        try:
                            VariantRecord.model_validate(parent_record)
                        except ValueError as exc:
                            raise BuildError(f"invalid repair parent VariantRecord: {exc}") from exc
                        if grader_identity(parent_record) != (task, parent):
                            raise BuildError("repair alias has a different native parent record")
                        parent_package = (
                            default_variants_root(REPO) / task.replace("/", "__") / parent[7:19]
                        )
                        self.package(str(parent_package), parent, task)
                    else:
                        raise BuildError(
                            "repair alias lacks retained native original-package custody"
                        )
                if parent == v6["final_digest"]:
                    if set(record.get("components_changed", [])) - {"verifier"}:
                        raise BuildError("repair changes agent-visible task/environment/solution")
                    if not record.get("files") or any(
                        f.get("path") not in {"tests/test.patch", "tests/test_command.sh"}
                        for f in record["files"]
                    ):
                        raise BuildError("repair is not a test-only native variant")
                    roles.add("verifier-repair@1")
        elif transform == "verifier-repair@1>separate-verifier@10":
            source = alias["source"]
            if (
                alias["manifest"] is not None
                or alias["manifest_row"] != {}
                or not isinstance(source, dict)
                or source.get("transform") != "verifier-repair@1"
            ):
                raise BuildError("composed repair alias lacks its original repair selection")
            source_roles, source_claims = self.selection(source, task, v6)
            for reference in records:
                path = self.evidence.path(reference, relative_to=REPO)
                record = self.evidence.observed_json(path)
                try:
                    VariantRecord.model_validate(record)
                except ValueError as exc:
                    raise BuildError(f"invalid composed native VariantRecord: {exc}") from exc
                parent = record.get("parent") or {}
                if (
                    grader_identity(record) != (task, expected)
                    or record.get("transform") != "separate-verifier@10"
                    or parent.get("digest") != source["variant_digest"]
                ):
                    raise BuildError("composed repair task/digest/source ancestry mismatch")
                origin = parent.get("source") or {}
                if origin.get("kind") == "local":
                    self.package(origin.get("path", ""), source["variant_digest"], task)
                elif origin.get("kind") == "variant":
                    parent_path = self.evidence.path(origin.get("record", ""), relative_to=REPO)
                    if parent_path not in {
                        self.evidence.path(claim, relative_to=REPO) for claim in source_claims
                    }:
                        raise BuildError("composed repair names a different native source record")
                else:
                    raise BuildError("composed repair lacks native original-package custody")
            claims = {f"{parent} > {record}" for parent in source_claims for record in records}
            roles.update(source_roles & {"verifier-repair@1"})
        else:
            raise BuildError(f"unsupported clean grader selection transform: {transform}")
        return roles, claims

    def target(
        self, row: dict[str, Any], source: Source
    ) -> tuple[set[str], tuple[str, str], dict[str, Any]]:
        from evallab.storage.paths import shared_checkout_root

        identity = grader_identity(row.get("grader_identity"))
        task, expected = identity
        gs = row.get("grader_source")
        if (
            task != source.task
            or not isinstance(gs, dict)
            or grader_identity(gs.get("identity")) != identity
            or grader_identity(gs) != identity
            or row.get("transform") != gs.get("transform")
            or row.get("grader") != gs.get("label")
            or gs.get("label") != f"{gs.get('transform')}:{expected[7:19]}"
        ):
            raise BuildError("regrade physical task/transform/digest identity mismatch")
        v6 = self.v6.get(task)
        if (
            not v6
            or v6.get("status") != "built"
            or not set(v6.get("chain", "").split(">")) >= V6_CHAIN
        ):
            raise BuildError("missing globally hardened v6 ancestry")
        grader_identity({"task_name": task, "variant_digest": v6.get("final_digest")})
        manifest_package = v6.get("package_path")
        if not isinstance(manifest_package, str) or not manifest_package:
            raise BuildError("v6 manifest lacks its actual native package path")
        manifest_path = self.evidence.path(manifest_package, relative_to=shared_checkout_root(REPO))
        self.package(str(manifest_path), v6["final_digest"], task)
        package = self.package(gs.get("package", ""), expected, task)
        aliases = gs.get("aliases")
        if not isinstance(aliases, list) or not aliases:
            raise BuildError("missing native physical grader selection aliases")
        roles: set[str] = set()
        representative = False
        for alias in aliases:
            if not isinstance(alias, dict) or alias.get("variant_digest") != expected:
                raise BuildError("final grader alias does not bind the full physical digest")
            selected_roles, claims = self.selection(alias, task, v6)
            roles.update(selected_roles)
            if (
                alias["transform"] == gs["transform"]
                and self.evidence.path(alias["package"], relative_to=REPO) == package["path"]
                and gs.get("record") in claims
            ):
                representative = True
        if not representative or not roles:
            raise BuildError("physical grader lacks a proven representative/qualification route")
        return roles, identity, package

    def native_replay(
        self,
        row: dict[str, Any],
        identity: tuple[str, str],
        score: int,
        *,
        script: str | None = None,
    ) -> None:
        from evallab.registry import compute_task_digests
        from evallab.results import load_job
        from evallab.runner import matrix_run_outcome, staged_matrix_request
        from evallab.schemas import ExperimentMatrix

        if row.get("refusals") or row.get("execution_error") or row.get("regraded") != score:
            raise BuildError("replay was refused, failed, unscored or has a different reward")
        if row.get("input_kind") != "native_patch_replay":
            raise BuildError(
                "missing native patch replay custody (snapshot-only summaries need native matrix evidence)"
            )
        if grader_identity(row.get("grader_identity")) != identity:
            raise BuildError("native replay physical grader identity mismatch")
        _, expected = identity
        receipt_path = self.evidence.path(row.get("matrix_receipt", ""))
        receipt = self.evidence.observed_json(receipt_path)
        matrix_doc, outcomes = receipt.get("matrix") or {}, receipt.get("results") or []
        if matrix_doc.get("task_package_digest") != expected or len(outcomes) != 1:
            raise BuildError("native matrix grader digest/result multiplicity mismatch")
        result = outcomes[0]
        if (
            result.get("status") not in {"ok", "mismatch"}
            or result.get("error")
            or result.get("rewards") != [score]
        ):
            raise BuildError("native matrix is not an observed scored replay on the exact grader")
        try:
            matrix = ExperimentMatrix.model_validate(matrix_doc)
        except ValueError as exc:
            raise BuildError(f"invalid native replay matrix: {exc}") from exc
        if matrix.jobs_dir != "runs" or len(matrix.runs) != 1:
            raise BuildError("unsupported native QA replay matrix layout")
        run = matrix.runs[0]
        if (
            run.agent != "oracle"
            or run.attempts != 1
            or not run.solution
            or result.get("name") != run.name
        ):
            raise BuildError("native replay matrix lacks its single oracle source script")
        replay_root = receipt_path.parents[2]
        original = compute_task_digests((replay_root / matrix.task).resolve())
        if original.package != expected or original.verifier != matrix.verifier_digest:
            raise BuildError("native replay original grader bytes do not match the matrix")
        script_path = self.evidence.path(run.solution, relative_to=replay_root)
        script_ref = self.evidence.capture(script_path)
        script_hash = script_ref["sha256"][7:]
        if row.get("script_sha256") != script_hash or result.get("solution_sha256") != script_hash:
            raise BuildError("native replay script hash mismatch")
        if script is not None and script_path.read_text() != script:
            raise BuildError("native replay script is not the actual source/control patch")
        # A solution override changes the full package digest. Reconstruct the
        # canonical snapshot rather than equating it with the original grader.
        with staged_matrix_request(matrix, run, repo_root=replay_root) as (_, provenance):
            staged_digest = provenance["staged_task_digest"]
        if result.get("staged_task_digest") != staged_digest:
            raise BuildError("native replay solution-staged package digest mismatch")
        job_path = self.evidence.path(row.get("job_dir", ""))
        if job_path != (replay_root / matrix.jobs_dir / run.name).resolve():
            raise BuildError("native replay job does not belong to its matrix run")
        try:
            job = load_job(job_path)
        except (OSError, ValueError) as exc:
            raise BuildError(f"missing/invalid native replay job: {exc}") from exc
        # Retain the native bytes consumed by the canonical outcome qualifier,
        # not just the counterfactual/matrix summary's scalar reward.
        for file in job.files:
            self.evidence.capture(job_path / file.relative_path)
        native = matrix_run_outcome(job, run)
        staging = job.metadata.get("task_staging") or {}
        if (
            native.get("status") not in {"ok", "mismatch"}
            or native.get("error")
            or native.get("rewards") != [score]
            or job.metadata.get("exit_code") != 0
            or job.metadata.get("timed_out")
            or staging.get("source_package_digest") != staged_digest
            or not staging.get("source_harbor_digest")
            or staging.get("source_harbor_digest") != staging.get("staged_harbor_digest")
        ):
            raise BuildError("native replay outcome/staging disagrees with its matrix receipt")

    def retained_envcheck_controls(
        self, identity: tuple[str, str], package: dict[str, Any]
    ) -> None:
        """Consume actual EnvCheck Harbor results/stability, never manifest booleans alone."""
        task, expected = identity
        variant, archive = self.envcheck_controls[identity]
        if (
            package["task"] != task
            or variant.get("variant_digest") != expected
            or canonical_task(variant.get("task", "")) != package["task"]
            or variant.get("status") != "repaired"
            or variant.get("declared_survivors") != 0
            or variant.get("v6_oracle_x3") != [1, 1, 1]
            or variant.get("v6_nop_x3") != [0, 0, 0]
        ):
            raise BuildError(
                "retained EnvCheck repair is partial, has survivors or lacks valid controls"
            )
        short = package["task"].split("format-code-task-")[-1]
        for agent, score in (("oracle", 1), ("nop", 0)):
            job = archive / "v6-controls/jobs" / f"v6rep-{short}-{agent}"
            metadata = self.evidence.observed_json(job / "lab-metadata.json")
            summary = self.evidence.observed_json(job / "result.json")
            staging = metadata.get("task_staging") or {}
            if (
                metadata.get("timed_out")
                or metadata.get("exit_code") != 0
                or staging.get("source_package_digest") != expected
                or staging.get("declared_task_name") != package["task"]
                or not staging.get("source_harbor_digest")
                or staging.get("source_harbor_digest") != staging.get("staged_harbor_digest")
            ):
                raise BuildError(
                    "EnvCheck native control is not bound to the exact staged repaired grader"
                )
            evaluations = (summary.get("stats") or {}).get("evals") or {}
            names = [
                name
                for evaluation in evaluations.values()
                for group in ((evaluation.get("reward_stats") or {}).get("reward") or {}).values()
                for name in group
            ]
            if len(names) != 1 or Path(names[0]).name != names[0]:
                raise BuildError("EnvCheck native control is missing or ambiguous")
            trial = job / names[0]
            result = self.evidence.observed_json(trial / "result.json")
            config = result.get("config") or {}
            if (
                config.get("job_id") != summary.get("id")
                or (config.get("agent") or {}).get("name") != agent
                or result.get("exception_info") is not None
                or reward(result) != score
                or canonical_task(result.get("task_name", "")) != package["task"]
            ):
                raise BuildError(
                    "EnvCheck native nop/reference control result disagrees with custody or reward"
                )
            stability = self.evidence.observed_json(trial / "verifier/stability.json")
            runs = stability.get("runs") or []
            if (
                stability.get("schema") != "evallab.verifier_stability/v1"
                or stability.get("repeat_n_requested") != 3
                or stability.get("n_runs") != 3
                or len(runs) != 3
                or any(run.get("reward") != score or run.get("error") for run in runs)
            ):
                raise BuildError(
                    "EnvCheck native control lacks three actual consistent scored verifications"
                )
            reward_file = trial / "verifier/reward.txt"
            self.evidence.capture(reward_file)
            if float(reward_file.read_text().strip()) != score:
                raise BuildError("EnvCheck native reward file disagrees with its result")

    def controls_ok(self, identity: tuple[str, str], package: dict[str, Any]) -> None:
        if package["task"] != identity[0]:
            raise BuildError("native controls have a different physical task identity")
        if identity in self._checked_controls:
            return
        if identity in self.envcheck_controls:
            self.retained_envcheck_controls(identity, package)
            if identity not in self.controls:
                self._checked_controls.add(identity)
                return
        from evallab.qa import counterfactual as cf

        ctrl = self.controls.get(identity)
        if (
            not ctrl
            or grader_identity(ctrl.get("grader_identity")) != identity
            or ctrl.get("engine") != cf.ENGINE_VERSION
            or ctrl.get("nop") != 0
            or ctrl.get("reference") != 1
            or ctrl.get("valid") is not True
        ):
            raise BuildError(
                "missing valid native nop/reference controls; scalar regrade controls are not evidence"
            )
        patch = cf.reference_patch(package["task"], REPO)
        if patch is None or hashlib.sha256(patch.encode()).hexdigest() != ctrl.get(
            "reference_sha256"
        ):
            raise BuildError(
                "native reference control lacks the exact retained reference patch binding"
            )
        workdir = package["config"]["environment"]["workdir"]
        control_rows = ctrl.get("rows") or {}
        self.native_replay(
            control_rows.get("nop") or {}, identity, 0, script=cf._patch_script("", workdir)
        )
        kept, _ = cf.strip_binary_artifacts(patch)
        self.native_replay(
            control_rows.get("reference") or {}, identity, 1, script=cf._patch_script(kept, workdir)
        )
        self._checked_controls.add(identity)

    def regrade(
        self, row: dict[str, Any], source: Source, score: int
    ) -> tuple[set[str], tuple[str, str]]:
        from evallab.qa import counterfactual as cf

        if row.get("engine") != cf.ENGINE_VERSION or row.get("execution_id") != source.key[1]:
            raise BuildError("regrade is not bound to the native source execution")
        trial = self.evidence.path(row.get("trial", ""))
        original = self.evidence.observed_json(trial / "result.json")
        key = ((original.get("config") or {}).get("job_id"), original.get("id"))
        observed = reward(original)
        exit_status = ((original.get("agent_result") or {}).get("metadata") or {}).get(
            "native_exit_status"
        )
        if (
            key != source.key
            or type(observed) not in (int, float)
            or observed not in {0, 1}
            or observed != reward(source.result)
            or row.get("recorded") != observed
            or original.get("exception_info") is not None
            or not original.get("finished_at")
            or exit_status not in {None, "Idle"}
        ):
            raise BuildError("regrade source identity/clean native recorded-outcome binding mismatch")
        if (
            self.evidence.capture(trial / "agent/trajectory.json")["sha256"]
            != self.evidence.capture(source.trajectory_path)["sha256"]
        ):
            raise BuildError("regrade and training targets use different source trajectory bytes")
        if cf.source_digest(trial) != row.get("source_digest"):
            raise BuildError("regrade source digest is stale or unbound")
        # source_digest reads these paths, so retain the same present-byte dependencies.
        for relative in (
            "config.json",
            "verifier/agent.diff",
            "artifacts/manifest.json",
            "artifacts/var/tmp/mimo-separate/workspace.tgz",
            "artifacts/logs/agent/trajectory.json",
        ):
            if (trial / relative).is_file():
                self.evidence.capture(trial / relative)
        roles, identity, package = self.target(row, source)
        self.controls_ok(identity, package)
        diff_path = trial / "verifier/agent.diff"
        self.evidence.capture(diff_path)
        kept, _ = cf.strip_binary_artifacts(diff_path.read_text())
        script = cf._patch_script(kept, package["config"]["environment"]["workdir"])
        self.native_replay(row, identity, score, script=script)
        if row.get("outcome") != ("stays" if score == observed else "flips"):
            raise BuildError("regrade outcome disagrees with native observed rewards")
        return roles, identity

    def clean(self, source: Source) -> list[dict[str, Any]]:
        if self.faults.get(source.key[1], set()) & FAULTS:
            raise BuildError("source has an unresolved/observed functional fault")
        rows = self.regrades.get(source.key[1], [])
        if not rows:
            raise BuildError("no hash-bound clean regrade evidence")
        accepted: dict[tuple[str, str], dict[str, Any]] = {}
        kinds: set[str] = set()
        errors = []
        for row in rows:
            try:
                roles, identity = self.regrade(row, source, int(row.get("regraded") == 1))
                if row.get("regraded") != 1:
                    raise BuildError("source fails a qualified clean grader")
                kinds.update(roles)
                if identity in accepted and accepted[identity] != row:
                    raise BuildError("conflicting observations for the same physical grader")
                accepted[identity] = row
            except BuildError as exc:
                errors.append(str(exc))
        # Refused/malformed supplied evidence is not silently ignored in favor of a pass.
        if errors:
            raise BuildError("; ".join(sorted(set(errors))))
        if kinds != {"mimo-clean-v6", "verifier-repair@1"}:
            raise BuildError("requires observed pass on BOTH mimo-clean-v6 and verifier-repair@1")
        return list(accepted.values())


@dataclass
class Prepared:
    source: Source
    messages: list[dict[str, Any]]
    sequence_tokens: int
    assistant_loss_tokens: int
    provenance: dict[str, Any] = field(default_factory=dict)
    loss: str = "all"


def require_minimum(rows: list[Prepared], minimum: int = MINIMUM_B) -> None:
    if minimum < MINIMUM_B:
        raise BuildError(f"B-v2 minimum cannot be smaller than {MINIMUM_B}")
    distinct = {row.source.key for row in rows if row.assistant_loss_tokens > 0}
    if len(distinct) != len(rows) or len(distinct) < minimum:
        raise BuildError(
            f"B-v2 insufficient distinct nonzero-loss verified-clean rows: supplied={len(rows)}, distinct_nonzero={len(distinct)}, required={minimum}"
        )


def matched_control(pool: list[Prepared], b: list[Prepared], seed: int) -> list[Prepared]:
    """Exact positive-weight subset sum, randomized order; not uniform subset sampling.

    Two reachability bitsets track whether a non-B row has been included. Thus even
    when B is the only feasible subset this refuses instead of returning B itself.
    Prefix bitsets permit deterministic reconstruction without millions of Python
    predecessor objects. No row repetition, partial conversation or token padding.
    """
    b_keys = {r.source.key for r in b}
    target = sum(r.assistant_loss_tokens for r in b)
    if target <= 0 or any(r.assistant_loss_tokens <= 0 for r in pool):
        raise BuildError("exact exposure matching requires positive assistant-loss token weights")
    if len({r.source.key for r in pool}) != len(pool) or not b_keys <= {r.source.key for r in pool}:
        raise BuildError("C pool must contain distinct original-pass rows including every B source")
    ordered = sorted(pool, key=lambda row: row.source.key)
    random.Random(seed).shuffle(ordered)
    cap = (1 << (target + 1)) - 1
    history = [(1, 0)]
    for row in ordered:
        without, with_other = history[-1]
        shift = row.assistant_loss_tokens
        if row.source.key in b_keys:
            history.append(
                ((without | (without << shift)) & cap, (with_other | (with_other << shift)) & cap)
            )
        else:
            history.append((without, (with_other | ((without | with_other) << shift)) & cap))
    if not ((history[-1][1] >> target) & 1):
        raise BuildError(
            f"C-v2 infeasible exact subset sum: target_loss_tokens={target}, pool_rows={len(pool)}; B itself is not a control"
        )
    remaining, other, selected = target, True, []
    for index in range(len(ordered) - 1, -1, -1):
        row = ordered[index]
        without, with_other = history[index]
        previous = with_other if other else without
        if (previous >> remaining) & 1:
            continue
        weight = row.assistant_loss_tokens
        selected.append(row)
        remaining -= weight
        if row.source.key not in b_keys and other:
            other = not bool((without >> remaining) & 1)
    if (
        remaining
        or other
        or sum(r.assistant_loss_tokens for r in selected) != target
        or {r.source.key for r in selected} == b_keys
    ):
        raise BuildError("exact subset-sum reconstruction failed")
    return sorted(selected, key=lambda row: row.source.key)


def correction_environment(evidence: Evidence, corrected: Source, expected: str) -> None:
    """A repaired grader is not permission to expose that grader to the agent."""
    if corrected.result.get("verifier_environment_mode") != "separate":
        raise BuildError("D correction lacks a native separate-verifier source boundary")
    ref = corrected.provenance.get("metadata")
    if not ref:
        raise BuildError("D missing hash-bound native correction task-staging metadata")
    staging = evidence.json(ref).get("task_staging") or {}
    if (
        staging.get("source_package_digest") != expected
        or staging.get("declared_task_name") != corrected.task
        or not staging.get("source_harbor_digest")
        or staging.get("source_harbor_digest") != staging.get("staged_harbor_digest")
    ):
        raise BuildError("D correction was not executed on the exact separate repaired grader")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _project_feedback(raw: str) -> str:
    """Recompute EnvCheck's finite-vocabulary v1 projection, never forward a raw line."""
    counts = r"(?:[0-9]+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?))"
    summary_pattern = re.compile(
        rf"(?:=+\s*)?({counts}(?:, {counts})*)"
        r"(?: in [0-9]+(?:\.[0-9]+)?s(?: \([0-9:]+\))?)?(?:\s*=+)?"
    )
    summaries, checks, exceptions = [], {}, set()
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw)
    for line in text.replace("\r", "\n").splitlines():
        line = line.strip()
        summary = summary_pattern.fullmatch(line)
        if summary:
            summaries.append({
                kind: int(count)
                for count, kind in re.findall(r"([0-9]+) ([a-z]+)", summary[1])
            })
        unittest = re.fullmatch(
            r"FAILED \((failures=[0-9]+(?:, errors=[0-9]+)?|errors=[0-9]+)\)", line
        )
        if unittest:
            summaries.append({
                kind: int(count)
                for kind, count in re.findall(r"(failures|errors)=([0-9]+)", unittest[1])
            })
        failed = re.match(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+.*)?$", line)
        progress = re.fullmatch(r"(\S+::\S+)\s+(FAILED|ERROR)(?:\s+\[\s*[0-9]+%\])?", line)
        unitcase = re.match(r"^(FAIL|ERROR):\s+(.+)$", line)
        token = (
            failed[2] if failed else progress[1] if progress else unitcase[2] if unitcase else None
        )
        if token:
            check = digest(token.encode())
            error = (
                (failed and failed[1] == "ERROR")
                or (progress and progress[2] == "ERROR")
                or (unitcase and unitcase[1] == "ERROR")
            )
            checks[check] = {"check": check, "outcome": "error" if error else "failed"}
        exception = re.match(r"^(?:E\s+)?([A-Za-z]+)(?::|$)", line)
        if exception and exception[1] in FEEDBACK_EXCEPTIONS:
            exceptions.add(exception[1])
        if failed and " - " in line:
            name = line.split(" - ", 1)[1].split(":", 1)[0]
            if name in FEEDBACK_EXCEPTIONS:
                exceptions.add(name)
    if not checks and not exceptions and not any(
        sum(row.get(kind, 0) for kind in ("failed", "failures", "error", "errors")) > 0
        for row in summaries
    ):
        raise BuildError("D stdout has no safely recognized failing diagnostic")
    return _canonical_bytes({
        "policy": FEEDBACK_POLICY,
        "outcome_counts": summaries,
        "failing_checks": sorted(checks.values(), key=lambda item: item["check"]),
        "exception_classes": sorted(exceptions),
    }).decode() + "\n"


def _correction_model(source: Source) -> str:
    config = source.result.get("config") or {}
    agent = config.get("agent") or {}
    metadata = (source.result.get("agent_result") or {}).get("metadata") or {}
    if (
        source.result.get("exception_info") is not None
        or config.get("source_trial")
        or agent.get("name") in {"oracle", "nop"}
        or "codex" in str(agent.get("name", "")).lower()
        or metadata.get("teacher")
        or metadata.get("copied_fix")
    ):
        raise BuildError("D refuses abnormal, teacher, copied or replay-only native executions")
    info = (source.result.get("agent_info") or {}).get("model_info") or {}
    model = agent.get("model_name")
    if not model:
        model = f"{info['provider']}/{info['name']}" if info.get("provider") else info.get("name")
    if not isinstance(model, str) or not model:
        raise BuildError("D missing actual native model selector")
    return model


def _correction_request(
    evidence: Evidence,
    ref: dict[str, Any],
    original: Source,
    corrected: Source,
    before: dict[str, Any],
    repaired_digest: str,
    original_messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Bind the actual producer request, while independently checking its native evidence."""
    from evallab.task_variants import task_directory_digest

    request = evidence.json(ref)
    if (
        request.get("schema") != "envcheck.correction-request/v1"
        or request.get("protocol") != CORRECTION_PROTOCOL
        or request.get("purpose") != "training"
        or request.get("heldout") is not False
        or request.get("partition_scope") != "mechanical_repaired_grader"
        or request.get("source_execution_id") != original.key[1]
        or canonical_task(request.get("task")) != original.task
        or request.get("model") != _correction_model(original)
        or request.get("model") != _correction_model(corrected)
    ):
        raise BuildError("D request protocol/task/source/model binding mismatch")
    load_heldout(evidence, request["heldout_manifest"])
    partition = request["source_partition"]
    variant = partition["variant"]
    if (
        partition.get("execution_id") != original.key[1]
        or partition.get("task") != original.task
        or partition.get("model") != request["model"]
        or partition.get("partition") != "false_pass"
        or partition.get("status") != "measured"
        or partition.get("original_reward") != 1
        or partition.get("repaired_reward") != 0
        or partition.get("repaired_digest") != repaired_digest
        or variant.get("repaired_digest") != repaired_digest
    ):
        raise BuildError("D request is not the bound measured original1/repaired0 source")
    flags = partition.get("flags") or {}
    if flags.get("alias_conflicts") or flags.get("teacher") or flags.get("copied_fix"):
        raise BuildError("D request retains unresolved alias, teacher or copied-fix counterevidence")
    exclusion = variant["manifest_row"]
    if (
        any(exclusion.get(key) for key in (
            "training_excluded", "training_exclusion", "excluded_from_training"
        ))
        or exclusion.get("training_eligible") is False
    ):
        raise BuildError("D request carries a manifest training exclusion")
    selected = partition["provenance"]["selected_receipt"]
    selected_ref = {key: selected[key] for key in ("path", "sha256")}
    index = int(selected["selector"].rsplit("/", 1)[1])
    if (
        selected.get("valid") is not True
        or index < 0
        or rows_from_document(evidence, selected_ref)[index] != before
        or selected.get("row") != before
        or selected.get("row_sha256") != digest(_canonical_bytes(before))
    ):
        raise BuildError("D producer partition differs from the untouched native failed receipt")
    correction = request["provenance"]["correction"]
    if (
        correction.get("protocol") != CORRECTION_PROTOCOL
        or correction.get("source_execution_id") != original.key[1]
        or correction.get("partition_scope") != "mechanical_repaired_grader"
        or any(correction.get(key) is not False for key in (
            "hidden_test_source_included", "reference_source_included",
            "full_historical_context_included",
        ))
        or correction.get("allowed_model_inputs") != [
            "task_prompt", "source_patch", "repaired_failure_stdout"
        ]
    ):
        raise BuildError("D request does not declare the bounded reset-context inputs")
    prompt = evidence.bound(request["task_prompt"]).read_text()
    package_prompt = evidence.bound(correction["task_prompt"]).read_text()
    first_user = next((m for m in original_messages if m.get("role") == "user"), None)
    if not first_user or prompt != first_user.get("content") or prompt.strip() != package_prompt.strip():
        raise BuildError("D request differs from the actual original task prompt")
    patch = evidence.bound(correction["source_patch"]).read_bytes()
    actual_patch = original.result_path.parent / "verifier/agent.diff"
    if (
        not patch.strip()
        or evidence.capture(actual_patch)["sha256"] != digest(patch)
        or evidence.bound(request["patch_input"]).read_bytes() != patch
    ):
        raise BuildError("D request lacks the actual incomplete pre-hidden-test source patch")
    snapshot = correction["task_package_snapshot"]
    package = evidence.path(snapshot["path"])
    native_fields = request["native_spec_fields"]
    if (
        snapshot["sha256"] != repaired_digest
        or task_directory_digest(package) != repaired_digest
        or native_fields.get("task_package_digest") != repaired_digest
        or native_fields.get("model") != request["model"]
        or native_fields.get("agent") != "mimoagent"
        or native_fields.get("task") != original.task
    ):
        raise BuildError("D request repaired snapshot/model pin mismatch")
    for file in sorted(package.rglob("*")):
        if file.is_file():
            evidence.capture(file)
    if (package / "instruction.md").read_text().strip() != prompt.strip():
        raise BuildError("D task snapshot prompt differs from the original native input")
    feedback_path = evidence.bound(correction["feedback"])
    failed_job = evidence.path(before["job_dir"])
    result_path = evidence.bound(correction["feedback_result"])
    failure = evidence.json(correction["feedback_result"])
    if (
        failed_job not in feedback_path.parents
        or feedback_path.parent.name != "verifier"
        or feedback_path.name != "test_output.log"
        or result_path != feedback_path.parent.parent / "result.json"
        or reward(failure) != 0
        or failure.get("exception_info") is not None
    ):
        raise BuildError("D feedback is not the actual noninfra failing native repaired output")
    raw = feedback_path.read_text()
    safe = evidence.bound(correction["safe_feedback"]).read_text()
    if safe != _project_feedback(raw) or request.get("feedback_policy") != {
        "policy": FEEDBACK_POLICY, "raw_sha256": digest(raw.encode()),
        "safe_sha256": digest(safe.encode()), "source_lines_forwarded": 0,
        "exception_messages_forwarded": 0,
    }:
        raise BuildError("D feedback is not the exact source-free outcome projection")
    extra = (
        "Reset-context feedback correction. The following incomplete patch was produced for the task above. "
        "Correct it in the task workspace; this is a new conversation, not restored history.\n\n"
        "Incomplete source patch (pre-hidden-test agent diff):\n" + patch.decode() +
        "\n\nObserved repaired-verifier failing stdout (source-free diagnostic projection; "
        "source, tracebacks, paths and exception messages omitted):\n" + safe
    )
    extra_path = evidence.bound(correction["extra_instruction"])
    if (
        extra_path.read_text() != extra
        or native_fields.get("extra_instruction_sha256") != digest(extra.encode())
        or request.get("request_id") != digest(_canonical_bytes({
            "source_execution_id": original.key[1], "repaired_digest": repaired_digest,
            "extra_instruction_sha256": digest(extra.encode()),
        })).removeprefix("sha256:")
    ):
        raise BuildError("D request contains inputs beyond its bound patch and safe feedback")
    return request, extra


def correction_row(
    evidence: Evidence,
    link: dict[str, Any],
    sources: dict[tuple[str, str], Source],
    qualification: Qualification,
    heldout: set[str],
    qa: Any,
) -> Source:
    try:
        original, corrected = sources[tuple(link["source"])], sources[tuple(link["corrected"])]
    except (KeyError, TypeError) as exc:
        raise BuildError("D missing genuine source/corrected native execution") from exc
    if (
        original.key == corrected.key
        or original.task != corrected.task
        or (original.parents | corrected.parents) & heldout
    ):
        raise BuildError("D correction identity/task/heldout ancestry refusal")
    if reward(original.result) != 1:
        raise BuildError("D requires an observed original native false pass")
    if original.result.get("verifier_environment_mode") != "separate":
        raise BuildError("D original context has no native hidden-test/source separation evidence")
    try:
        before_ref, after_ref = link["failed_regrade"], link["passed_regrade"]
        if any(type(ref.get("row")) is not int or ref["row"] < 0 for ref in (before_ref, after_ref)):
            raise BuildError("D regrade custody needs nonnegative integer row indices")
        before = rows_from_document(evidence, before_ref["document"])[before_ref["row"]]
        after = rows_from_document(evidence, after_ref["document"])[after_ref["row"]]
    except (IndexError, TypeError, KeyError) as exc:
        raise BuildError("D missing actual failed/passed repaired regrade rows") from exc
    roles_before, identity_before = qualification.regrade(before, original, 0)
    roles_after, identity_after = qualification.regrade(after, corrected, 1)
    if (
        "verifier-repair@1" not in roles_before
        or "verifier-repair@1" not in roles_after
        or identity_before != identity_after
    ):
        raise BuildError("D requires false-pass -> true-pass on the same exact repaired grader")
    digest_before = identity_before[1]
    correction_environment(evidence, corrected, digest_before)
    try:
        original_messages, _, fidelity = qa.to_messages(original.trajectory)
        original_native = original.result_path.parent / "agent/mimoagent/native-trajectory.json"
        if original_native.is_file():
            original_messages = evidence.observed_json(original_native)["trajs"]["main"]["messages"]
        elif fidelity != "raw_completion":
            raise BuildError("D lacks the actual original task input and assistant targets")
        request, extra = _correction_request(
            evidence, link["request"], original, corrected, before, digest_before, original_messages
        )
        native_path = corrected.result_path.parent / "agent/mimoagent/native-trajectory.json"
        main = evidence.observed_json(native_path)["trajs"]["main"]
        messages = main["messages"]
        config = corrected.result.get("config") or {}
        metadata = (corrected.result.get("agent_result") or {}).get("metadata") or {}
        if (
            (config.get("agent") or {}).get("name")
            != "evallab.harbor_mimoagent:NativeMimoAgent"
            or config.get("resume_trajectory") or config.get("load_trajectory")
            or (config.get("agent") or {}).get("resume_trajectory")
            or (config.get("agent") or {}).get("load_trajectory")
            or metadata.get("native_exit_status") != "Idle"
            or not corrected.result.get("finished_at")
        ):
            raise BuildError("D requires a clean completed native reset-context main agent")
        first_user = next(i for i, m in enumerate(messages) if m.get("role") == "user")
        prompt = original_messages[next(
            i for i, m in enumerate(original_messages) if m.get("role") == "user"
        )]["content"].strip()
        content = messages[first_user].get("content")
        if (
            any(m.get("role") != "system" for m in messages[:first_user])
            or not isinstance(content, str)
            or not content.strip().startswith(prompt)
            or content.strip()[len(prompt):].strip() != extra.strip()
        ):
            raise BuildError("D native input is not the exact task plus bounded reset feedback")
        target = max(i for i, m in enumerate(messages) if m.get("role") == "assistant")
        message = messages[target]
        if (
            target <= first_user
            or not (message.get("content") or message.get("reasoning_content") or message.get("tool_calls"))
            or message in [m for m in original_messages if m.get("role") == "assistant"]
        ):
            raise BuildError("D has no genuinely new final native assistant target")
        source_patch = evidence.bound(request["provenance"]["correction"]["source_patch"]).read_bytes()
        corrected_patch = corrected.result_path.parent / "verifier/agent.diff"
        evidence.capture(corrected_patch)
        if not corrected_patch.read_bytes().strip() or corrected_patch.read_bytes() == source_patch:
            raise BuildError("D reuses the incomplete source patch instead of a native correction")
    except (KeyError, TypeError, IndexError, StopIteration, ValueError) as exc:
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"D missing/malformed native reset-context request: {exc}") from exc
    if qa.network_fetch(corrected.trajectory):
        raise BuildError("D correction contains observed network answer retrieval")
    corrected.correction_messages, corrected.correction_tools = messages, main.get("tools")
    corrected.provenance = {
        **corrected.provenance,
        "self_correction": link,
        "native_trajectory": evidence.capture(native_path),
        "repaired_digest": digest_before,
        "correction_protocol": CORRECTION_PROTOCOL,
        "feedback_policy": FEEDBACK_POLICY,
        "partition_scope": "mechanical_repaired_grader",
        "semantic_truth": "not_independently_established",
        "full_historical_context_included": False,
        "source_original_reward": reward(original.result),
        "native_original_reward": reward(corrected.result),
        "repaired_before_reward": 0,
        "repaired_after_reward": 1,
        "envcheck_variant": request["source_partition"]["variant"],
        "correction_target_turn": target,
        "target_message_sha256": digest(_canonical_bytes(message)),
    }
    return corrected


def tokenizer_identity(evidence: Evidence, directory: Path, sft: Any) -> tuple[Any, dict[str, Any]]:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise BuildError(
            "--tokenizer-dir must be an existing LOCAL pinned tokenizer; downloads are forbidden"
        )
    if sft.MODEL_REVISION != "2367e865d009c13ac81713a2878291d33ab28177":
        raise BuildError("the v2 tokenizer byte pins no longer match the trainer's model revision")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    files = []
    for name, sha in TOKENIZER_SHA256.items():
        path = directory / name
        evidence.bound({"path": str(path), "sha256": sha})
        files.append(evidence.capture(path))
    unexpected = {"special_tokens_map.json", "added_tokens.json"} & {
        p.name for p in directory.iterdir()
    }
    if unexpected:
        raise BuildError(f"unpinned tokenizer overrides: {sorted(unexpected)}")
    tokenizer = sft.get_tokenizer(str(directory))
    return tokenizer, {
        "model": sft.MODEL_ID,
        "model_revision": sft.MODEL_REVISION,
        "tokenizer_files": files,
        "chat_template_sha256": digest(tokenizer.chat_template.encode()),
        "mask_code": evidence.capture(REPO / "tools/modal-mimo-sft/sft.py"),
        "lossless_code": evidence.capture(
            REPO / "research/experiments/repaired-data-sft-v1/to_export.py"
        ),
        "raw_conversion_code": evidence.capture(
            REPO / "research/experiments/qa-trace/training-arms/build_arms.py"
        ),
        "builder_code": evidence.capture(Path(__file__)),
    }


def prepare(
    source: Source,
    tok: Any,
    v1: Any,
    qa: Any,
    sft: Any,
    max_length: int,
    *,
    target_turn: int | None = None,
) -> Prepared:
    loss = "all"
    if target_turn is None:
        messages, tools, fidelity = qa.to_messages(source.trajectory)
        if fidelity != "raw_completion":
            raise BuildError(f"source fidelity={fidelity}; parsed actions are not assistant targets")
    else:
        if (
            source.correction_messages is None
            or target_turn != source.provenance.get("correction_target_turn")
            or not 0 <= target_turn < len(source.correction_messages)
            or source.correction_messages[target_turn].get("role") != "assistant"
        ):
            raise BuildError("D target is not the genuinely new final native assistant completion")
        messages = source.correction_messages[: target_turn + 1]
        tools, fidelity, loss = source.correction_tools, "native_messages", "last"
    row = {
        "messages": messages,
        "tools": tools,
        "agent": (source.trajectory.get("agent") or {}).get("name"),
        "execution_id": source.key[1],
    }
    try:
        flat = v1.flatten(tok, row)
        rendered = sft.render_and_mask(
            tok,
            flat,
            max_length=max_length,
            label=source.key[1],
            loss=loss,
        )
    except (sft.SftError, ValueError) as exc:
        raise BuildError(str(exc)) from exc
    model = (
        _correction_model(source) if target_turn is not None
        else ((source.result.get("agent_info") or {}).get("model_info") or {}).get("name")
    )
    provenance = {
        **source.provenance,
        "native_pair": list(source.key),
        "execution_id": source.key[1],
        "task": source.task,
        "parent_ancestry": sorted(source.parents),
        "source_model": model,
        "source_model_config_sha256": digest(
            json.dumps(
                (source.result.get("config") or {}).get("agent"),
                sort_keys=True,
            ).encode()
        ),
        "fidelity": fidelity,
        "loss": loss,
        "recorded_messages_sha256": digest(json.dumps(messages, sort_keys=True).encode()),
        "recorded_tools_sha256": digest(json.dumps(tools, sort_keys=True).encode()),
        "sequence_tokens": len(rendered.input_ids),
        "assistant_loss_tokens": rendered.trained,
        "input_ids_sha256": digest(json.dumps(rendered.input_ids).encode()),
        "assistant_loss_mask_sha256": digest(bytes(rendered.mask)),
    }
    if target_turn is not None:
        provenance["target_turn_index"] = target_turn
    return Prepared(source, flat, len(rendered.input_ids), rendered.trained, provenance, loss)


def write_arm(out: Path, rows: list[Prepared], fields: dict[str, Any], v1: Any, sft: Any) -> None:
    if all(row.loss == "all" for row in rows):
        v1.write_export(out, [row.messages for row in rows], fields)
    else:
        out.mkdir()
        data = "".join(
            json.dumps({"messages": row.messages, "loss": row.loss}) + "\n" for row in rows
        )
        (out / sft.CONVERSATIONS_FILE).write_text(data)
        manifest = {
            "contract": sft.EXPORT_CONTRACT,
            "conversations_sha256": digest(data.encode()),
            **fields,
        }
        (out / sft.MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "provenance.jsonl").write_text(
        "".join(json.dumps(row.provenance, sort_keys=True) + "\n" for row in rows)
    )


def totals(rows: list[Prepared], epochs: int) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "tasks": len({r.source.task for r in rows}),
        "sequence_tokens": sum(r.sequence_tokens for r in rows),
        "assistant_loss_tokens": sum(r.assistant_loss_tokens for r in rows),
        "sequence_token_presentations": epochs * sum(r.sequence_tokens for r in rows),
        "assistant_loss_token_presentations": epochs * sum(r.assistant_loss_tokens for r in rows),
        "native_pairs": [list(r.source.key) for r in rows],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if type(args.epochs) is not int or args.epochs < 1:
        raise BuildError("--epochs must be a realized positive integer common full-epoch count")
    if type(args.max_length) is not int or not 1 <= args.max_length <= 65536:
        raise BuildError(
            "--max-length must be positive and no larger than the native 65536 context"
        )
    evidence = Evidence(args.inputs.resolve().parent)
    input_ref = evidence.capture(args.inputs.resolve())
    inputs = evidence.json(input_ref)
    if inputs.get("schema") != SCHEMA:
        raise BuildError(f"expected {SCHEMA}")
    heldout = load_heldout(evidence, inputs["heldout"])
    inventory = evidence.json(inputs["inventory"])
    if inventory.get("schema") != "repaired-data-sft-v2.inventory/1":
        raise BuildError("expected hash-bound native inventory/1")
    v1, qa, sft = conversion_modules()
    tokenizer, identity = tokenizer_identity(evidence, args.tokenizer_dir, sft)
    qualification = Qualification(evidence, inputs)
    discovered, source_refusals = [], []
    for observation in inventory["rows"]:
        try:
            discovered.append(native_source(evidence, observation))
        except BuildError as exc:
            source_refusals.append(
                {
                    "native_pair": [observation.get("job_id"), observation.get("trial_id")],
                    "task": observation.get("task"),
                    "refusal": str(exc),
                }
            )
    sources = deduplicate(discovered)
    source_map = {source.key: source for source in sources}
    excluded = {canonical_task(t) for t in inputs.get("exclusions", [])}
    correction_keys = {tuple(link["corrected"]) for link in inputs.get("corrections", [])}
    pool, b, refusals = [], [], source_refusals
    v6_frame = {
        task
        for task, row in qualification.v6.items()
        if row.get("status") == "built" and set(row.get("chain", "").split(">")) >= V6_CHAIN
    }
    for source in sources:
        entry: dict[str, Any] = {"native_pair": list(source.key), "task": source.task}
        try:
            original_pass_eligible(source, heldout, excluded, correction_keys, v6_tasks=v6_frame)
            row = prepare(source, tokenizer, v1, qa, sft, args.max_length)
            pool.append(row)
            entry.update(
                sequence_tokens=row.sequence_tokens, assistant_loss_tokens=row.assistant_loss_tokens
            )
            if qa.network_fetch(source.trajectory):
                raise BuildError("observed network answer retrieval cannot qualify B")
            row.provenance["clean_regrades"] = qualification.clean(source)
            b.append(row)
        except BuildError as exc:
            entry["refusal"] = str(exc)
            refusals.append(entry)
    receipt: dict[str, Any] = {
        "schema": "repaired-data-sft-v2.build/1",
        "inputs": input_ref,
        "identity": identity,
        "heldout_parents": sorted(heldout),
        "additional_training_exclusions": sorted(excluded),
        "selection_seed": args.seed,
        "schedule": {
            "epochs": args.epochs,
            "loss": "all",
            "packing": False,
            "max_steps": None,
            "max_length": args.max_length,
            "length_policy": "refuse; never truncate",
            "requires_complete_epochs": True,
        },
        "counts": {
            "inventory_observations": len(inventory["rows"]),
            "distinct_native_pairs": len(sources),
            "eligible_original_pass": len(pool),
            "verified_clean_nonzero_loss": len(b),
            "required_b": MINIMUM_B,
        },
        "prospective_pool": totals(pool, args.epochs),
        "prospective_b_not_admitted": totals(b, args.epochs),
        "refusals": refusals,
    }
    receipt["d"] = {
        "status": "not_yet_admitted"
        if inputs.get("corrections")
        else "missing_genuine_verified_corrections",
        "input_links": len(inputs.get("corrections", [])),
        "protocol": CORRECTION_PROTOCOL,
        "feedback_policy": FEEDBACK_POLICY,
    }
    if args.out.exists():
        raise BuildError(f"refusing to overwrite existing output/data: {args.out}")
    args.out.mkdir(parents=True)

    def save() -> None:
        receipt["evidence"] = sorted(evidence.files.values(), key=lambda ref: ref["path"])
        (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    if args.plan:
        receipt["status"] = "prospective_only_not_training_admission"
        receipt["candidate_rows"] = [r.provenance for r in pool]
        save()
        return receipt
    try:
        require_minimum(b)
        c = matched_control(pool, b, args.seed)
        d, d_refusals = [], []
        for link in inputs.get("corrections", []):
            try:
                corrected = correction_row(
                    evidence, link, source_map, qualification, heldout | excluded, qa
                )
                d.append(prepare(
                    corrected, tokenizer, v1, qa, sft, args.max_length,
                    target_turn=corrected.provenance["correction_target_turn"],
                ))
            except BuildError as exc:
                d_refusals.append(
                    {
                        "source": link.get("source"),
                        "corrected": link.get("corrected"),
                        "refusal": str(exc),
                    }
                )
        target_ids = [(r.source.key, r.provenance["target_turn_index"]) for r in d]
        if len(set(target_ids)) != len(target_ids):
            raise BuildError("duplicate corrected native assistant targets in D")
        receipt["d"] = {
            "status": "ready" if d and not d_refusals else "missing_genuine_verified_corrections",
            "refusals": d_refusals,
            "input_links": len(inputs.get("corrections", [])),
            "protocol": CORRECTION_PROTOCOL,
            "feedback_policy": FEEDBACK_POLICY,
        }
        if d_refusals or (args.require_d and not d):
            raise BuildError(
                "D-v2 missing/refused genuine EnvCheck false-pass -> true-pass self-corrections"
            )
        arms = {"B": b, "C": c}
        if d:
            arms["D"] = d
        receipt["arms"] = {arm: totals(rows, args.epochs) for arm, rows in arms.items()}
        exposure = receipt["arms"]["B"]["assistant_loss_token_presentations"]
        if exposure != receipt["arms"]["C"]["assistant_loss_token_presentations"]:
            raise BuildError("B/C realized schedule exposure mismatch")
        receipt["exposure_match"] = {
            "exact": True,
            "assistant_loss_token_presentations": exposure,
            "sampler": "seeded_permutation_feasible_subset_not_uniform",
            "same_original_pass_pool": True,
            "c_is_not_b": True,
        }
        for arm, rows in arms.items():
            manifest = {
                "builder": str(Path(__file__).relative_to(REPO)),
                "arm": arm,
                "rows": len(rows),
                **totals(rows, args.epochs),
                "execution_ids": [r.source.key[1] for r in rows],
                "tasks": sorted({r.source.task for r in rows}),
                "identity": identity,
                "schedule": receipt["schedule"],
                "heldout_applied": True,
                "heldout_parents": sorted(heldout),
                "selection_seed": args.seed,
                "exposure_match": receipt["exposure_match"] if arm in {"B", "C"} else None,
            }
            if arm == "D":
                manifest["schedule"] = {**receipt["schedule"], "loss": "last"}
                manifest["protocol"] = CORRECTION_PROTOCOL
                manifest["feedback_policy"] = FEEDBACK_POLICY
                manifest["partition_scope"] = "mechanical_repaired_grader"
                manifest["semantic_truth"] = "not_independently_established"
                manifest["target_policy"] = (
                    "last_native_main_assistant_target_with_exact_reset_context_prefix"
                )
            write_arm(args.out / arm, rows, manifest, v1, sft)
        receipt["status"] = "ready"
    except BuildError as exc:
        receipt["status"], receipt["error"] = "refused", str(exc)
        save()
        raise
    save()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--epochs", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--max-length", type=int, default=65536)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--require-d", action="store_true")
    args = parser.parse_args()
    try:
        receipt = run(args)
    except (BuildError, OSError, KeyError, ValueError) as exc:
        print(f"repaired-data-sft-v2: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "counts": receipt["counts"],
                "receipt": str(args.out / "receipt.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
