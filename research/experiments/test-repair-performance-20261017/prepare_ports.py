"""Stage retained v6 verifier-repair candidates without executing any controls ($0).

Run from the owned worktree with its native Eval Lab Python environment::

    uv run python research/experiments/test-repair-performance-20261017/prepare_ports.py \
        --root "$PWD" --port-plan /path/to/port-plan.json \
        --accepted-manifest /path/to/repaired-v6-variants-manifest.json \
        --heldout /path/to/heldout.json \
        --out runs/envcheck-20261017/retained-ports

Repeat --task with six-digit suffixes, parent task IDs, or native MiMo task names
for a subset. Accepted variants and the already-prepared 001478 are never staged.
Outputs are immutable-on-conflict, including the candidate manifest: use a new
--out for different inputs or a different subset. Identical invocations are no-ops.

Each candidates/<task>/matrix.json is a native ExperimentMatrix consumable by
qualify.py. It requests the package's Oracle reference, nop, then every exact
retained killed script. Only the parent may launch that qualifier with its own
spend gate. Source receipts/VariantRecords are custody, NOT new Modal evidence;
full/partial is the inherited scope, NOT a qualification or training admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.execution_contracts import SAFE_JOB_NAME, new_ulid
from evallab.registry import compute_subpath_digest, compute_task_digests, harbor_task_digest
from evallab.schemas import ExperimentMatrix, MatrixRun
from evallab.task_variants import VariantRecord, verify

SCHEMA = "envcheck.retained-port-candidates/v1"
PORT_SCHEMA = "mimo-clean-v6-repaired.ports/v1"
TASK_PATTERN = re.compile(r"format-code-task-\d{6}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
EXTRA_SUFFIXES = frozenset({
    "001478", "000511", "002402", "002866", "000769", "002394",
    "002382", "001553", "002222", "001671", "001425", "001770",
})
ALREADY_PREPARED = frozenset({"format-code-task-001478"})
NOTICE = (
    "Preparation only: no provider, model, Docker, verifier, queue or qualification "
    "execution. Every staged repair remains status=candidate and qualified=false. "
    "Old receipts and full/partial dispositions describe retained custody and scope, "
    "not new Modal proof. Passing the listed controls cannot resolve unadmitted or "
    "remaining scope, remove training exclusions, or turn a partial repair into full."
)


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def task_id(value: str) -> str:
    name = value.removeprefix("mimo-v2.6-rl/")
    if re.fullmatch(r"\d{6}", name):
        name = "format-code-task-" + name
    if TASK_PATTERN.fullmatch(name) is None:
        raise ValueError(f"not a MiMo parent task ID: {value!r}")
    return name


def parent_id(task: str) -> str:
    return "mimo-v2.6-rl/" + task_id(task)


def source_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve(strict=True)


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ValueError(f"{label} mismatch: {actual!r} != {expected!r}")


def read_source(path: Path, expected: str | None = None) -> tuple[bytes, dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"source is not a file: {path}")
    data = path.read_bytes()
    actual = sha256(data)
    if expected is not None:
        if not isinstance(expected, str) or DIGEST_PATTERN.fullmatch(expected) is None:
            raise ValueError(f"invalid pinned source digest for {path}: {expected!r}")
        require_equal(actual, expected, f"source SHA-256 {path}")
    return data, {"path": str(path), "sha256": actual, "bytes": len(data)}


def snapshot(
    path: Path, destination: Path, outputs: dict[Path, bytes], expected: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    data, reference = read_source(path, expected)
    outputs[destination] = data
    reference["snapshot"] = str(destination)
    return json.loads(data), reference


def unique_tasks(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        task = task_id(row["task"])
        if task in indexed:
            raise ValueError(f"duplicate {label} task: {task}")
        indexed[task] = row
    return indexed


def assert_no_symlinks(directory: Path) -> None:
    if not directory.is_dir():
        raise ValueError(f"package directory missing: {directory}")
    if any(path.is_symlink() for path in directory.rglob("*")):
        raise ValueError(f"native package snapshots reject symlinks: {directory}")


def validate_record(
    port: dict[str, Any], record: VariantRecord, old_record: VariantRecord,
    old_receipt: dict[str, Any], *, package: Path, parent: Path, root: Path,
    port_base: Path,
) -> Any:
    task = task_id(port["task"])
    require_equal(record.task_name, parent_id(task), f"{task} native record task")
    require_equal(old_record.task_name, record.task_name, f"{task} retained record task")
    require_equal(record.variant_digest, port["ported_digest"], f"{task} variant digest")
    require_equal(record.parent.digest, port["v6_parent_digest"], f"{task} parent digest")
    require_equal(old_record.variant_digest, port["old_variant_digest"], f"{task} old digest")
    require_equal(old_receipt.get("schema"), "evallab.verifier_repair.result/v1",
                  f"{task} retained receipt schema")
    require_equal(old_receipt.get("task"), record.task_name, f"{task} retained receipt task")
    require_equal(old_receipt.get("variant_digest"), old_record.variant_digest,
                  f"{task} retained receipt variant")
    for key, expected in (
        ("port_of_variant", port["old_variant_digest"]),
        ("source_record_sha256", port["old_record_sha256"]),
        ("source_receipt_sha256", port["old_receipt_sha256"]),
        ("v6_parent_digest", port["v6_parent_digest"]),
    ):
        require_equal(record.inputs.get(key), expected, f"{task} retained input {key}")
    require_equal(record.parent.source.kind, "local", f"{task} parent source kind")
    require_equal(source_path(record.parent.source.path or "", base=port_base), parent,
                  f"{task} native parent source path")
    require_equal(package, package.parent.parent / record.task_slug / record.digest12,
                  f"{task} native materialized package location")
    require_equal(record.transform, "verifier-repair@1", f"{task} retained transform")
    require_equal(record.components_changed, ["verifier"], f"{task} changed components")
    for change in record.files:
        relative = Path(change.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"record change escapes package: {change.path!r}")
        live = package / relative
        actual = sha256(live.read_bytes()) if live.is_file() else None
        require_equal(actual, change.after_sha256, f"{task} live record file {change.path}")
    assert_no_symlinks(package)
    assert_no_symlinks(parent)
    failures = verify(record, parent_dir=parent, repo_root=root,
                      variants_root=package.parent.parent)
    if failures:
        raise ValueError(f"{task} native VariantRecord verification failed: {'; '.join(failures)}")
    digests = compute_task_digests(package)
    parent_digests = compute_task_digests(parent)
    require_equal(digests.package, port["ported_digest"], f"{task} live package digest")
    require_equal(digests.verifier, port["verifier_digest"], f"{task} live verifier digest")
    require_equal(parent_digests.package, port["v6_parent_digest"], f"{task} live parent digest")
    require_equal(harbor_task_digest(package), record.variant_harbor_digest,
                  f"{task} native Harbor variant digest")
    require_equal(harbor_task_digest(parent), record.parent.harbor_digest,
                  f"{task} native Harbor parent digest")
    for component in ("task_toml", "instruction", "environment"):
        require_equal(getattr(digests, component), getattr(parent_digests, component),
                      f"{task} unchanged {component}")
    require_equal(compute_subpath_digest(package / "solution"),
                  compute_subpath_digest(parent / "solution"), f"{task} unchanged solution")
    if not (package / "solution/solve.sh").is_file():
        raise ValueError(f"package Oracle reference is missing: {package / 'solution/solve.sh'}")
    if tomllib.loads((package / "task.toml").read_text()).get("steps"):
        raise ValueError(f"native solution overrides do not support declared task steps: {task}")
    return digests


def prepare_candidate(
    port: dict[str, Any], *, root: Path, output: Path, port_base: Path,
    inputs_digest: str, heldout: frozenset[str], outputs: dict[Path, bytes],
) -> dict[str, Any]:
    task = task_id(port["task"])
    disposition = port["disposition"]
    if disposition not in {"full", "partial"}:
        raise ValueError(f"{task} has no retained full/partial disposition")
    remaining = port["old_remaining_measured"]
    if type(remaining) is not int or remaining < 0:
        raise ValueError(f"{task} has an invalid retained remaining-scope count")
    if not isinstance(port["training_exclusion"], str):
        raise ValueError(f"{task} training exclusion must retain its original string")
    destination = output / "candidates" / task
    sources: dict[str, Any] = {}
    payloads: dict[str, Any] = {}
    for key in ("record", "old_record", "old_receipt"):
        payloads[key], sources[key] = snapshot(
            source_path(port[key], base=port_base), destination / "sources" / f"{key}.json",
            outputs, port[f"{key}_sha256"],
        )
    record = VariantRecord.model_validate(payloads["record"])
    old_record = VariantRecord.model_validate(payloads["old_record"])
    old_receipt = payloads["old_receipt"]
    package = source_path(port["package"], base=port_base)
    parent = source_path(port["parent_package"], base=port_base)
    digests = validate_record(port, record, old_record, old_receipt, package=package,
                              parent=parent, root=root, port_base=port_base)
    identity = hashlib.sha256(json_bytes({
        "inputs_sha256": inputs_digest, "out": str(output), "port": port,
    })).hexdigest()
    prefix = f"e1017-{task[-6:]}-{identity[:12]}"
    runs = [
        MatrixRun(name=f"{prefix}-reference", agent="oracle", attempts=1,
                  expect_reward=1.0, allow_billable=False),
        MatrixRun(name=f"{prefix}-nop", agent="nop", attempts=1,
                  expect_reward=0.0, allow_billable=False),
    ]
    controls: list[dict[str, Any]] = [
        {"run": runs[0].name, "role": "reference", "expected_reward": 1.0,
         "required_verifier_observations": 3, "reference": read_source(package / "solution/solve.sh")[1]},
        {"run": runs[1].name, "role": "nop", "expected_reward": 0.0,
         "required_verifier_observations": 3},
    ]
    mutants = port["killed_mutants"]
    if not isinstance(mutants, list) or not mutants:
        raise ValueError(f"{task} has no retained killed wrong scripts")
    seen: set[str] = set()
    for index, mutant in enumerate(mutants, start=1):
        ident = mutant["id"]
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError(f"{task} has a missing or duplicate retained mutant ID: {ident!r}")
        seen.add(ident)
        script, reference = read_source(source_path(mutant["script"], base=port_base),
                                         mutant["script_sha256"])
        name = f"{prefix}-wrong-{index:02d}-{reference['sha256'][7:17]}"
        copied = destination / "scripts" / f"{name}.sh"
        outputs[copied] = script
        reference["snapshot"] = str(copied)
        runs.append(MatrixRun(name=name, agent="oracle", attempts=1, expect_reward=0.0,
                              allow_billable=False, solution=copied.relative_to(root).as_posix()))
        controls.append({"run": name, "role": "known-wrong", "retained_mutant_id": ident,
                         "expected_reward": 0.0, "required_verifier_observations": 1,
                         "script": reference})
    matrix = ExperimentMatrix(
        matrix_id=new_ulid(
            timestamp_ms=int(datetime(2026, 10, 17, tzinfo=UTC).timestamp() * 1000),
            randomness=int(identity[:20], 16),
        ),
        name=f"{prefix}-retained-port",
        hypothesis=(
            "Measure the exact retained v6 repair's package reference=1, nop=0 and "
            "listed retained wrong submissions=0 on native Modal. The inherited "
            f"{disposition} scope and all training exclusions remain unchanged."
        ),
        benchmark_family="mimo-clean-v6-repaired", task_id=task, task=str(package),
        task_package_digest=digests.package, verifier_digest=digests.verifier,
        environment="modal", jobs_dir="runs", concurrency=1, timeout_seconds=600, runs=runs,
    )
    matrix_path = destination / "matrix.json"
    matrix_bytes = json_bytes(matrix.model_dump(mode="json"))
    outputs[matrix_path] = matrix_bytes
    exclusions = []
    if port["training_exclusion"]:
        exclusions.append({"parent_id": record.task_name, "reason": port["training_exclusion"],
                           "source": "retained_port.training_exclusion"})
    if record.task_name in heldout:
        exclusions.append({"parent_id": record.task_name, "reason": "frozen-heldout-parent",
                           "source": "heldout.heldout"})
    return {
        "task": task, "parent_id": record.task_name, "status": "candidate", "qualified": False,
        "disposition": disposition, "variant_digest": record.variant_digest,
        "package_path": str(package), "verifier_digest": digests.verifier,
        "v6_parent_digest_of_variant": record.parent.digest, "parent_package": str(parent),
        "matrix": {"path": str(matrix_path), "sha256": sha256(matrix_bytes)},
        "source_custody": sources, "retained_port": port,
        "record_snapshot": sources["record"], "old_remaining_measured": remaining,
        "unadmitted_scope": {
            "ported_record": record.inputs.get("unrepairable", []),
            "retained_record": old_record.inputs.get("unrepairable", []),
            "retained_receipt": old_receipt.get("proposal", {}).get("unrepairable", []),
        },
        "controls": controls, "modal_observations": [],
        "missing_modal_controls": [control["run"] for control in controls],
        "training_allowed": False, "training_exclusion": port["training_exclusion"],
        "training_exclusions": exclusions,
        "training_holds": ["candidate-not-Modal-qualified", *[item["reason"] for item in exclusions]],
        "in_frozen_heldout_33": record.task_name in heldout,
    }


def publish(outputs: dict[Path, bytes], output: Path) -> None:
    # Preflight the entire batch before creating files. Never replace any bytes,
    # even when a prior invocation used the same output for a different subset.
    for path, data in outputs.items():
        if not path.is_relative_to(output):
            raise ValueError(f"planned output escaped --out: {path}")
        for ancestor in (path, *path.parents):
            if ancestor.is_symlink():
                raise ValueError(f"output symlink is not owned immutable storage: {ancestor}")
            if ancestor == output:
                break
        if path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise ValueError(f"refusing to overwrite different existing bytes: {path}; use a new --out")
    for path, data in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if not path.is_file() or path.read_bytes() != data:
                raise ValueError(f"output changed while preparing; not overwritten: {path}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port-plan", type=Path, required=True)
    parser.add_argument("--accepted-manifest", type=Path, required=True)
    parser.add_argument("--heldout", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--task", action="append", default=[], help="Repeatable parent task or six-digit suffix")
    args = parser.parse_args()
    try:
        root = args.root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("--root must be the owned worktree directory")
        runs = root / "runs"
        if runs.resolve() != runs:
            raise ValueError("the owned canonical runs/ directory must not be a symlink")
        output_arg = args.out.expanduser()
        output = (output_arg if output_arg.is_absolute() else root / output_arg).resolve()
        if output == runs or not output.is_relative_to(runs):
            raise ValueError("--out must be a dedicated directory below the owned worktree's runs/")
        outputs: dict[Path, bytes] = {}
        inputs: dict[str, Any] = {}
        documents: dict[str, Any] = {}
        for key, argument in (("port_plan", args.port_plan), ("accepted_manifest", args.accepted_manifest),
                              ("heldout", args.heldout)):
            documents[key], inputs[key] = snapshot(
                source_path(argument, base=root), output / "sources" / f"{key}.json", outputs,
            )
        ports_doc = documents["port_plan"]
        require_equal(ports_doc.get("schema"), PORT_SCHEMA, "source port-plan schema")
        ports = unique_tasks(ports_doc["ports"], "source port-plan")
        accepted_doc = documents["accepted_manifest"]
        accepted = unique_tasks(accepted_doc["variants"], "accepted manifest")
        require_equal(accepted_doc["count"], len(accepted), "accepted manifest count")
        heldout_doc = documents["heldout"]
        require_equal(heldout_doc.get("schema"), "mimo.heldout/v1", "frozen heldout schema")
        require_equal(heldout_doc.get("freeze_ready"), True, "heldout freeze readiness")
        heldout = frozenset(parent_id(task) for task in heldout_doc["heldout"])
        require_equal(len(heldout_doc["heldout"]), len(heldout), "unique heldout parent count")
        require_equal(len(heldout), 33, "frozen heldout parent count")
        expected_tasks = {"format-code-task-" + suffix for suffix in EXTRA_SUFFIXES}
        if expected_tasks - ports.keys():
            raise ValueError(f"source port-plan is missing assigned retained candidates: {sorted(expected_tasks - ports.keys())}")
        excluded = []
        candidates = {}
        for task, port in ports.items():
            if task in accepted:
                excluded.append({"task": task, "reason": "accepted-manifest", "accepted_variant": accepted[task]})
            elif task in ALREADY_PREPARED:
                excluded.append({"task": task, "reason": "already-prepared-Modal-qualified; do not regenerate"})
            elif task not in expected_tasks:
                raise ValueError(f"unaccepted source port is outside the assigned candidate set: {task}")
            else:
                candidates[task] = port
        selected = {task_id(task) for task in args.task} if args.task else set(candidates)
        if selected - candidates.keys():
            raise ValueError(f"--task includes noncandidate, accepted or already-prepared parents: {sorted(selected - candidates.keys())}")
        inputs_digest = sha256(json_bytes(inputs))
        rows = [prepare_candidate(candidates[task], root=root, output=output,
                                  port_base=Path(inputs["port_plan"]["path"]).parent,
                                  inputs_digest=inputs_digest, heldout=heldout, outputs=outputs)
                for task in sorted(selected)]
        names = [control["run"] for row in rows for control in row["controls"]]
        if len(names) != len(set(names)) or any(SAFE_JOB_NAME.fullmatch(name) is None for name in names):
            raise ValueError("generated native job names must be unique and bounded to 3-80 safe characters")
        retained_exclusions = [
            {"parent_id": parent_id(task), "reason": port["training_exclusion"]}
            for task, port in ports.items() if port.get("training_exclusion")
        ]
        summary = {
            "accepted_manifest_variants": len(accepted), "source_ports": len(ports),
            "excluded_accepted_ports": sum(item["reason"] == "accepted-manifest" for item in excluded),
            "excluded_already_prepared_ports": sum(item["task"] in ALREADY_PREPARED for item in excluded),
            "available_candidates": len(candidates), "prepared_candidates": len(rows),
            "full_candidates": sum(row["disposition"] == "full" for row in rows),
            "partial_candidates": sum(row["disposition"] == "partial" for row in rows),
            "planned_controls": len(names), "missing_modal_controls": len(names),
            "training_held_candidates": len(rows),
            "retained_training_exclusion_candidates": sum(bool(row["training_exclusion"]) for row in rows),
            "frozen_heldout_candidates": sum(row["in_frozen_heldout_33"] for row in rows),
            "new_qualified_repairs": 0, "executions": 0, "spend_usd": 0,
        }
        manifest_path = output / "candidate-manifest.json"
        outputs[manifest_path] = json_bytes({
            "schema": SCHEMA, "notice": NOTICE, "status": "candidate", "qualified": False,
            "source_custody": inputs, "summary": summary, "candidates": rows, "excluded": excluded,
            "unselected_candidate_parents": sorted(set(candidates) - selected),
            "training_policy": {
                "exclude_by": "parent_id", "variant_renaming_cannot_bypass_exclusions": True,
                "frozen_heldout_parent_ids": sorted(heldout),
                "retained_port_exclusions": retained_exclusions,
                "training_exclusion_parent_ids": sorted(heldout | {
                    item["parent_id"] for item in retained_exclusions
                }),
                "unqualified_candidates_training_allowed": False,
            },
        })
        publish(outputs, output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"manifest": str(manifest_path), "summary": summary,
                      "matrices": [row["matrix"]["path"] for row in rows]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
