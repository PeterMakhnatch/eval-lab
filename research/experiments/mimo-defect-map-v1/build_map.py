"""Reproduce a metadata-only MiMo source map; never execute a benchmark task.

Committed source blobs are authoritative. Local packages are read only to bind
inventory identities. Known findings never become matcher inputs. Missing match
measurements are dispositions, not negative predictions or zero recall.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REQUIRED_CATEGORIES = (
    "atlas114",
    "initial_v7",
    "batch2_v7",
    "round2",
    "round3",
    "round4",
    "round5",
    "round6",
    "training_wrong_pass8",
    "vals_answer_leak",
)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()


def task_id(value: str) -> str:
    value = value.removeprefix("mimo-v2.6-rl/")
    return "format-code-task-" + value if re.fullmatch(r"\d{6}", value) else value


def ref(source_id: str, pointer: str = "") -> dict[str, str]:
    return {"source_id": source_id, "json_pointer": pointer}


def at(document: Any, pointer: str) -> Any:
    for component in pointer.lstrip("/").split("/") if pointer else ():
        component = component.replace("~1", "/").replace("~0", "~")
        document = document[int(component)] if isinstance(document, list) else document[component]
    return document


def load_sources(manifest: dict[str, Any], roots: dict[str, Path]) -> dict[str, Any]:
    """Validate frozen Git/capsule bytes or explicitly authorized local evidence."""
    documents: dict[str, Any] = {}
    blobs: dict[tuple[str, str | None, str], bytes] = {}
    archives: dict[tuple[str, str | None, str], tarfile.TarFile] = {}
    with ExitStack() as stack:
        for source in manifest["sources"]:
            name = source["source_id"]
            require(name not in documents, f"duplicate source_id: {name}")
            revision = source["revision"]
            local_file = source.get("kind") == "authorized-file"
            require(
                (local_file and revision is None and source["access"] == "authorized-local")
                or (
                    not local_file
                    and isinstance(revision, str)
                    and bool(re.fullmatch(r"[0-9a-f]{40}", revision))
                ),
                f"source revision is not full or authorized-local: {name}",
            )
            require(
                source["access"] in {"public", "private-repository", "authorized-local"},
                f"source access scope absent: {name}",
            )
            key = (source["repository"], revision, source["path"])
            if key not in blobs:
                if local_file:
                    relative = Path(source["path"])
                    require(
                        not relative.is_absolute() and ".." not in relative.parts,
                        f"authorized source must remain inside its named evidence root: {name}",
                    )
                    blobs[key] = (roots[source["repository"]] / relative).read_bytes()
                else:
                    blobs[key] = subprocess.check_output(
                        ["git", "show", f"{revision}:{source['path']}"],
                        cwd=roots[source["repository"]],
                    )
            material = blobs[key]
            if source.get("member"):
                container = source["container"]
                require(
                    digest(material) == container["sha256"] and len(material) == container["bytes"],
                    f"source container bytes changed: {name}",
                )
                if key not in archives:
                    archives[key] = stack.enter_context(
                        tarfile.open(fileobj=io.BytesIO(material), mode="r:gz")
                    )
                member = archives[key].extractfile(source["member"])
                require(member is not None, f"source member is not a regular file: {name}")
                with member:
                    material = member.read()
            require(
                digest(material) == source["sha256"] and len(material) == source["bytes"],
                f"source bytes changed: {name}",
            )
            suffix = Path(source.get("member", source["path"])).suffix
            if suffix == ".jsonl":
                documents[name] = [
                    json.loads(line) for line in material.splitlines() if line.strip()
                ]
            elif suffix == ".json":
                documents[name] = json.loads(material)
            elif suffix in {".yaml", ".yml"}:
                documents[name] = yaml.safe_load(material)
            else:
                documents[name] = material.decode()
            for first, last in source.get("source_line_ranges", ()):
                require(
                    isinstance(documents[name], str)
                    and 1 <= first <= last <= len(documents[name].splitlines()),
                    f"cited source line range is outside the frozen text: {name}",
                )
    return documents


def taxonomy_classes(document: dict[str, Any]) -> dict[str, dict[str, str]]:
    classes = {row["key"]: {"id": row["id"], "key": row["key"]} for row in document["task_side"]}
    require(len(classes) == len(document["task_side"]), "taxonomy has duplicate task-side keys")
    return classes


def inventory(
    atlas: list[dict[str, Any]], heldout: list[str], package_root: Path | None
) -> dict[str, Any]:
    from evallab.registry import compute_task_digests, harbor_task_digest

    ids = [task_id(row["task_id"]) for row in atlas]
    require(
        len(ids) == len(set(ids)) == 567,
        "the frozen full fleet must contain exactly 567 unique IDs",
    )
    require(
        len(heldout) == len(set(heldout)) == 33,
        "the training exclusion must contain all frozen 33 IDs",
    )
    rows = []
    for index, row in enumerate(atlas):
        name, expected = ids[index], row["package_digest"]
        require(bool(DIGEST.fullmatch(expected)), f"atlas lacks an exact package identity: {name}")
        hint = row["source_binding"]["package_path"]
        path = (package_root / name) if package_root else Path(hint).expanduser()
        observation: dict[str, Any] = {
            "state": "missing_package",
            "locator": str(path),
            "resolved_locator": None,
            "error_type": None,
            "observed_package_digest": None,
        }
        harbor = verifier = None
        if path.is_dir():
            try:
                material = compute_task_digests(path)
                harbor, verifier = harbor_task_digest(path.resolve()), material.verifier
                observation.update(
                    state="verified_exact_package"
                    if material.package == expected
                    else "package_mismatch",
                    resolved_locator=str(path.resolve()),
                    observed_package_digest=material.package,
                    task_toml_digest=material.task_toml,
                    instruction_digest=material.instruction,
                    environment_digest=material.environment,
                )
            except (OSError, ValueError) as error:
                observation.update(state="inventory_error", error_type=type(error).__name__)
        rows.append(
            {
                "task_id": name,
                "record_id": name + "@" + expected,
                "package_digest": expected,
                "harbor_digest": harbor,
                "verifier_digest": verifier,
                "selection_index": row["selection_index"],
                "selection_scope": "frozen_atlas567_historical_packages",
                "source": ref("atlas", f"/{index}"),
                "materialization": observation,
                "historical_semantic_verdict": row["semantic_verdict"],
                "historical_unknown_reasons": row["unknowns"],
                "heldout_excluded_from_training_all_versions": name in heldout,
            }
        )
    return {
        "schema": "mimo.defect-map.inventory/v1",
        "dataset_id": "mimo-defect-map-v1",
        "selection": "All 567 exact historical atlas package selections, not a latest-version alias",
        "counts": dict(sorted(Counter(row["materialization"]["state"] for row in rows).items())),
        "heldout_task_ids_excluded_from_training_all_versions": heldout,
        "tasks": rows,
    }


def atlas_findings(
    documents: dict[str, Any], classes: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    """Retain each archived witness and its original qualification; do not replay it."""
    items = {
        task_id(row["subject"]["task_id"]): (index, row)
        for index, row in enumerate(documents["atlas_items"])
    }
    original = {row["id"]: (index, row) for index, row in enumerate(documents["atlas_findings"])}
    require(len(items) == 114, "atlas supported task inventory changed")
    result = []
    for index, row in enumerate(documents["atlas"]):
        name = task_id(row["task_id"])
        if name not in items:
            require(not row["witnesses"], f"supported witness missing task label: {name}")
            continue
        item_index, item = items[name]
        require(
            row["package_digest"] == item["subject"]["task_digest"], "atlas item/package mismatch"
        )
        for witness_index, witness in enumerate(row["witnesses"]):
            source_id = f"mimo-weak-tests-v2.1:{name}:{witness['id']}"
            finding_index, source = original[source_id]
            require(
                source["subject"]["task_digest"] == row["package_digest"],
                "atlas finding/package mismatch",
            )
            inherited_reference_defect = name in {
                "format-code-task-000030",
                "format-code-task-000627",
            }
            primary_key = "wrong_answer_key" if inherited_reference_defect else "weak_check"
            controls = witness["controls"]
            result.append(
                {
                    "finding_id": source_id,
                    "source_finding_id": source["id"],
                    "category": "atlas114",
                    "task_id": name,
                    "package_digest": row["package_digest"],
                    "harbor_digest": None,
                    "verifier_digest": None,
                    "primary_class": classes[primary_key],
                    "secondary_classes": [],
                    "run_side_class": None,
                    "class_mapping_status": "INFERENCE",
                    "direction": "false_pass",
                    "evidence_status": "source_supported_historical",
                    "source_qualification": {
                        "task_label_strength": item["label_strength"],
                        "semantic_verdict": row["semantic_verdict"],
                        "witness_review_label": witness["review_label"],
                        "measurement": source["measurement"],
                        "source_deduction_marker_present": "INFERENCE" in witness["reason"],
                        "not_fresh_replay": True,
                        "not_prompt_bound_wrongness_for_every_survivor": True,
                    },
                    "program": {
                        "source_program_id": source_id,
                        "sha256": "sha256:" + witness["sha256"],
                        "kind": witness["kind"],
                    },
                    "executions": [
                        {
                            "execution_id": None,
                            "source_execution_identity": {
                                "report": row["primary"]["report"],
                                "grade_line": grade.get("line"),
                                "role": role,
                            },
                            "reward": grade.get("reward"),
                            "scored": grade.get("status") == "scored",
                            "returncode": grade.get("rc"),
                        }
                        for role, grade in controls.items()
                    ],
                    "differential": {
                        "confirmed": False,
                        "original_reward": controls["witness"]["reward"],
                        "fixed_reward": None,
                        "reason": "no bound original1/fixed0 replay in atlas source",
                    },
                    "resolution": {
                        "status": "historical_parent_finding",
                        "transfers_to_descendants": False,
                    },
                    "uncertainty": row["unknowns"]
                    + (
                        [
                            "The retained witness also fails the task requirement in the full reference. TS08 reference-solution precedence is an inferred mapping; do not interpret this as a dropped-line repair or independent secondary-class adjudication."
                        ]
                        if inherited_reference_defect
                        else []
                    ),
                    "safe_summary": "Archived instruction-adjudicated wrong-patch witness; taxonomy mapping is inference, not fresh differential confirmation.",
                    "evidence": [
                        ref("atlas", f"/{index}/witnesses/{witness_index}"),
                        ref("atlas_items", f"/{item_index}"),
                        ref("atlas_findings", f"/{finding_index}"),
                    ],
                }
            )
    require(len(result) == len(original) == 242, "atlas must preserve all 242 distinct witnesses")
    return result


def initial_history(
    documents: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Keep the initial repair parents, diagnostics and selected control scopes."""
    findings: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    controls = []
    for index, row in enumerate(documents["clean_v7"]["tasks"]):
        if row["status"] == "known-weak":
            continue
        measured = row["controls"]
        controls.append(
            {
                "task_id": row["task_id"],
                "package_digest": row["package_digest"],
                "harbor_digest": row["harbor_digest"],
                "verifier_digest": row["verifier_digest"],
                "source_status": row["status"],
                "case_bound_reference_nop_qualified": measured.get(
                    "case_bound_expected_pair_at_requested_snapshot"
                )
                is True,
                "strict_reference_nop_pair": measured.get("strict_reference_nop_pair"),
                "reference": {
                    key: measured.get("reference", {}).get(key)
                    for key in (
                        "rewards",
                        "primary_rewards",
                        "status",
                        "run_id",
                        "native_execution_id",
                    )
                },
                "nop": {
                    key: measured.get("nop", {}).get(key)
                    for key in (
                        "rewards",
                        "primary_rewards",
                        "status",
                        "run_id",
                        "native_execution_id",
                    )
                },
                "observations": [
                    {
                        "role": observation["role"],
                        "observation": {
                            key: observation["observation"].get(key)
                            for key in (
                                "source_package_digest",
                                "source_harbor_digest",
                                "executed_harbor_digest",
                                "execution_id",
                                "result_sha256",
                                "metadata_sha256",
                                "native_identity_verified",
                                "scored",
                                "process_ok",
                                "process_failed",
                                "timed_out",
                                "exception_present",
                                "rewards",
                                "repeat_scored",
                                "test_execution",
                                "run_error_present",
                            )
                        },
                    }
                    for observation in measured.get("observations", [])
                ],
                "scope": "Exact historical selected-package controls from the pinned prior metadata producer; not semantic cleanliness or a new execution.",
                "evidence": [ref("clean_v7", f"/tasks/{index}/controls")],
            }
        )
    inputs = (
        (
            "heldout",
            "/semantic_repairs/handoff/repairs",
            documents["heldout"]["semantic_repairs"]["handoff"]["repairs"],
        ),
        ("train_repairs", "/repairs", documents["train_repairs"]["repairs"]),
    )
    for source_id, prefix, source_repairs in inputs:
        for index, repair in enumerate(source_repairs):
            name = task_id(repair["task_id"])
            pointer = f"{prefix}/{index}"
            selected_source = next(
                row
                for row in documents["clean_v7"]["tasks"]
                if row["task_id"] == name and row["package_digest"] == repair["package_digest"]
            )
            repairs.append(
                {
                    "repair_id": f"initial-v7:{name}:{repair['package_digest']}",
                    "category": "initial_v7",
                    "task_id": name,
                    "parent_package_digest": repair["parent_digest"],
                    "frozen_parent_package_digest": repair.get("frozen_parent_digest"),
                    "child_package_digest": repair["package_digest"],
                    "child_harbor_digest": repair["harbor_digest"],
                    "child_verifier_digest": repair["verifier_digest"],
                    "native_record_sha256": repair["native_record_sha256"],
                    "qualification_sha256": repair["qualification_sha256"],
                    "source_qualification": {
                        "status": repair["status"],
                        "qualified_for_v7": repair["qualified_for_v7"],
                        "reference": repair["reference"],
                        "nop": repair["do_nothing"],
                        "no_original_assertion_weakened": repair["no_original_assertion_weakened"],
                        "task_control_bindings_unchanged": repair[
                            "task_control_bindings_unchanged"
                        ],
                        "source_inputs_unchanged": repair["source_inputs_unchanged"],
                    },
                    "selected_package_status": selected_source["status"],
                    "expanded_case_scope": selected_source.get("current_case_coverage"),
                    "historical_label_transferred_to_child": False,
                    "supplied_correct_alternates": [
                        {
                            key: case[key]
                            for key in ("finding_id", "patch_sha256", "script_sha256", "run")
                            if key in case
                        }
                        for case in repair.get("adjudicated_correct_controls", [])
                    ],
                    "correct_alternate_results": [
                        {
                            key: case[key]
                            for key in (
                                "finding_id",
                                "patch_sha256",
                                "repeats",
                                "rewards",
                                "status",
                                "accepted",
                                "run",
                            )
                            if key in case
                        }
                        for case in repair.get("adjudicated_correct_results", [])
                    ],
                    "evidence": [ref(source_id, pointer)],
                }
            )
            for case_index, case in enumerate(repair["reproducers"]):
                diagnostic = (
                    case.get("counts_as_certified_wrong_kill") is False
                    or case.get("current_semantic_verdict") == "unspecified"
                )
                findings.append(
                    {
                        "finding_id": f"initial-v7:{source_id}:{case['finding_id']}",
                        "source_finding_id": case["finding_id"],
                        "category": "initial_v7",
                        "task_id": name,
                        "package_digest": repair["parent_digest"],
                        "package_binding_basis": "Explicit repair parent; original native execution identity is not recovered here.",
                        "primary_class_key": None if diagnostic else "weak_check",
                        "primary_class": None,
                        "secondary_classes": [],
                        "run_class": None,
                        "class_mapping_status": "not_a_supported_defect"
                        if diagnostic
                        else "INFERENCE",
                        "direction": None if diagnostic else "false_pass",
                        "evidence_status": "source_optional_diagnostic"
                        if diagnostic
                        else "source_supported_historical",
                        "source_qualification": {
                            "current_semantic_verdict": case.get("current_semantic_verdict"),
                            "counts_as_certified_wrong_kill": case.get(
                                "counts_as_certified_wrong_kill"
                            ),
                            "killed": case["killed"],
                            "status": case["status"],
                            "independently_fresh_confirmed": False,
                        },
                        "program": {
                            "source_program_id": case["finding_id"],
                            "sha256": case["patch_sha256"],
                            "replay_script_sha256": case.get("script_sha256"),
                        },
                        "executions": [
                            {
                                "role": "repaired_wrong_or_diagnostic",
                                "execution_id": None,
                                "source_execution_identity": case["run"],
                                "source_package_digest": repair["package_digest"],
                                "rewards": case["rewards"],
                                "repeats": case.get("repeats", []),
                            }
                        ],
                        "differential": {
                            "confirmed": False,
                            "original_reward": None,
                            "fixed_reward": case["rewards"][0]
                            if len(case["rewards"]) == 1
                            else None,
                            "reason": "Repaired-side results and parent lineage are retained; this projection has not established a same-program original1/fixed0 native custody pair.",
                        },
                        "resolution": {
                            "status": "optional_diagnostic_not_a_wrong_label"
                            if diagnostic
                            else "child_repair_source_reported",
                            "child_package_digest": repair["package_digest"],
                            "historical_label_transferred_to_child": False,
                        },
                        "uncertainty": [
                            "Killing a source-adjudicated case is scoped evidence, not proof that every mutant is wrong or that all future assertions are covered.",
                            "An explicit unspecified/optional diagnostic is not a known weak label, even when reward is zero.",
                        ],
                        "safe_summary": "Initial v7 archival repair case; exact parent and repaired-side program/result identities are separate.",
                        "evidence": [ref(source_id, f"{pointer}/reproducers/{case_index}")],
                    }
                )
    require(len(repairs) == 21 and len(findings) == 35, "initial repair/case inventory changed")
    return (
        findings,
        repairs,
        controls,
        {
            "status": "pinned_initial_source_cases_and_control_scopes",
            "selected_heldout_semantic_repairs": 12,
            "selected_train_semantic_repairs": 9,
            "physical_reproducer_rows": 35,
            "source_supported_wrong_cases": 34,
            "optional_diagnostics": 1,
            "source_heldout_authoritative_weak_ids": 11,
            "expanded_scope_blocked_train_ids": [
                row["task_id"]
                for row in documents["clean_v7"]["tasks"]
                if row.get("current_case_coverage", {}).get(
                    "additional_required_wrong_cases_unmeasured", 0
                )
            ],
            "semantic_clean_count": 0,
        },
    )


TIMING_TASK = "format-code-task-000657"


def morning_review(documents: dict[str, Any]) -> dict[str, Any]:
    """Extract the cited independent verdicts. A round absent from the cited ranges is not checked."""
    lines = documents["morning_review"].splitlines()
    track, claims = lines[11:40], lines[97:121]

    def cells(rows: list[str], prefix: str) -> list[str]:
        matches = [line for line in rows if line.startswith(prefix)]
        require(len(matches) == 1, f"independent review row changed: {prefix}")
        return [cell.strip() for cell in matches[0].strip("|").split("|")]

    rounds = {
        f"round{number}": cells(track, prefix)
        for number, prefix in (
            (2, "| 2 (attack"),
            (3, "| 3 (attack"),
            (4, "| 4 (attack"),
            (5, "| 5 (GLM"),
        )
    }
    verdicts = {name: row[-1] for name, row in rounds.items()}
    require(
        verdicts == {"round2": "PASS", "round3": "PASS", "round4": "PASS", "round5": "UNVERIFIED"},
        "independent review round verdicts changed",
    )
    require(
        "in progress" in rounds["round5"][1] and "07:29 ET" in rounds["round5"][1],
        "round-5 independent review is no longer an in-progress observation",
    )
    require(
        not any(re.match(r"\| 6\b", line) or "round 6" in line.lower() for line in track),
        "an independent round-6 review row appeared; consume it explicitly",
    )
    good_data = cells(claims, "| 3 | Good data")
    require(
        good_data[-1].startswith("PARTIAL")
        and "3 of the 6 earlier fails not located" in good_data[-1],
        "independent training-pool review scope changed",
    )
    return {
        "source_id": "morning_review",
        "line_ranges": [[12, 40], [98, 121]],
        "created_et": "2026-10-19 07:30",
        "round_verdicts": {
            "round2": {
                "verdict": "PASS",
                "scope": "wrong-program counts, append-only repairs, qualification reward files; 001248 adjudicated with a docstring-based caveat",
            },
            "round3": {
                "verdict": "PASS",
                "scope": "wrong-program count and qualification reward files",
            },
            "round4": {
                "verdict": "PASS",
                "scope": "zero wrong programs at one task and K<=3; measures that budget, not exhaustion",
            },
            "round5": {
                "verdict": "UNVERIFIED",
                "scope": "round in progress at 07:29 ET with no repairs file at review time; not independently checked",
            },
            "round6": {"verdict": "NOT_CHECKED", "scope": "absent from the cited review ranges"},
        },
        "training_pool_review": {
            "verdict": "PARTIAL",
            "recomputed": "14 repaired-grader rows, 70 pool rows and tonight's 2/7",
            "not_located": "evidence for 000169 x2 and 002486 772a78ad in repairs-train-v7",
        },
    }


def failed_reference_node(log: str) -> dict[str, Any]:
    """Extract only the failed node id and assertion shape; no log or test text is exported."""
    failed = re.findall(r"^FAILED (\S+) - ", log, re.MULTILINE)
    require(len(failed) == 1, "timing reference log must name exactly one failed node")
    return {
        "failed_node_id": failed[0],
        "elapsed_lower_bound_assertion": bool(
            re.search(r"AssertionError: [0-9.]+ not greater than [0-9.]+", log)
        ),
    }


def timing_findings(documents: dict[str, Any]) -> list[dict[str, Any]]:
    """Separate TS09 parent/child findings; failed reference controls are never closed here."""
    repair_document = documents["round6_repairs"]
    repair_index, repair = next(
        (index, row)
        for index, row in enumerate(repair_document["repairs"])
        if row["task_id"] == TIMING_TASK
    )
    parent, record = documents["round6_timing_parent"], documents["round6_timing_lineage"]["record"]
    child = repair["repaired"]["package_digest"]
    require(
        parent["package_digest"] == repair["parent"]["package_digest"] == record["parent"]["digest"]
        and record["variant_digest"] == child
        and parent["complete"] is True
        and parent["qualified"] is False
        and repair["qualification"]["qualified"] is False,
        "round-6 timing parent/child lineage or qualification changed",
    )

    def rewards(rows: list[dict[str, Any]], role: str) -> list[float]:
        return [row["reward"] for row in rows if row["role"] == role]

    parent_rows, child_rows = parent["rows"], repair["qualification"]["cells"]
    require(
        rewards(parent_rows, "oracle") == [1.0, 1.0, 0.0]
        and rewards(parent_rows, "nop") == [0.0]
        and rewards(child_rows, "oracle") == [1.0, 1.0, 0.0]
        and rewards(child_rows, "nop") == [0.0]
        and repair["qualification"]["reference_pass"] == "2/3"
        and repair["qualification"]["wrong_rejected"] == "5/5",
        "round-6 timing control rewards changed",
    )
    nodes = {
        "parent": failed_reference_node(documents["round6_timing_parent_failed_reference_log"]),
        "child": failed_reference_node(documents["round6_timing_child_failed_reference_log"]),
    }
    added_module = repair["change"]["test_repo_path"]
    require(
        nodes["parent"] == nodes["child"]
        and nodes["parent"]["elapsed_lower_bound_assertion"]
        and not nodes["parent"]["failed_node_id"].startswith(added_module),
        "the failed reference node is no longer one original-module timing assertion shared by both sides",
    )

    def finding(
        side: str,
        package: str,
        harbor: str,
        verifier: str | None,
        rows: list[dict[str, Any]],
        evidence: list[dict[str, str]],
        resolution: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "finding_id": f"hardening:round6:{TIMING_TASK}:{side}-reference-timing-flake",
            "source_finding_id": None,
            "category": "round6",
            "task_id": TIMING_TASK,
            "package_digest": package,
            "harbor_digest": harbor,
            "verifier_digest": verifier,
            "primary_class_key": "env_mismatch_or_flaky",
            "primary_class": None,
            "secondary_classes": [],
            "run_class": None,
            "class_mapping_status": "INFERENCE",
            "direction": "false_fail",
            "evidence_status": "source_supported_historical",
            "source_qualification": {
                "reference_rewards": rewards(rows, "oracle"),
                "nop_rewards": rewards(rows, "nop"),
                "source_qualified": False,
                "failed_original_node": nodes[side],
                "failed_node_is_original_module_not_added_test": True,
                **extra,
            },
            "program": None,
            "executions": [
                {
                    "role": row["role"],
                    "label": row["label"],
                    "execution_id": None,
                    "source_execution_identity": row["trial"],
                    "reward": row["reward"],
                    "expected": row["expected"],
                    "source_package_digest": package,
                }
                for row in rows
            ],
            "differential": {
                "confirmed": False,
                "original_reward": None,
                "fixed_reward": None,
                "reason": "A failed-reference false-fail control, not an exploit; no original1/fixed0 differential applies.",
            },
            "resolution": {
                "status": resolution,
                "child_package_digest": child,
                "historical_label_transferred_to_child": False,
            },
            "uncertainty": [
                "The same original-module elapsed-time assertion fails one of three reference runs on both parent and child, so the added hardening assertion neither introduced nor removed it. The failed controls remain open; this is not a semantic qualification of the child.",
                "[INFERENCE] Timing/load dependence; the retained log states a measured elapsed value below a lower bound but not the source of variance.",
            ],
            "safe_summary": "Reference solution fails the original module's timing assertion in one of three runs; no log text or test body is exported.",
            "evidence": evidence,
        }

    return [
        finding(
            "parent",
            parent["package_digest"],
            record["parent"]["harbor_digest"],
            None,
            parent_rows,
            [ref("round6_timing_parent"), ref("round6_timing_parent_failed_reference_log")],
            "unresolved_failed_reference_control",
            {},
        ),
        finding(
            "child",
            child,
            record["variant_harbor_digest"],
            repair["repaired"]["verifier_digest"],
            child_rows,
            [
                ref("round6_repairs", f"/repairs/{repair_index}/qualification"),
                ref("round6_timing_lineage", "/record"),
                ref("round6_timing_child_failed_reference_log"),
            ],
            "unresolved_failed_reference_control_after_repair",
            {
                "wrong_programs_rejected": repair["qualification"]["wrong_rejected"],
                "reference_passes": repair["qualification"]["reference_pass"],
            },
        ),
    ]


def round_history(
    documents: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Project retained rounds without publishing program/test contents."""
    findings: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    coverage = {}
    review = morning_review(documents)
    review_rounds = {
        name for name, row in review["round_verdicts"].items() if row["verdict"] != "NOT_CHECKED"
    }
    for number in range(2, 7):
        supported_programs = 0
        program_source, repair_source = f"round{number}_hardening", f"round{number}_repairs"
        programs, repair_document = documents[program_source], documents[repair_source]
        category = f"round{number}"
        require(
            programs["status"] == repair_document["status"] == "complete",
            f"retained {category} source is incomplete",
        )
        for index, repair in enumerate(repair_document["repairs"]):
            qualification = repair["qualification"]
            repairs.append(
                {
                    "repair_id": f"hardening:{category}:{repair['task_id']}:{repair['repaired']['package_digest']}",
                    "category": category,
                    "task_id": repair["task_id"],
                    "parent_package_digest": repair["parent"]["package_digest"],
                    "child_package_digest": repair["repaired"]["package_digest"],
                    "child_verifier_digest": repair["repaired"]["verifier_digest"],
                    "lineage_record": repair["repaired"]["lineage_record"],
                    "historical_label_transferred_to_child": False,
                    "source_qualification": {
                        key: qualification[key]
                        for key in (
                            "complete",
                            "qualified",
                            "reference_pass",
                            "nop_fail",
                            "wrong_rejected",
                            "correct_accepted",
                        )
                    },
                    "qualification_cells": [
                        {
                            key: cell[key]
                            for key in ("role", "label", "reward", "expected", "ok", "trial")
                        }
                        for cell in qualification["cells"]
                    ],
                    "qualification_scope": "Archival author-side native qualification; not independent Verifier acceptance or new replay.",
                    "interpretation_note_present": bool(
                        repair["change"].get("interpretation_note")
                    ),
                    "assertion_change_sha256": repair["change"].get("test_sha256"),
                    "evidence": [ref(repair_source, f"/repairs/{index}")],
                }
            )
        for index, program in enumerate(programs["programs"]):
            native, probe = program["native"], program.get("independent_probe") or {}
            verdict = program["independent_incorrectness"]
            supported = (
                verdict == "verified"
                and probe.get("verified_wrong") is True
                and native.get("fresh_s3_reward") == 1
            )
            matching_repairs = [
                repair
                for repair in repair_document["repairs"]
                if repair["task_id"] == program["task_id"]
                and repair["parent"]["package_digest"] == program["package_digest"]
            ]
            fixed_cells = [
                cell
                for repair in matching_repairs
                for cell in repair["qualification"]["cells"]
                if cell["role"] == "wrong" and cell["label"] == program["label"]
            ]
            uncertainty = []
            if number == 2 and program["task_id"] == "format-code-task-001248":
                uncertainty.append(
                    "Preservation reading depends on docstring/API behaviour, not a literal issue requirement; retain interpretation dependence."
                )
            if number == 6 and program["task_id"] == "format-code-task-000371":
                uncertainty.append(
                    "Data-scope requirement is contested; author-side incorrectness is not independent release acceptance."
                )
            if number >= 5:
                uncertainty.append(
                    "Independent morning review: "
                    + review["round_verdicts"][category]["verdict"]
                    + " ("
                    + review["round_verdicts"][category]["scope"]
                    + "); no independent qualification of this round."
                )
            supported_programs += supported
            findings.append(
                {
                    "finding_id": f"hardening:{category}:{program['task_id']}:{program['label']}:{program['sha256']}",
                    "category": category,
                    "task_id": program["task_id"],
                    "package_digest": program["package_digest"],
                    "harbor_digest": None,
                    "verifier_digest": None,
                    "primary_class_key": "weak_check" if supported else None,
                    "primary_class": None,
                    "secondary_classes": [],
                    "run_class": None,
                    "class_mapping_status": "INFERENCE" if supported else "not_a_supported_defect",
                    "direction": "false_pass" if supported else None,
                    "evidence_status": "source_supported_historical"
                    if supported
                    else "source_refuted_or_unverified",
                    "source_qualification": {
                        "independent_incorrectness": verdict,
                        "author_probe_verified_wrong": probe.get("verified_wrong"),
                        "author_probe_not_independent_verifier_release": True,
                        "independent_review": review["round_verdicts"][category]["verdict"],
                    },
                    "program": {
                        "source_id": program["label"],
                        "kind": program["kind"],
                        "sha256": "sha256:" + program["sha256"],
                    },
                    "executions": [
                        {
                            "role": role,
                            "execution_id": None,
                            "source_execution_identity": native[trial],
                            "reward": native[reward],
                            "source_package_digest": program["package_digest"],
                        }
                        for role, trial, reward in (
                            ("s2a", "s2a_trial", "s2a_reward"),
                            ("fresh_s3", "s3_trial", "fresh_s3_reward"),
                        )
                    ]
                    + [
                        {
                            "role": "repaired_wrong",
                            "execution_id": None,
                            "source_execution_identity": cell["trial"],
                            "reward": cell["reward"],
                            "source_package_digest": matching_repairs[0]["repaired"][
                                "package_digest"
                            ],
                        }
                        for cell in fixed_cells
                    ],
                    "differential": {
                        "confirmed": False,
                        "original_reward": native.get("fresh_s3_reward"),
                        "fixed_reward": fixed_cells[0]["reward"] if len(fixed_cells) == 1 else None,
                        "source_reported_original1_fixed0": bool(
                            supported and len(fixed_cells) == 1 and fixed_cells[0]["reward"] == 0
                        ),
                        "reason": "Archive reports reward flips; full native execution/program/package custody is not independently established by these bridge summaries.",
                    },
                    "resolution": {
                        "status": "child_repair_source_reported"
                        if matching_repairs
                        else "no_retained_repair",
                        "child_package_digests": [
                            repair["repaired"]["package_digest"] for repair in matching_repairs
                        ],
                        "historical_label_transferred_to_child": False,
                    },
                    "uncertainty": uncertainty,
                    "safe_summary": "Archival wrong-patch observation with source adjudication retained; no raw patch or requirement test is exported.",
                    "evidence": [ref(program_source, f"/programs/{index}")]
                    + ([ref("morning_review")] if category in review_rounds else []),
                }
            )
            if verdict == "withdrawn_overtightening":
                findings.append(
                    {
                        "finding_id": f"hardening:{category}:{program['task_id']}:withdrawn-repair-overtightening",
                        "category": category,
                        "task_id": program["task_id"],
                        "package_digest": None,
                        "primary_class_key": "unstated_requirement",
                        "secondary_classes": [],
                        "run_class": None,
                        "class_mapping_status": "INFERENCE",
                        "direction": "false_fail",
                        "evidence_status": "source_supported_historical",
                        "program": {
                            "source_id": program["label"],
                            "sha256": "sha256:" + program["sha256"],
                        },
                        "differential": {"confirmed": False},
                        "resolution": {"status": "withdrawn_repair_not_selected"},
                        "uncertainty": [
                            "The false-fail concerns the withdrawn repaired grader, not the passing program's parent package. That child digest is not supplied in this retained row; no parent digest is substituted."
                        ],
                        "safe_summary": "Source withdrew a maxsplit requirement after a canonical-pass rejection; retain as over-tightening, not a weak-check positive.",
                        "evidence": [ref(program_source, f"/programs/{index}/parent_disposition")],
                    }
                )
        if number == 6:
            timing = timing_findings(documents)
            findings += timing
            for repair_row in repairs:
                if repair_row["category"] == category and repair_row["task_id"] == TIMING_TASK:
                    repair_row["control_failure_finding_ids"] = [
                        row["finding_id"] for row in timing
                    ]
        coverage[category] = {
            "status": "retained_complete_round_with_zero_programs"
            if not programs["programs"]
            else "retained_complete_round",
            "source_program_count": len(programs["programs"]),
            "source_independent_incorrectness_verdicts": dict(
                sorted(
                    Counter(
                        program["independent_incorrectness"] for program in programs["programs"]
                    ).items()
                )
            ),
            "source_verified_wrong_program_count": programs["verified_wrong_programs"],
            "source_supported_program_count": supported_programs,
            "source_unsupported_program_count": len(programs["programs"]) - supported_programs,
            "repair_count": len(repair_document["repairs"]),
            "source_qualified_repair_count": sum(
                row["qualification"]["qualified"] for row in repair_document["repairs"]
            ),
            "source_unqualified_repair_count": sum(
                not row["qualification"]["qualified"] for row in repair_document["repairs"]
            ),
            "independent_review": review["round_verdicts"][category],
            "independent_review_scope": "Author-side qualification is not independent release acceptance; only the stated independent verdict applies.",
        }
    return findings, repairs, coverage


def training_history(
    documents: dict[str, Any], config: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Preserve the current eight observations without laundering alias/replay identities."""
    requested = config["training_wrong_pass_execution_ids"]
    require(
        len(requested) == len(set(requested)) == 8,
        "training cohort must identify eight distinct original executions",
    )
    originals = [
        (index, row)
        for index, row in enumerate(documents["training_original_inventory"])
        if row["execution_id"] in requested
    ]
    require(
        {row["execution_id"] for _, row in originals} == set(requested) and len(originals) == 8,
        "a current training original is missing or duplicated in its frozen inventory",
    )
    findings = []
    same_artifact_pairs = 0
    review_text = "\n".join(documents["morning_review"].splitlines()[97:121])
    for fragment in (
        "000792 `dfd522f8` and 002391 `a6bee1be`",
        "001809 ×2 and 001985 `bb7aebe3` were killed",
        "000169 ×2 or 002486 `772a78ad`",
    ):
        require(fragment in review_text, "independent training-pool review wording changed")
    review_status = {
        **dict.fromkeys(
            ("dfd522f8", "a6bee1be"), "independently_reproduced_fail_on_repaired_grader"
        ),
        **dict.fromkeys(
            ("affd5ea5", "bb7d135d", "bb7aebe3"),
            "located_in_repair_source_not_independently_replayed",
        ),
        **dict.fromkeys(("c5625dc0", "c65962f1", "772a78ad"), "not_independently_located"),
    }
    require(
        set(review_status) == {value[:8] for value in requested},
        "training review status map must cover the eight originals",
    )
    for index, row in originals:
        execution = row["execution_id"]
        aliases = []
        for alias in row["provenance"]["aliases"]:
            aliases.append(
                {
                    "executed_task_digest": alias.get("executed_task_digest"),
                    "source_digest": alias.get("source_digest"),
                    "task_checksum": alias.get("task_checksum"),
                    "files": {
                        name: {"path": value.get("path"), "sha256": value.get("sha256")}
                        if value
                        else None
                        for name, value in alias["files"].items()
                        if name in {"patch", "result", "config", "lock", "trajectory", "native"}
                    },
                }
            )
        patch_digests = sorted(
            {
                alias["files"]["patch"]["sha256"]
                for alias in aliases
                if alias["files"].get("patch") and alias["files"]["patch"].get("sha256")
            }
        )
        require(
            len(patch_digests) == 1,
            f"training execution has ambiguous/missing original program identity: {execution}",
        )
        patch_digest = patch_digests[0]
        annotations = [
            {
                "source": annotation["source"],
                "row": {
                    key: annotation["row"].get(key)
                    for key in ("item_id", "fault", "recorded_label", "review")
                },
            }
            for annotation in row["flags"].get("annotations", [])
        ]
        conflicting_annotation = any(
            "reviewed_correct" in {raw["row"].get("fault"), raw["row"].get("recorded_label")}
            or raw["row"].get("review") == "correct"
            for raw in row["flags"].get("annotations", [])
        )
        probe_cited = any(
            (raw["row"].get("native_evidence") or {}).get("failed_probe")
            for raw in row["flags"].get("annotations", [])
        )
        disputed = conflicting_annotation and not probe_cited
        altered_replay = execution == "a6bee1be-0f52-4972-9248-cff576af61a6"
        replay_pairs = []
        for repair_index, repair in enumerate(documents["train_repairs"]["repairs"]):
            for case_index, case in enumerate(repair["reproducers"]):
                if case["patch_sha256"] == patch_digest:
                    replay_pairs.append(
                        {
                            "source_finding_id": case["finding_id"],
                            "program_sha256": case["patch_sha256"],
                            "child_package_digest": repair["package_digest"],
                            "source_run": case["run"],
                            "rewards": case["rewards"],
                            "repeats": case.get("repeats", []),
                            "evidence": ref(
                                "train_repairs", f"/repairs/{repair_index}/reproducers/{case_index}"
                            ),
                        }
                    )
        same_artifact_pairs += bool(replay_pairs)
        findings.append(
            {
                "finding_id": f"training-current8:{execution}",
                "source_finding_id": execution,
                "category": "training_wrong_pass8",
                "task_id": row["task"],
                "package_digest": None,
                "package_binding_basis": "Original source_digest and executed_task_digest have different identity layers; neither is relabelled as a task package digest.",
                "primary_class_key": "weak_check",
                "primary_class": None,
                "secondary_classes": [],
                "run_class": None,
                "class_mapping_status": "INFERENCE",
                "direction": "false_pass",
                "evidence_status": "source_claim" if disputed else "source_supported_historical",
                "source_qualification": {
                    "current_wrong_pass_cohort_member": True,
                    "wrongness_disputed": disputed,
                    "reviewed_correct_annotation_present": conflicting_annotation,
                    "native_failed_probe_cited_by_source": probe_cited,
                    "independent_morning_review_row_status": review_status[execution[:8]],
                    "independently_fresh_confirmed": False,
                    "original_reward": row["observed_reward"],
                    "source_annotations": annotations,
                    "mechanism_adjudicated_in_current_audit": False,
                    "source_reported_replay_artifact_modified": altered_replay,
                },
                "program": {"source_program_id": execution, "sha256": patch_digest},
                "executions": [
                    {
                        "role": "original",
                        "execution_id": execution,
                        "source_execution_identity": row["source_trial"],
                        "job_uuid": row["provenance"].get("job_uuid"),
                        "trial_uuid": row["provenance"].get("trial_uuid"),
                        "source_result_sha256": row["provenance"].get("source_result_sha256"),
                        "observed_reward": row["observed_reward"],
                        "aliases": aliases,
                        "physical_execution_count": 1,
                        "alias_scope": "Alternate retained copies of this execution, not independent executions or repeated replays.",
                    }
                ],
                "same_program_source_replay_pairs": replay_pairs,
                "differential": {
                    "confirmed": False,
                    "original_reward": row["observed_reward"],
                    "fixed_reward": replay_pairs[0]["rewards"][0]
                    if len(replay_pairs) == 1 and len(replay_pairs[0]["rewards"]) == 1
                    else None,
                    "program_identity_bound": bool(replay_pairs),
                    "package_identities_bound": False,
                    "controls_bound": False,
                    "reason": "Original and repaired source metadata are retained, but exact native package/control custody is not established by this projection.",
                },
                "resolution": {
                    "status": "claimed_repaired_side_rejection_not_a_clean_certificate",
                    "historical_label_transferred_to_child": False,
                },
                "uncertainty": [
                    "The current source reports eight wrong passes among 14 regraded rows out of 70; 56 are unmeasured, not clean negatives.",
                    "Missing package identities prevent an exact-package atlas join; no repaired-child label transfer or intent/tamper behavior is inferred.",
                ]
                + (
                    [
                        "This original is also annotated reviewed_correct as a reference-identical leak copy with no native failed probe cited; its wrongness is a disputed source claim and is excluded from supported-defect recall."
                    ]
                    if disputed
                    else []
                )
                + (
                    [
                        "The retained patch carries binary hunks that the native path refused; a stripped replay is a different program artifact, not this original."
                    ]
                    if altered_replay
                    else []
                )
                + (
                    [
                        "The recovered 002486 run6 receipt passed 1/1/1 on an older package and does not establish the claimed rejection of this UUID."
                    ]
                    if execution == "772a78ad-799b-4b76-8ec0-399c08a97474"
                    else []
                )
                + (
                    [
                        "A reviewed_correct trace annotation coexists with a cited native failed probe; both source statements are retained."
                    ]
                    if conflicting_annotation and not disputed
                    else []
                ),
                "safe_summary": "Current training-pool archival wrong-pass observation; original execution/program identities, source disagreements and replay limitations remain explicit.",
                "evidence": [
                    ref("training_original_inventory", f"/{index}"),
                    ref("training_wrong_claims", ""),
                ]
                + (
                    [ref("morning_review")]
                    if review_status[execution[:8]] != "not_independently_located"
                    else []
                ),
            }
        )
    disputed_ids = [
        row["source_finding_id"]
        for row in findings
        if row["source_qualification"]["wrongness_disputed"]
    ]
    require(
        disputed_ids == ["c5625dc0-ffd0-4204-88f0-2e79f587d528"],
        "the disputed training source claim changed; review the exclusion explicitly",
    )
    return findings, {
        "status": "all_eight_current_source_observations_identity_bound_originals",
        "source_cohort_programs": 8,
        "source_cohort_original_executions": 8,
        "source_cohort_task_ids": len({task_id(row["task"]) for _, row in originals}),
        "source_reported_A_rows": 70,
        "source_reported_regraded_rows": 14,
        "source_reported_wrong_rows": 8,
        "source_reported_unmeasured_rows": 56,
        "disputed_wrongness_rows": len(disputed_ids),
        "disputed_wrongness_execution_ids": disputed_ids,
        "supported_rows_after_exclusions": sum(
            row["evidence_status"] == "source_supported_historical" for row in findings
        ),
        "reviewed_correct_annotation_rows": sum(
            row["source_qualification"]["reviewed_correct_annotation_present"] for row in findings
        ),
        "original_artifact_modified_for_current_replay_rows": sum(
            row["source_qualification"]["source_reported_replay_artifact_modified"]
            for row in findings
        ),
        "independent_morning_review_row_status": dict(
            sorted(
                Counter(
                    row["source_qualification"]["independent_morning_review_row_status"]
                    for row in findings
                ).items()
            )
        ),
        "same_program_source_initial_repair_pairs": same_artifact_pairs,
        "exact_package_original_bindings": 0,
        "differential_confirmed_here": 0,
        "review_scope": "Historical source observations, not eight newly independently confirmed wrong submissions; biased old7 and new7 remain distinct in the pinned source.",
    }


RUNTIME_TASK = "format-code-task-001246"


def runtime_repair_history(
    documents: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Keep the selected package, the physically staged packages and the unrepaired parent distinct."""
    index, task = next(
        (i, row)
        for i, row in enumerate(documents["heldout"]["tasks"])
        if task_id(row["task_id"]) == RUNTIME_TASK
    )
    target = task["evaluation_target"]
    receipt = target["selection_handoff"]["receipt"]
    selected, parent = target["package_digest"], task["v6_package_digest"]
    require(
        task["successor_selection"] == "runtime-repair"
        and target["repair_scope"] == "source-bound-collection-boundary"
        and receipt["schema"] == "mimo-v7-runtime-repair/v1"
        and receipt["parent"]["package_digest"] == parent
        and receipt["transform"] == "mimo-001246-collection@1"
        and target["qualified_for_v7"] is False
        and bool(DIGEST.fullmatch(selected))
        and bool(DIGEST.fullmatch(parent)),
        "the 001246 runtime-repair selection changed",
    )
    controls = target["runtime_repair_controls"]
    observations = []
    for observation in controls["observations"]:
        native = observation["native"]
        observations.append(
            {
                "role": observation["role"],
                "agent": native["agent"],
                "execution_id": native["execution_id"],
                "selected_package_digest": selected,
                "staged_source_package_digest": native["source_package_digest"],
                "staged_source_harbor_digest": native["source_harbor_digest"],
                "executed_harbor_digest": native["executed_harbor_digest"],
                "selected_harbor_digest": target["harbor_digest"],
                "staged_package_is_selected_package": native["source_package_digest"] == selected
                and native["executed_harbor_digest"] == target["harbor_digest"],
                "native_identity_verified": native["native_identity_verified"],
                "scored_repeats": native["repeat_scored"],
                "rewards": native["rewards"],
            }
        )
    off_selected = [
        row["role"] for row in observations if not row["staged_package_is_selected_package"]
    ]
    require(
        off_selected == ["prompt-valid-alternate"],
        "the 001246 control/staged-package relationship changed; review exact-package scope",
    )
    require(
        {
            row["role"]: row["rewards"]
            for row in observations
            if row["staged_package_is_selected_package"]
        }
        == {"reference": [1.0, 1.0, 1.0], "nop": [0.0, 0.0, 0.0]},
        "the 001246 selected-package controls changed",
    )
    frozen = task["native_controls"]["retained_frozen_v6"]
    parent_reward = {
        item["observation"]["receipt"]["agent"]: item["observation"]["receipt"]["reward"]
        for item in frozen["observations"]
    }
    causal = receipt["causal_evidence"]
    release_scope = documents["initial_release_adoption"]["scope"]
    require(
        release_scope["historical_001246_c4d_grades_not_transferred_to_aa6"] is True,
        "the independent release scope no longer separates the 001246 parent grades",
    )
    repair = {
        "repair_id": f"initial-v7:runtime:{RUNTIME_TASK}:{selected}",
        "category": "initial_v7",
        "task_id": RUNTIME_TASK,
        "repair_kind": "runtime_collection_boundary",
        "parent_package_digest": parent,
        "child_package_digest": selected,
        "child_harbor_digest": target["harbor_digest"],
        "child_verifier_digest": target["verifier_digest"],
        "native_record_sha256": "sha256:"
        + receipt["native_record"]["sha256"].removeprefix("sha256:"),
        "historical_label_transferred_to_child": False,
        "source_qualification": {
            "qualified_for_v7": target["qualified_for_v7"],
            "native_custody_status": controls["status"],
            "selected_package_status": task["native_controls"]["selected_package"]["status"],
            "selected_package_missing": task["native_controls"]["selected_package"]["missing"],
            "scope": controls["scope"],
        },
        "control_observations": observations,
        "controls_not_on_selected_package": off_selected,
        "parent_retained_control": {
            "package_digest": parent,
            "oracle_reward": parent_reward["oracle"],
            "nop_reward": parent_reward["nop"],
            "repeats": 1,
            "transferred_to_child": False,
        },
        "causal_evidence_digests": {
            "native_execution_id": causal["native_execution_id"],
            **{
                key: {"sha256": causal[key]["sha256"], "bytes": causal[key]["bytes"]}
                for key in ("setup_log", "snapshot", "test_output")
            },
        },
        "evidence": [ref("heldout", f"/tasks/{index}")],
    }
    finding = {
        "finding_id": f"initial-v7:heldout:{RUNTIME_TASK}:runtime-collection-repair",
        "source_finding_id": receipt["schema"],
        "category": "initial_v7",
        "task_id": RUNTIME_TASK,
        "package_digest": None,
        "parent_package_digest": parent,
        "package_binding_basis": "The causal unchanged-control package is not bound by the pinned metadata; only the source-declared repair parent and child are exact.",
        "primary_class_key": "env_mismatch_or_flaky",
        "primary_class": None,
        "secondary_classes": [],
        "run_class": None,
        "class_mapping_status": "INFERENCE",
        "direction": "false_fail",
        "evidence_status": "source_claim",
        "source_qualification": {
            "selection": task["successor_selection"],
            "repair_scope": target["repair_scope"],
            "failing_observation_retained_only_as_digest": True,
        },
        "program": None,
        "executions": [
            {
                "role": "causal_unchanged_control",
                "execution_id": causal["native_execution_id"],
                "source_package_digest": None,
                "reward": None,
            }
        ],
        "differential": {
            "confirmed": False,
            "reason": "A runtime/collection-boundary repair selection, not an exploit; no original1/fixed0 differential applies.",
        },
        "resolution": {
            "status": "runtime_repair_selected_controls_not_semantically_certified",
            "child_package_digest": selected,
            "historical_label_transferred_to_child": False,
        },
        "uncertainty": [
            "Class and false-fail direction are inferred from the runtime-repair selection and collection-boundary scope; the failing observation itself is pinned only by digest, not by a verifiable reward in this projection.",
            "Selected-package reference x3=1 and nop x3=0 are native-custody controls, not semantic certification; the prompt-valid alternate was staged on a different package and is not a selected-package control. Parent v6 oracle/nop x1 grades are not transferred to the child.",
        ],
        "safe_summary": "A source-selected runtime/collection-boundary repair of the parent package; selected, staged and parent identities stay separate.",
        "evidence": [ref("heldout", f"/tasks/{index}")],
    }
    return (
        finding,
        repair,
        {
            "task_id": RUNTIME_TASK,
            "selected_package_digest": selected,
            "parent_package_digest": parent,
            "selected_package_controls": {"reference": [1.0, 1.0, 1.0], "nop": [0.0, 0.0, 0.0]},
            "controls_not_on_selected_package": off_selected,
            "semantic_clean": False,
        },
    )


GENERIC_NORMALIZATIONS = frozenset(
    {
        "agent-network-none",
        "separate-verifier",
        "mtime-normalize",
        "purge-build-caches",
        "purge-installed-copies",
        "strip-future-history",
    }
)
EXPLICIT_RUNTIME_CORRECTIONS = frozenset(
    {"setup-python-path", "env-keep-files", "env-keep-build-outputs"}
)
CONTROLS_ONLY = "controls_only_reference_nop"
COHORT_CATEGORY = {"batch2": "batch2_v7"}
RECORDED_SCOPE_KEYS = (
    "recorded_wrong_cases",
    "recorded_wrong_zero_vectors",
    "QA_adjudicated_correct_alternatives",
    "QA_correct_accepted_vectors",
    "observed_QA_correct_false_negatives",
    "nop_native_trials",
    "reference_native_trials",
    "unrecorded_correct_implementation_claim",
)


def edge_kind(transform: str) -> str:
    """Classify a registered transform by name only; never infer a defect from a generic step."""
    name = transform.split("@")[0]
    if name in GENERIC_NORMALIZATIONS:
        return "generic_normalization"
    if name == "verifier-repair":
        return "verifier_repair"
    if name == "leak-close-pypi":
        return "leak_closure"
    if name in EXPLICIT_RUNTIME_CORRECTIONS or name.endswith("-collection"):
        return "explicit_runtime_correction"
    return "unclassified_transform"


def catalog_lineage(row: dict[str, Any]) -> dict[str, Any]:
    lineage = row["lineage"]
    edges = [
        {
            "package_digest": edge["package_digest"],
            "parent_package_digest": edge["parent"]["package_digest"],
            "transform": edge["transform"],
            "components_changed": edge["components_changed"],
            "kind": edge_kind(edge["transform"]),
            "record_sha256": edge.get("record_sha256"),
        }
        for edge in lineage.get("registered_edges_newest_first", ())
    ]
    return {
        "kind_mapping_status": "INFERENCE",
        "kind_basis": "transform name only",
        "edge_count": len(edges),
        "registered_edges_newest_first": edges,
        "record_sha256": lineage.get("record_sha256"),
    }


def known_weak_ancestry(
    row: dict[str, Any],
    known_weak: dict[str, list[str]],
    lineage: dict[str, Any],
    direct_parent: str | None,
) -> dict[str, Any]:
    """Bind a known-weak ancestor only when a registered edge names that exact package.

    A same-task historical positive is task-ID ancestry. It is never evidence about the repaired
    child or its direct parent, because the verifier changes between those packages.
    """
    named = {
        digest
        for edge in lineage["registered_edges_newest_first"]
        for digest in (edge["package_digest"], edge["parent_package_digest"])
    }
    candidates = known_weak.get(row["task_id"], [])
    established = [digest for digest in candidates if digest in named]
    return {
        "same_task_historical_positive_item": row["weakness_review"][
            "same_task_historical_positive_item"
        ],
        "same_task_known_weak_package_digests": candidates,
        "ancestor_package_digests_in_registered_lineage": established,
        "ancestry_basis": "registered_lineage_edge"
        if established
        else "same_task_id_only_not_established"
        if candidates
        else None,
        "direct_parent_package_digest": direct_parent,
        "ancestor_is_direct_parent": direct_parent in established,
        "label_transferred_to_child": False,
        "label_transferred_to_direct_parent": False,
    }


def batch2_details(batch2: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[Any, Any]:
    """Project only controls-handoff metadata for each batch2 repaired package."""
    require(
        batch2["training_admission"] is False
        and batch2["scoring_allowed"] is False
        and batch2["dispatch_allowed"] is False
        and batch2["all_requested_reference_nop_vectors_pass"] is True,
        "batch2 control handoff changed its admission scope",
    )
    details: dict[tuple[str, str], dict[str, Any]] = {}
    for row in catalog:
        if row["status"] != "repaired" or row["evidence"]["source_id"] != "batch2":
            continue
        pointer = row["evidence"]["json_pointer"]
        source = at(batch2, pointer)
        selected = source["selected"]
        name = task_id(source["task_id"])
        require(
            name == row["task_id"]
            and selected["package_digest"] == row["package_digest"]
            and selected["harbor_digest"] == row["harbor_digest"]
            and selected["verifier_digest"] == row["verifier_digest"],
            f"batch2 metadata disagrees with the catalog selection: {row['task_id']}",
        )
        record = source["lineage_record"]
        superseded = source.get("supersedes_requested_package")
        coverage = source.get("coverage")
        inherited = source.get("coverage_scope_inherited_from")
        details[(name, row["package_digest"])] = {
            "source": ref("batch2", pointer),
            "evidence_origin": source["evidence_origin"],
            "strict_assertion_scope_admission": source["strict_assertion_scope_admission"],
            "reference_nop_repeat3_pair_proved": source.get("reference_nop_repeat3_pair_proved"),
            "transformation": source.get("transformation"),
            "changed_files": source.get("changed_files"),
            "definition_inventory": source.get("definition_inventory"),
            "recorded_scope_counts": {key: coverage.get(key) for key in RECORDED_SCOPE_KEYS}
            if coverage
            else {
                key: source.get(key)
                for key in (
                    "known_wrong_cases",
                    "recorded_wrong_zero_vectors",
                    "adjudicated_correct_alternatives",
                )
                if key in source
            }
            or None,
            "inherited_scope": None
            if inherited is None
            else {
                "sha256": inherited["sha256"],
                "json_pointer": inherited["json_pointer"],
            },
            "lineage_record": {
                key: record.get(key) for key in ("sha256", "status", "complete_edges")
            },
            "supersedes_requested_package": None
            if superseded is None
            else {
                key: superseded[key]
                for key in ("package_digest", "harbor_digest", "verifier_digest")
            },
            "source_limits": [
                text for text in batch2["limits"] if name.removeprefix("format-code-task-") in text
            ],
        }
    return details


REVIEW_NOTE = "Reviewer INFERENCE from a read-only metadata review; not a source-recorded diagnosis, not a recall input."
RUNTIME_CLASS_REVIEWS = {
    ("format-code-task-001948", "setup-python-path@1"): {
        "primary_class": None,
        "status": "unmapped_underdetermined",
        "note": REVIEW_NOTE,
        "defect_subject": "derived-package setup helper (pipeline), not the source task or its grader",
        "candidate_considered": {
            "env_mismatch_or_flaky": "weak thematic fit only; no bound observation shows one artifact scored differently with a named differing factor"
        },
        "not_transferred": "The selected child's weak_check finding binds to the intermediate package after this edge; it does not describe this setup edge or its parent.",
    },
    ("format-code-task-002486", "mimo-002486-collection@1"): {
        "primary_class": None,
        "direction": None,
        "status": "unmapped_underdetermined",
        "note": REVIEW_NOTE,
        "defect_subject": "control evidence quality: parent nop reached only collection, not a source-task grader defect",
        "rejected": {
            "env_mismatch_or_flaky": "the source classifies the parent failure as task work, not infrastructure, and names no differing environment factor",
            "false_fail": "no wrong grade of a correct submission is recorded; a nop zero is the expected control outcome",
        },
        "not_copied_by_analogy_from": "format-code-task-001246",
    },
    ("format-code-task-001246", "mimo-001246-collection@1"): {
        "primary_class": None,
        "status": "unmapped_underdetermined",
        "note": REVIEW_NOTE,
        "existing_finding_unchanged": "The separate source_claim finding keeps its visible env_mismatch_or_flaky/false_fail INFERENCE; it is excluded from supported recall and was not relabeled here.",
        "evidence_against_candidates": {
            "unstated_requirement": "the failing line is a first-party import chain in the base checkout, not a grader assertion, and an executed prompt-valid alternate passed",
            "env_mismatch_or_flaky": "the nop and setup failures are the same deterministic missing-helper ImportError; no artifact was scored differently across environments",
        },
        "open_for_owner": "Whether to relabel the existing finding's class/direction is left to the parent integrator.",
    },
}


def runtime_correction_details(
    documents: dict[str, Any], catalog: list[dict[str, Any]]
) -> dict[Any, Any]:
    """Bind explicit corrective runtime edges to their pinned records; no file bodies are exported."""
    details: dict[tuple[str, str], dict[str, Any]] = {
        key: {"class_review": review} for key, review in RUNTIME_CLASS_REVIEWS.items()
    }

    def selected(task: str) -> dict[str, Any]:
        rows = [r for r in catalog if r["task_id"] == task and r["status"] == "repaired"]
        require(len(rows) == 1, f"expected exactly one selected repaired package for {task}")
        return rows[0]

    task = "format-code-task-001948"
    variant, transform = documents["variant_001948_setup_python_path"], "setup-python-path@1"
    edge = next(
        edge
        for edge in catalog_lineage(selected(task))["registered_edges_newest_first"]
        if edge["transform"] == transform
    )
    require(
        task_id(variant["task_name"]) == task
        and variant["transform"] == transform
        and variant["variant_digest"] == edge["package_digest"]
        and variant["parent"]["digest"] == edge["parent_package_digest"],
        "the 001948 setup-python-path record disagrees with the catalog lineage edge",
    )
    details[(task, transform)].update(
        {
            "source": ref("variant_001948_setup_python_path", ""),
            "record_status": variant["status"],
            "parent_package_digest": variant["parent"]["digest"],
            "components_changed": variant["components_changed"],
            "file_digests": [
                {key: item[key] for key in ("path", "before_sha256", "after_sha256")}
                for item in variant["files"]
            ],
            "tests_tree_sha256_before_and_after": variant["inputs"]["tests_before_sha256"],
            "setup_python_path": variant["inputs"]["setup_python_path"],
            "source_fix": variant["inputs"]["source_fix"],
            "source_recorded_reason": variant["rationale"],
            "executed_parent_failure": None,
            "executed_parent_failure_note": "No executed failing observation of the parent is bound in the pinned metadata; the reason is a source-recorded claim, not an observed wrong score. Absence of a bound failure is not evidence that none exists in unpinned logs.",
            "child_side_controls": "setup-only native controls on the intermediate package are recorded by the source as candidate-only; they are not semantic certification.",
        }
    )
    task = "format-code-task-002486"
    lineage_record, transform = documents["batch2_002486_lineage"], "mimo-002486-collection@1"
    child = selected(task)
    parent_nop = [
        row
        for row in documents["batch2_inherited_scope"]["native_controls"]
        if task_id(row["task_id"]) == task and row["case"] == "nop"
    ]
    require(len(parent_nop) == 1, "the 002486 parent nop observation is not unique")
    observed, scope = (
        parent_nop[0]["observation"],
        parent_nop[0]["semantic_scope"]["classification"],
    )
    require(
        task_id(lineage_record["task_name"]) == task
        and lineage_record["transform"] == transform
        and lineage_record["variant_digest"] == child["package_digest"]
        and lineage_record["parent"]["digest"] == observed["source_package_digest"]
        and observed["agent"] == "nop"
        and observed["rewards"] == [0.0, 0.0, 0.0],
        "the 002486 lineage record disagrees with the catalog or the retained parent nop",
    )
    details[(task, transform)].update(
        {
            "source": ref("batch2_002486_lineage", ""),
            "record_status": lineage_record["status"],
            "parent_package_digest": lineage_record["parent"]["digest"],
            "parent_harbor_digest": lineage_record["parent"]["harbor_digest"],
            "components_changed": lineage_record["components_changed"],
            "file_digests": [
                {key: item[key] for key in ("path", "before_sha256", "after_sha256")}
                for item in lineage_record["files"]
            ],
            "cause": lineage_record["inputs"]["cause"],
            "collection_boundary": lineage_record["inputs"]["collection_boundary"],
            "original_test_definition_count": sum(
                lineage_record["inputs"]["original_test_definitions"].values()
            ),
            "native_controls_executed_on_the_record_itself": lineage_record["inputs"][
                "native_controls_executed"
            ],
            "source_recorded_reason": lineage_record["rationale"],
            "executed_parent_failure": {
                "source": ref("batch2_inherited_scope", "/native_controls"),
                "role": "nop",
                "package_digest": observed["source_package_digest"],
                "execution_id": observed["execution_id"],
                "rewards": observed["rewards"],
                "source_classification": scope["observed_disposition"],
                "missing_requested_api_is_task_work": scope[
                    "missing_build_install_plan_is_task_work"
                ],
                "assertion_suite_executed": scope["assertions_executed_now"],
            },
        }
    )
    return details


def corrective_runtime_lineage(
    task: str,
    lineage: dict[str, Any],
    detail: dict[str, Any] | None,
    child: str,
    runtime: dict[Any, Any],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "source": "clean_v7_registered_edge",
            "transform": edge["transform"],
            "components_changed": edge["components_changed"],
            "package_digest": edge["package_digest"],
            "parent_package_digest": edge["parent_package_digest"],
        }
        for edge in lineage["registered_edges_newest_first"]
        if edge["kind"] == "explicit_runtime_correction"
    ]
    transformation = (detail or {}).get("transformation")
    if transformation and edge_kind(transformation) == "explicit_runtime_correction":
        steps.append(
            {
                "source": "batch2_selected_transformation",
                "transform": transformation,
                "changed_files": detail["changed_files"],
                "package_digest": child,
                "parent_package_digest": None,
            }
        )
    for step in steps:
        extra = runtime.get((task, step["transform"]))
        if extra is not None:
            step["source_detail"] = extra
            step["parent_package_digest"] = step["parent_package_digest"] or extra.get(
                "parent_package_digest"
            )
    return steps


def staged_role(observed: dict[str, Any], child: str) -> dict[str, Any]:
    """A control counts for the selected package only when it was staged on that exact package."""
    return {
        "rewards": observed.get("rewards"),
        "execution_id": observed.get("execution_id"),
        "staged_source_package_digest": observed.get("source_package_digest"),
        "staged_on_selected_package": observed.get("source_package_digest") == child,
    }


def reconcile_v7_repairs(
    catalog: list[dict[str, Any]],
    repairs: list[dict[str, Any]],
    details: dict[Any, Any],
    heldout: list[str],
    runtime: dict[Any, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Cover every selected repaired v7 package once, keyed by exact task+child package."""
    runtime = runtime or {}
    known_weak: dict[str, list[str]] = {}
    for row in catalog:
        if row["status"] == "known-weak":
            known_weak.setdefault(row["task_id"], []).append(row["package_digest"])
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    for row in repairs:
        key = (row["task_id"], row["child_package_digest"])
        require(key not in existing, f"duplicate repair row for exact task/package: {key}")
        existing[key] = row
    selected = [(index, row) for index, row in enumerate(catalog) if row["status"] == "repaired"]
    keys = [(row["task_id"], row["package_digest"]) for _, row in selected]
    require(len(set(keys)) == len(keys), "catalog repeats a repaired task/package")
    catalog_status = {(row["task_id"], row["package_digest"]): row["status"] for row in catalog}
    decorated: dict[tuple[str, str], dict[str, Any]] = {}
    added: list[dict[str, Any]] = []
    for index, row in selected:
        key = (row["task_id"], row["package_digest"])
        require(
            row["heldout_excluded_from_training"] is (row["task_id"] in heldout),
            f"catalog heldout exclusion disagrees with the frozen all-version list: {row['task_id']}",
        )
        lineage = catalog_lineage(row)
        detail = details.get(key)
        prior = existing.get(key)
        steps = corrective_runtime_lineage(
            row["task_id"], lineage, detail, row["package_digest"], runtime
        )
        if prior is None:
            category = COHORT_CATEGORY.get(row["evidence"]["source_id"])
            require(
                category is not None and detail is not None,
                f"selected repaired package has no repair history or recognized cohort: {key}",
            )
            parent = next(
                (
                    step["parent_package_digest"]
                    for step in steps
                    if step["source"] == "batch2_selected_transformation"
                ),
                None,
            )
        else:
            category, parent = prior["category"], prior.get("parent_package_digest")
        newest = lineage["registered_edges_newest_first"][:1]
        pointer = f"/tasks/{index}"
        block = {
            "source": ref("clean_v7", pointer),
            "record_id": row["record_id"],
            "status": row["status"],
            "selection": row.get("selection"),
            "repair_scope": row["repair_scope"],
            "heldout_excluded_from_training": row["heldout_excluded_from_training"],
            "usage": row["usage"],
            "lineage": lineage,
            "existing_parent_matches_newest_registered_edge": None
            if prior is None or not newest
            else newest[0]["parent_package_digest"] == parent,
        }
        controls = row["controls"]
        common = {
            "source_catalog": block,
            "known_weak_ancestry": known_weak_ancestry(row, known_weak, lineage, parent),
            "corrective_runtime_lineage": steps,
        }
        if prior is not None:
            decorated[key] = {
                **prior,
                **common,
                "evidence": [*prior["evidence"], ref("clean_v7", pointer)],
            }
            continue
        observations = controls.get("observations", [])
        added.append(
            {
                "repair_id": f"v7-catalog:{category}:{row['task_id']}:{row['package_digest']}",
                "category": category,
                "task_id": row["task_id"],
                "repair_kind": CONTROLS_ONLY,
                "evidence_kind": CONTROLS_ONLY,
                "observed_wrong_program_positive": False,
                "parent_package_digest": parent,
                "parent_binding": "named by the pinned candidate lineage record for the selected transformation"
                if parent
                else "not recorded in the pinned catalog or controls-handoff metadata; only the lineage record digest is exported",
                "supersedes_requested_package": detail["supersedes_requested_package"],
                "child_package_digest": row["package_digest"],
                "child_harbor_digest": row["harbor_digest"],
                "child_verifier_digest": row["verifier_digest"],
                "historical_label_transferred_to_child": False,
                "source_qualification": {
                    "status": "strict_reference_nop_pair_source_scoped",
                    "scope": row["repair_scope"],
                    "strict_assertion_scope_admission": detail["strict_assertion_scope_admission"],
                    "reference_nop_repeat3_pair_proved": detail[
                        "reference_nop_repeat3_pair_proved"
                    ],
                    "semantic_certification": False,
                    "training_admission": False,
                    "dispatch_authorized": False,
                },
                "control_summary": {
                    "strict_reference_nop_pair": controls.get("strict_reference_nop_pair"),
                    "reference": staged_role(controls.get("reference", {}), row["package_digest"]),
                    "nop": staged_role(controls.get("nop", {}), row["package_digest"]),
                    "other_package_observation_count": sum(
                        item["observation"]["source_package_digest"] != row["package_digest"]
                        for item in observations
                    ),
                },
                "controls_handoff": {
                    key: value
                    for key, value in detail.items()
                    if key not in {"source", "supersedes_requested_package"}
                },
                **common,
                "evidence": [ref("clean_v7", pointer), detail["source"]],
            }
        )
    merged = [decorated.get((row["task_id"], row["child_package_digest"]), row) for row in repairs]
    merged += added
    retained = [
        {
            "repair_id": row["repair_id"],
            "category": row["category"],
            "task_id": row["task_id"],
            "child_package_digest": row["child_package_digest"],
            "catalog_status_of_exact_package": catalog_status.get(
                (row["task_id"], row["child_package_digest"])
            ),
        }
        for row in repairs
        if (row["task_id"], row["child_package_digest"]) not in decorated
    ]
    packages: dict[str, set[str]] = {}
    for row in merged:
        packages.setdefault(row["task_id"], set()).add(row["child_package_digest"])
    corrective = [
        {
            "task_id": row["task_id"],
            "child_package_digest": row["child_package_digest"],
            "transforms": [step["transform"] for step in row["corrective_runtime_lineage"]],
        }
        for row in merged
        if row.get("corrective_runtime_lineage")
    ]
    covered = {(row["task_id"], row["child_package_digest"]) for row in merged}
    summary = {
        "source": ref("clean_v7", "/tasks"),
        "identity_join": ["task_id", "child_package_digest"],
        "catalog_rows_by_status": dict(sorted(Counter(row["status"] for row in catalog).items())),
        "selected_repaired_packages": len(selected),
        "selected_distinct_task_ids": len({key[0] for key in keys}),
        "reconciled_to_existing_rows": len(decorated),
        "added_from_catalog": len(added),
        "added_by_category": dict(sorted(Counter(row["category"] for row in added).items())),
        "selected_packages_without_repair_row": len(set(keys) - covered),
        "selected_heldout_excluded_packages": sum(
            row["heldout_excluded_from_training"] for _, row in selected
        ),
        "repair_rows_total": len(merged),
        "repair_rows_by_category": dict(sorted(Counter(row["category"] for row in merged).items())),
        "retained_repair_rows_outside_selected_catalog": {
            "count": len(retained),
            "by_category": dict(sorted(Counter(row["category"] for row in retained).items())),
            "rows": retained,
        },
        "task_ids_with_multiple_repair_packages": {
            name: len(digests) for name, digests in sorted(packages.items()) if len(digests) > 1
        },
        "explicit_runtime_correction_rows": corrective,
        "ancestry_established_selected_packages": sum(
            bool(row["known_weak_ancestry"]["ancestor_package_digests_in_registered_lineage"])
            for row in merged
            if "known_weak_ancestry" in row
        ),
        "same_task_id_only_ancestry_selected_packages": sum(
            row["known_weak_ancestry"]["ancestry_basis"] == "same_task_id_only_not_established"
            for row in merged
            if "known_weak_ancestry" in row
        ),
    }
    require(
        summary["selected_packages_without_repair_row"] == 0, "v7 repair coverage is incomplete"
    )
    return merged, summary


def classify_repairs(
    repairs: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    classes: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Attach an INFERENCE class only where a supported source finding gives one; else unmapped.

    Claim-only or diagnostic findings, controls-only rows and corrective transforms are never
    classified by analogy to another task's repair.
    """
    supported = {"source_supported_historical", "differential_confirmed"}
    linked: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for finding in findings:
        child = finding.get("resolution", {}).get("child_package_digest")
        if child and finding.get("primary_class"):
            linked.setdefault((finding["task_id"], child), []).append(finding)
    result = []
    for row in repairs:
        controls_only = row.get("evidence_kind") == CONTROLS_ONLY
        candidates = (
            [] if controls_only else linked.get((row["task_id"], row["child_package_digest"]), [])
        )
        matched = [finding for finding in candidates if finding["evidence_status"] in supported]
        unsupported = sorted(
            finding["finding_id"] for finding in candidates if finding not in matched
        )
        if matched:
            keys = sorted({finding["primary_class"]["key"] for finding in matched})
            result_class = {
                "status": "INFERENCE",
                "class_keys": keys,
                "classes": [classes[key] for key in keys],
                "basis": "classes already mapped on supported source findings for this exact child package",
                "linked_finding_ids": sorted(finding["finding_id"] for finding in matched),
            }
        else:
            result_class = {
                "status": "unmapped_underdetermined",
                "class_keys": [],
                "classes": [],
                "basis": "controls-only repaired metadata does not diagnose a defect"
                if controls_only
                else "only claim-only source findings are linked; they keep their own visible INFERENCE mapping"
                if unsupported
                else "no class-bearing supported source finding is linked to this exact child package",
                "linked_finding_ids": [],
            }
        if unsupported:
            result_class["linked_unsupported_finding_ids"] = unsupported
        result.append({**row, "repair_class": result_class})
    return result


def batch2_coverage(
    repairs: list[dict[str, Any]], reconciliation: dict[str, Any], batch2: dict[str, Any]
) -> dict[str, Any]:
    rows = [row for row in repairs if row["category"] == "batch2_v7"]
    counts = []
    for row in rows:
        recorded = row["controls_handoff"]["recorded_scope_counts"] or {}
        counts.append(recorded.get("recorded_wrong_cases", recorded.get("known_wrong_cases")))
    return {
        "status": "controls_only_reconciled_no_wrong_program_findings",
        "scope": "20 strict reference/nop pairs; no dispatch or semantic admission",
        "selected_batch2_tasks": len(batch2["selected_task_ids"]),
        "repair_rows": len(rows),
        "added_by_reconciliation": reconciliation["added_by_category"].get("batch2_v7", 0),
        "strict_reference_nop_pairs": sum(
            row["control_summary"]["strict_reference_nop_pair"] is True for row in rows
        ),
        "reference_all_pass_nop_all_fail_reported": sum(
            set(row["control_summary"]["reference"]["rewards"] or ()) == {1}
            and set(row["control_summary"]["nop"]["rewards"] or ()) == {0}
            for row in rows
        ),
        "reference_and_nop_staged_on_selected_package": sum(
            row["control_summary"]["reference"]["staged_on_selected_package"]
            and row["control_summary"]["nop"]["staged_on_selected_package"]
            for row in rows
        ),
        "reference_staged_on_other_package_task_ids": sorted(
            row["task_id"]
            for row in rows
            if not row["control_summary"]["reference"]["staged_on_selected_package"]
        ),
        "observed_wrong_program_positive_rows": sum(
            row["observed_wrong_program_positive"] for row in rows
        ),
        "inherited_recorded_wrong_case_count_rows": sum(count is not None for count in counts),
        "inherited_recorded_wrong_case_count_total": sum(count or 0 for count in counts),
        "inherited_count_interpretation": "Counts from the inherited retained-controls scope; program identities are not exported by the pinned handoff, so they are neither findings nor recall inputs.",
        "heldout_task_ids": sorted(
            row["task_id"]
            for row in rows
            if row["source_catalog"]["heldout_excluded_from_training"]
        ),
        "same_task_other_package_repair_rows": sum(
            len(
                {
                    other["child_package_digest"]
                    for other in repairs
                    if other["task_id"] == row["task_id"]
                }
            )
            > 1
            for row in rows
        ),
        "semantic_admission": False,
        "training_admission": False,
        "source_limits": batch2["limits"],
    }


def release_provenance(documents: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Separate the independent PASS on the candidate bytes from the owner's gate-only adoption."""
    sources = {row["source_id"]: row for row in config["sources"]}
    review, adoption, released = (
        documents["initial_release_review"],
        documents["initial_release_adoption"],
        sources["heldout"],
    )
    proof = sources["initial_release_review"]
    candidate = adoption["independent_authority"]["verifier_qualified_candidate"]
    gate = adoption["authorized_gate_only_released_manifest"]
    require(
        review["pass"] is True
        and review["canonical"]["sha256"] == candidate["sha256"]
        and review["canonical"]["bytes"] == candidate["bytes"]
        and "sha256:" + adoption["independent_authority"]["retained_proof"]["sha256"]
        == proof["sha256"]
        and adoption["independent_authority"]["retained_proof"]["bytes"] == proof["bytes"]
        and "sha256:" + gate["sha256"] == released["sha256"]
        and gate["bytes"] == released["bytes"]
        and gate["sha256"] != candidate["sha256"]
        and sorted(adoption["adoption"]["changed_root_keys"])
        == ["freeze_ready", "independent_verifier", "pending_gates", "scoring_allowed", "status"],
        "initial release review/adoption identities changed",
    )
    heldout = documents["heldout"]

    def pending_nodes(value: Any) -> int:
        if isinstance(value, dict):
            return (value.get("status") == "pending") + sum(
                pending_nodes(item) for item in value.values()
            )
        if isinstance(value, list):
            return sum(pending_nodes(item) for item in value)
        return 0

    return {
        "independent_review": {
            "status": review["status"],
            "created_utc": review["created_at_utc"],
            "proof": {"sha256": proof["sha256"], "bytes": proof["bytes"]},
            "reviewed_candidate": {
                "sha256": "sha256:" + candidate["sha256"],
                "bytes": candidate["bytes"],
            },
            "candidate_flags_observed": review["canonical_flags_observed"],
            "counts": review["counts"],
            "same33_ordered_ids": review["same33_ordered_ids"],
            "new_native_trials": review["own_new_native_trials"],
            "new_grader_invocations": review["own_new_grader_invocations"],
        },
        "owner_adoption": {
            "released_manifest": {"sha256": released["sha256"], "bytes": released["bytes"]},
            "changed_root_keys": sorted(adoption["adoption"]["changed_root_keys"]),
            "released_flags_observed": {
                key: heldout[key] for key in ("status", "freeze_ready", "scoring_allowed")
            }
            | {"pending_gates_empty": heldout["pending_gates"] == []},
            "protected_admission_graders": adoption["protected_admission"][
                "actual_graders_admitted"
            ],
        },
        "independent_verifier_rehashed_final_released_bytes": False,
        "nested_status_pending_nodes_in_released_bytes": pending_nodes(heldout),
        "nested_pending_scope": "Historical nested per-task statuses in the released bytes were not rewritten by the five root-gate changes; they are not current release gates.",
        "scope": "The independent proof hashes the candidate bytes. The owner's gate-only adoption produced the released bytes; this map consumes them as owner-adopted, not as a verifier-rehashed artifact. No release rights are granted here.",
    }


def historical_findings(
    documents: dict[str, Any], config: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    initial, initial_repairs, controls, initial_coverage = initial_history(documents)
    runtime_finding, runtime_repair, runtime_coverage = runtime_repair_history(documents)
    rounds, round_repairs, coverage = round_history(documents)
    training, training_coverage = training_history(documents, config)
    vals = {
        "finding_id": "vals:mimo-v2.6:aggregate1795of2698",
        "source_finding_id": "vals-2026-10-07-coding-audit",
        "category": "vals_answer_leak",
        "task_id": None,
        "package_digest": None,
        "primary_class_key": "answer_reachable",
        "primary_class": None,
        "secondary_classes": [],
        "run_class": None,
        "class_mapping_status": "INFERENCE",
        "direction": "false_pass_risk",
        "evidence_status": "source_claim",
        "program": None,
        "executions": [],
        "differential": {"confirmed": False},
        "source_population": {
            "claimed_answer_reachable": 1795,
            "claimed_coding_tasks": 2698,
            "fleet567_task_package_join_available": False,
        },
        "safe_summary": "Vals reports retained answer material in 1,795 of a different 2,698-task coding population; no source per-ID/package join to this 567-task roster is provided.",
        "uncertainty": [
            "An aggregate public-source claim is not a task label on this fleet or an observed exploit in every affected environment."
        ],
        "resolution": {
            "status": "aggregate_only_unjoinable",
            "historical_label_transferred_to_child": False,
        },
        "evidence": [ref("vals_primary", "")],
    }
    require(
        "1,795" in documents["vals_primary"] and "2,698" in documents["vals_primary"],
        "Vals aggregate claim is absent from the frozen primary source",
    )
    coverage.update(
        {
            "atlas114": {
                "status": "mapped_source_supported_historical_witnesses",
                "positive_task_ids": 114,
                "physical_witnesses": 242,
                "mapping_scope": "240 TS01 weak_check and the two concrete inherited-reference block witnesses TS08 wrong_answer_key; all class mappings are INFERENCE.",
                "not_established_tasks": 453,
                "certified_negative_tasks": 0,
            },
            "initial_v7": {
                **initial_coverage,
                "runtime_repair": runtime_coverage,
                "release_provenance": release_provenance(documents, config),
            },
            "training_wrong_pass8": training_coverage,
            "vals_answer_leak": {
                "status": "aggregate_claim_unjoinable_to_fleet",
                "source_population": 2698,
                "source_claimed_answer_reachable": 1795,
                "exact_fleet_task_package_labels": 0,
            },
        }
    )
    return (
        initial + [runtime_finding] + rounds + training + [vals],
        initial_repairs + [runtime_repair] + round_repairs,
        controls,
        coverage,
    )


def normalize_findings(
    rows: list[dict[str, Any]], classes: dict[str, dict[str, str]], documents: dict[str, Any]
) -> list[dict[str, Any]]:
    result = []
    ids = set()
    for raw in rows:
        row = dict(raw)
        require(row["finding_id"] not in ids, f"duplicate finding identity: {row['finding_id']}")
        ids.add(row["finding_id"])
        if row.get("task_id"):
            row["task_id"] = task_id(row["task_id"])
        package = row.get("package_digest")
        require(
            package is None or bool(DIGEST.fullmatch(package)),
            "partial or invented package identity",
        )
        key = row.pop("primary_class_key", None)
        if key is not None:
            require(key in classes, f"class key absent from pinned taxonomy: {key}")
            row["primary_class"] = classes[key]
        for evidence in row["evidence"]:
            require(evidence["source_id"] in documents, "finding cites an unfrozen source")
            at(documents[evidence["source_id"]], evidence["json_pointer"])
        differential = row.get("differential", {})
        if differential.get("confirmed"):
            require(
                differential.get("original_reward") == 1
                and differential.get("fixed_reward") == 0
                and differential.get("controls_bound") is True
                and differential.get("program_identity_bound") is True
                and differential.get("package_identities_bound") is True,
                "confirmation needs exact original1/fixed0, identical program and bound controls",
            )
        result.append(row)
    return result


def comparison(
    findings: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    category_states: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Compare exact packages only. Unknown and failed scans never become negatives."""
    category_states = category_states or {}
    roster = {row["record_id"]: row for row in tasks}
    by_task = {row["task_id"]: row for row in tasks}
    predicted = {row["record_id"]: row for row in predictions}
    require(
        len(predicted) == len(predictions) and predicted.keys() == roster.keys(),
        "every roster selection needs exactly one prediction or explicit failure disposition",
    )
    dispositions = []
    for finding in findings:
        name, package = finding.get("task_id"), finding.get("package_digest")
        key = (finding.get("primary_class") or {}).get("key")
        record = f"{name}@{package}" if name and package else None
        if not name:
            disposition = "unjoinable_missing_task_identity"
        elif not package:
            disposition = "unjoinable_missing_package_identity"
        elif name not in by_task:
            disposition = "outside_frozen567_roster"
        elif record not in roster:
            disposition = "historical_or_other_package_not_scanned"
        elif key is None:
            disposition = "unmapped_class"
        elif predicted[record]["status"] != "measured":
            disposition = predicted[record]["status"]
        else:
            disposition = "matched" if key in predicted[record]["class_keys"] else "not_predicted"
        dispositions.append(
            {
                "finding_id": finding["finding_id"],
                "task_id": name,
                "package_digest": package,
                "class_key": key,
                "evidence_status": finding["evidence_status"],
                "category": finding["category"],
                "disposition": disposition,
            }
        )

    def recall(subset: list[dict[str, Any]]) -> dict[str, Any]:
        evaluable = [row for row in subset if row["disposition"] in {"matched", "not_predicted"}]
        matched = [row for row in evaluable if row["disposition"] == "matched"]

        def label(row: dict[str, Any]) -> tuple[str, str, str]:
            return row["task_id"], row["package_digest"], row["class_key"]

        def task(row: dict[str, Any]) -> tuple[str, str]:
            return row["task_id"], row["package_digest"]

        known_labels = {
            label(row)
            for row in subset
            if row["task_id"] and row["package_digest"] and row["class_key"]
        }
        evaluated_labels = {label(row) for row in evaluable}
        matched_labels = {label(row) for row in matched}
        known_tasks = {task(row) for row in subset if row["task_id"] and row["package_digest"]}
        evaluated_tasks = {task(row) for row in evaluable}
        matched_tasks = {task(row) for row in matched}
        state = (
            "no_known_findings"
            if not subset
            else "none_evaluable"
            if not evaluable
            else "evaluable"
        )
        return {
            "denominator_state": state,
            "known_findings": len(subset),
            "evaluable_findings": len(evaluable),
            "unevaluable_findings": len(subset) - len(evaluable),
            "matched_findings": len(matched),
            "finding_recall": len(matched) / len(evaluable) if evaluable else None,
            "known_exact_package_class_labels": len(known_labels),
            "evaluable_exact_package_class_labels": len(evaluated_labels),
            "matched_exact_package_class_labels": len(matched_labels),
            "class_label_recall": len(matched_labels) / len(evaluated_labels)
            if evaluated_labels
            else None,
            "known_task_packages": len(known_tasks),
            "evaluable_task_packages": len(evaluated_tasks),
            "matched_task_packages_any_known_class": len(matched_tasks),
            "task_recall_any_known_class": len(matched_tasks) / len(evaluated_tasks)
            if evaluated_tasks
            else None,
            "excluded_findings_by_reason": dict(
                sorted(
                    Counter(
                        row["disposition"]
                        for row in subset
                        if row["disposition"] not in {"matched", "not_predicted"}
                    ).items()
                )
            ),
        }

    supported_statuses = {"source_supported_historical", "differential_confirmed"}
    supported = [row for row in dispositions if row["evidence_status"] in supported_statuses]
    excluded = [row for row in dispositions if row["evidence_status"] not in supported_statuses]
    metrics = {
        "source_supported": recall(supported),
        "differential_confirmed_only": recall(
            [row for row in dispositions if row["evidence_status"] == "differential_confirmed"]
        ),
        "by_primary_class": {
            key: {
                **recall([row for row in supported if row["class_key"] == key]),
                "all_mapped_findings": sum(row["class_key"] == key for row in dispositions),
                "excluded_source_findings": sum(row["class_key"] == key for row in excluded),
            }
            for key in sorted(
                {row["class_key"] for row in dispositions if row["class_key"] is not None}
            )
        },
        "by_category": {
            category: {
                **recall([row for row in supported if row["category"] == category]),
                "coverage_status": category_states.get(category),
            }
            for category in sorted({row["category"] for row in dispositions} | set(category_states))
        },
        "claims_excluded_from_recall": sum(
            row["evidence_status"] == "source_claim" for row in dispositions
        ),
        "excluded_from_supported_recall": {
            "by_evidence_status": dict(
                sorted(Counter(row["evidence_status"] for row in excluded).items())
            ),
            "by_category_and_evidence_status": {
                category: dict(
                    sorted(
                        Counter(
                            row["evidence_status"]
                            for row in excluded
                            if row["category"] == category
                        ).items()
                    )
                )
                for category in sorted({row["category"] for row in excluded})
            },
            "interpretation": "Refuted or unverified programs, optional diagnostics and disputed or aggregate claims stay visible and enter neither a recall numerator nor denominator.",
        },
        "precision": None,
        "specificity": None,
        "negative_denominator": 0,
        "interpretation": "Retrospective source coverage only; no unlabelled task is a certified negative.",
    }
    known = {(row["task_id"], row["package_digest"], row["class_key"]) for row in supported}
    candidates = []
    for prediction in predictions:
        if prediction["status"] != "measured":
            continue
        for key in prediction["class_keys"]:
            if (prediction["task_id"], prediction["package_digest"], key) not in known:
                candidates.append(
                    {
                        "task_id": prediction["task_id"],
                        "package_digest": prediction["package_digest"],
                        "class_key": key,
                        "status": "unconfirmed_review_candidate",
                        "not_a_new_confirmed_defect_or_false_positive": True,
                    }
                )
    return dispositions, metrics, candidates


def pools(
    tasks: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    controls: list[dict[str, Any]],
    heldout: list[str],
) -> dict[str, Any]:
    predicted = {row["record_id"]: row for row in predictions}
    unresolved = {
        (row.get("task_id"), row.get("package_digest"))
        for row in findings
        if row["evidence_status"] in {"source_supported_historical", "differential_confirmed"}
        and row.get("resolution", {}).get("status") != "resolved_on_exact_finding_package"
    }
    qualified = {
        (row["task_id"], row["package_digest"])
        for row in controls
        if row.get("case_bound_reference_nop_qualified") is True
    }
    members, control_members, training_members = [], [], []
    exclusion_counts: Counter[str] = Counter()
    for row in tasks:
        prediction = predicted[row["record_id"]]
        if row["materialization"]["state"] != "verified_exact_package":
            exclusion_counts["package_not_verified"] += 1
        elif prediction["status"] != "measured":
            exclusion_counts["not_fully_measured_by_real_matcher"] += 1
        elif prediction["class_keys"]:
            exclusion_counts["one_or_more_predicted_classes"] += 1
        elif (row["task_id"], row["package_digest"]) in unresolved:
            exclusion_counts["unresolved_exact_package_source_supported_finding"] += 1
        else:
            members.append(row["record_id"])
            if (row["task_id"], row["package_digest"]) in qualified:
                control_members.append(row["record_id"])
                if row["task_id"] not in heldout:
                    training_members.append(row["record_id"])
    measured = sum(predicted[row["record_id"]]["status"] == "measured" for row in tasks)

    def count(values: list[str]) -> int | None:
        """No measured scan supports no count at all; zero would read as an affirmative negative."""
        return len(values) if measured else None

    return {
        "definition": "Frozen-atlas567 exact-package screen pool: verified materialized bytes, successful complete real-matcher scan, no predicted class, no unresolved source-supported finding on those exact bytes.",
        "not_current_descendant_or_semantic_clean_certificate": True,
        "scan_state": (
            "unmeasured" if not measured else "partial" if measured < len(tasks) else "complete"
        ),
        "roster_selections": len(tasks),
        "measured_selections": measured,
        "bounded_screen_pool": {"count": count(members), "members": members},
        "control_qualified_subset": {
            "definition": "Screen pool plus source-bound, scored reference-all1/nop-all0 controls on the exact package; case/assertion scope remains source-local.",
            "count": count(control_members),
            "members": control_members,
        },
        "training_exclusion_filtered_subset": {
            "definition": "Control-qualified screen members with none of the frozen33 heldout IDs; a necessary filter, not training approval or answer-exposure clearance.",
            "count": count(training_members),
            "members": training_members,
        },
        "training_authorized_by_this_release": False,
        "training_authorized_count": 0,
        "certified_semantic_clean_count": 0,
        "excluded_by_first_failed_condition": dict(sorted(exclusion_counts.items())),
        "heldout_task_ids_excluded_from_training_all_versions": heldout,
        "unknown_not_negative": True,
    }


def scan_predictions(
    index: dict[str, Any], tasks: list[dict[str, Any]], classes: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind a completed private hackbook-match scan index to roster predictions.

    Only task-side entry classes become predictions; raw stdout (which contains
    private source excerpts) is never copied into published outputs.
    """
    require(index.get("complete") is True, "scan index is not complete")
    require(not index.get("errors"), "scan index reports errors")
    id_to_key = {row["id"]: row["key"] for row in classes.values()}
    by_record: dict[str, dict[str, Any]] = {}
    for result in index["results"]:
        require(result.get("exit_code") == 0, "scan result did not exit cleanly")
        keys = sorted(
            {
                id_to_key[match["taxonomy_class_id"]]
                for match in result["matches"]
                if match.get("side") == "task"
            }
        )
        by_record[result["record_id"]] = {
            "task_id": result["task_id"],
            "package_digest": result["package_digest"],
            "harbor_digest": result["harbor_digest"],
            "verifier_digest": result["verifier_digest"],
            "status": "measured",
            "class_keys": keys,
            "raw_output": None,
            "invocation": "evallab hackbook match <task-locator> --kind task --top 51 --json",
        }
    require(
        set(by_record) == {row["record_id"] for row in tasks},
        "scan index does not cover exactly the frozen roster",
    )
    predictions = []
    for row in tasks:
        prediction = by_record[row["record_id"]]
        require(
            prediction["task_id"] == row["task_id"]
            and prediction["package_digest"] == row["package_digest"],
            "scan identity disagrees with roster package",
        )
        predictions.append({"record_id": row["record_id"], **prediction})
    matcher = {
        "status": "measured_hackbook_v0",
        "reason": "Real evallab hackbook match v0 run over all 567 frozen packages (private raw custody; only entry_id/class/side/score retained in the scan index). TS04/TS05 fire on every task, so they do not discriminate on this cohort; retrospective recall is source-overlapping (catalog seeded from atlas114), not heldout generalization.",
        "revision": (index.get("git") or {}).get("head"),
        "catalog": index.get("catalog"),
        "options": {"kind": "task", "top": 51},
        "raw_output": None,
        "known_labels_supplied_to_matcher": False,
        "scan_index_schema": index.get("schema"),
    }
    return predictions, matcher


def render_report(manifest: dict[str, Any]) -> str:
    counts, matcher = manifest["counts"], manifest["matcher"]
    metric = manifest["recall"]["source_supported"]
    pool = manifest["pools"]
    shown = lambda value: "not measured" if value is None else str(value)  # noqa: E731
    lines = [
        "# mimo-defect-map-v1 — historical source map",
        "",
        f"**Matcher readiness: {matcher['status']}.** {matcher['reason']}",
        "",
        "This is a metadata-only retrospective source map. Matcher predictions are static surface screening, not semantic confirmation.",
        "",
        "## Inventory and custody",
        "",
        f"- Frozen roster: **{counts['roster_tasks']} task IDs**, exact historical atlas packages; all33 heldout IDs remain training-excluded across every version.",
        f"- Package inventory: `{json.dumps(counts['inventory'], sort_keys=True)}`.",
        f"- Mapped source findings: **{counts['findings']}**, **{counts['distinct_finding_task_ids']} named task IDs**; findings/programs/executions are not collapsed to tasks.",
        f"- Real-matcher measured task packages: **{counts['matcher_measured']} / {counts['roster_tasks']}**. Every other package has an explicit disposition in `predictions.json`.",
        "- `sources.json` freezes repository/revision/path/byte digest/access; `manifest.json` binds generated artifacts. No raw hidden tests, solutions or trajectories are exported.",
        "",
        "## Required source cohorts",
        "",
        "| Category | Findings | Coverage disposition |",
        "| --- | ---: | --- |",
    ]
    for category, value in sorted(manifest["source_coverage"].items()):
        lines.append(f"| `{category}` | {value['finding_count']} | {value['status']} |")
    totals = manifest["hardening_round_totals"]
    lines += [
        "",
        "## Hardening rounds and independent review",
        "",
        f"Rounds 2-6 retain **{totals['source_programs']}** source programs: **{totals['source_supported_programs']}** supported, **{totals['source_unsupported_programs']}** refuted/unverified/withdrawn; **{totals['source_repair_rows']}** repair rows, **{totals['source_qualified_repair_rows']}** source-qualified, **{totals['source_unqualified_repair_rows']}** not. Complete rounds with zero programs ({', '.join(totals['complete_rounds_with_zero_programs']) or 'none'}) are real zero denominators, not missing rounds.",
        "",
        "| Round | Programs | Supported | Repairs (qualified) | Independent review |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for number in range(2, 7):
        value = manifest["source_coverage"][f"round{number}"]
        lines.append(
            f"| `round{number}` | {value['source_program_count']} | {value['source_supported_program_count']} | {value['repair_count']} ({value['source_qualified_repair_count']}) | {value['independent_review']['verdict']} |"
        )
    initial, training = (
        manifest["source_coverage"]["initial_v7"],
        manifest["source_coverage"]["training_wrong_pass8"],
    )
    release, runtime = initial["release_provenance"], initial["runtime_repair"]
    lines += [
        "",
        "Independent review verdicts come from the pinned morning review's cited ranges only; an absent or unverified verdict is not a pass.",
        "",
        "## Initial v7 release provenance, runtime repair and training exclusions",
        "",
        f"- The independent proof (`{release['independent_review']['status']}`) hashes the **candidate** bytes (`{release['independent_review']['reviewed_candidate']['bytes']}` B). The owner's gate-only adoption changed root keys `{', '.join(release['owner_adoption']['changed_root_keys'])}` and produced the released bytes (`{release['owner_adoption']['released_manifest']['bytes']}` B). The independent verifier did **not** rehash the final released bytes, and nested per-task pending statuses in them are historical.",
        f"- `{runtime['task_id']}` runtime/collection repair: selected package controls reference x3 `{runtime['selected_package_controls']['reference']}` and nop x3 `{runtime['selected_package_controls']['nop']}` are native-custody controls, not semantic certification; roles staged on a different package: `{', '.join(runtime['controls_not_on_selected_package'])}`. Parent-package grades are not transferred.",
        f"- Training wrong-pass cohort: 8 programs / 8 original executions / {training['source_cohort_task_ids']} task IDs. Disputed (excluded from supported recall): `{', '.join(training['disputed_wrongness_execution_ids'])}`; supported rows after exclusions: {training['supported_rows_after_exclusions']}; independent review row status `{json.dumps(training['independent_morning_review_row_status'], sort_keys=True)}`.",
        "- `format-code-task-000657` parent and child reference controls both fail the same original-module timing assertion in one of three runs; the child remains source-unqualified and the failures are not closed here.",
        "",
    ]
    recon, batch2 = manifest["v7_repair_reconciliation"], manifest["source_coverage"]["batch2_v7"]
    outside = recon["retained_repair_rows_outside_selected_catalog"]
    lines += [
        "## v7 repair coverage",
        "",
        f"- Pinned `clean_v7` selects **{recon['selected_repaired_packages']}** repaired task/package rows ({recon['selected_distinct_task_ids']} task IDs). Every one has a repair-history row keyed by exact task + child package: **{recon['reconciled_to_existing_rows']}** reconciled to existing records and **{recon['added_from_catalog']}** added (`{json.dumps(recon['added_by_category'], sort_keys=True)}`); uncovered: **{recon['selected_packages_without_repair_row']}**. Total repair rows **{recon['repair_rows_total']}** (`{json.dumps(recon['repair_rows_by_category'], sort_keys=True)}`).",
        f"- {outside['count']} earlier repair rows are not selected `clean_v7` packages (`{json.dumps(outside['by_category'], sort_keys=True)}`: blocked initial children and later hardening-round children). They are retained, qualified or not, and are not counted among the {recon['selected_repaired_packages']} selected packages.",
        f"- Task IDs with more than one repaired package stay separate: `{json.dumps(recon['task_ids_with_multiple_repair_packages'], sort_keys=True)}`.",
        f"- `batch2_v7`: **{batch2['repair_rows']}/{batch2['selected_batch2_tasks']}** rows, scope _{batch2['scope']}_. Controls only: reference-pass/nop-fail reported for **{batch2['reference_all_pass_nop_all_fail_reported']}**, but reference **and** nop were staged on the selected package for only **{batch2['reference_and_nop_staged_on_selected_package']}** (reference staged elsewhere: `{', '.join(batch2['reference_staged_on_other_package_task_ids'])}`). Observed wrong-program positive rows: **{batch2['observed_wrong_program_positive_rows']}**; findings exported: **{batch2['finding_count']}**. The inherited retained-case counts (total {batch2['inherited_recorded_wrong_case_count_total']}) are scope counts, not findings or recall inputs. No semantic or training admission.",
        f"- Repair classes are INFERENCE only where pinned metadata or a linked source finding supports one: selected packages `{json.dumps(recon['selected_repair_class_status'], sort_keys=True)}`; all repair rows `{json.dumps(recon['repair_class_status'], sort_keys=True)}`. `unmapped_underdetermined` is retained, not guessed.",
        f"- Known-weak ancestry: {recon['ancestry_established_selected_packages']} selected packages name a known-weak ancestor in a registered lineage edge; {recon['same_task_id_only_ancestry_selected_packages']} share only a task ID with a known-weak package (not established, never transferred). No ancestor label transfers to a child or direct parent.",
        f"- Explicit corrective runtime lineage (not generic normalizations): `{json.dumps(recon['explicit_runtime_correction_rows'], sort_keys=True)}`.",
        "",
    ]
    lines += [
        "",
        "## Retrospective recall",
        "",
        f"Source-supported known findings: **{metric['known_findings']}**; evaluable **{metric['evaluable_findings']}**; unevaluable **{metric['unevaluable_findings']}**; matched **{metric['matched_findings']}**.",
        f"Exact package/class labels: known **{metric['known_exact_package_class_labels']}**, evaluable **{metric['evaluable_exact_package_class_labels']}**, matched **{metric['matched_exact_package_class_labels']}**.",
        f"Task-package denominator: known **{metric['known_task_packages']}**, evaluable **{metric['evaluable_task_packages']}**; any-known-class matches **{metric['matched_task_packages_any_known_class']}**.",
        f"Finding/class/task recall: `{metric['finding_recall']}` / `{metric['class_label_recall']}` / `{metric['task_recall_any_known_class']}`. Null means no evaluable denominator, **not zero measured recall**.",
        f"Excluded findings: `{json.dumps(metric['excluded_findings_by_reason'], sort_keys=True)}`.",
        "Per-category denominators (including real zero-denominator rounds) and every unmatched/error/other-package/unjoinable finding are retained in `manifest.json` / `dispositions.json`.",
        "",
        "| Category | Known supported findings | Evaluable | State |",
        "| --- | ---: | ---: | --- |",
    ]
    for category, value in manifest["recall"]["by_category"].items():
        lines.append(
            f"| `{category}` | {value['known_findings']} | {value['evaluable_findings']} | {value['denominator_state']}; coverage `{value['coverage_status']}` |"
        )
    lines += [
        "",
        "| Primary class (inferred mapping) | All mapped | Supported | Evaluable | Matched | Finding recall |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for key, value in manifest["recall"]["by_primary_class"].items():
        recall_text = "null" if value["finding_recall"] is None else str(value["finding_recall"])
        lines.append(
            f"| `{key}` | {value['all_mapped_findings']} | {value['known_findings']} | {value['evaluable_findings']} | {value['matched_findings']} | {recall_text} |"
        )
    exclusion = manifest["recall"]["excluded_from_supported_recall"]
    lines += [
        "",
        f"Visible source exclusions (neither numerator nor denominator): `{json.dumps(exclusion['by_evidence_status'], sort_keys=True)}`.",
        "Claims, mapped-inference labels and differential confirmation are separate fields. No precision or specificity is estimated: the certified-negative denominator is zero.",
        "",
        "## New predictions and bounded pool",
        "",
        f"Unconfirmed new task/class predictions: **{counts['new_prediction_candidates']}**, listed separately in `new-predictions.json`; absent known labels do not make predictions false positives or confirmed defects.",
        pool["definition"],
        "",
        f"- Scan state: **{pool['scan_state']}** ({pool['measured_selections']} of {pool['roster_selections']} selections measured); a null count means the screen was never run, not an empty clean pool.",
        f"- Bounded screen pool: **{shown(pool['bounded_screen_pool']['count'])}**.",
        f"- Control-qualified subset: **{shown(pool['control_qualified_subset']['count'])}**.",
        f"- Training-exclusion-filtered subset: **{shown(pool['training_exclusion_filtered_subset']['count'])}**; newly training-authorized **0**.",
        "- Certified semantic-clean items: **0**. Static silence never proves semantic cleanliness.",
        "",
        "The pool concerns the frozen historical atlas selections, not the current repaired package fleet or a training admission. Selected-package controls and repaired lineage describe other exact packages; they would need their own exactly bound cohorts, and any supplemental scan of modern packages must be reported separately without changing this frozen roster. Historical labels do not transfer to repaired descendants; descendants and author/independent qualification remain separate in the repair history.",
        "",
        "## Limits",
        "",
        "Vals' 1,795/2,698 aggregate source claim concerns a different larger population and is not projected onto all567. Unjoinable task/package evidence remains explicit.",
        "This producer makes no native executions, model calls, container launches, registry mutations, paid requests or new training/scoring/serving grants.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(output: Path, values: dict[str, Any]) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    bindings = {}
    for name, value in values.items():
        material = value.encode() if isinstance(value, str) else encoded(value)
        (output / name).write_bytes(material)
        bindings[name] = {"sha256": digest(material), "bytes": len(material)}
    return bindings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "build", "report"))
    parser.add_argument("--sources", type=Path, default=HERE / "sources.json")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--results-root", type=Path)
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--scan-index", type=Path)
    args = parser.parse_args()
    if args.command == "report":
        manifest = json.loads((args.output_dir / "manifest.json").read_bytes())
        (args.output_dir / "report.md").write_text(render_report(manifest))
        print(
            json.dumps(
                {
                    "report": str(args.output_dir / "report.md"),
                    "matcher_status": manifest["matcher"]["status"],
                },
                sort_keys=True,
            )
        )
        return
    require(
        args.evidence_root is not None,
        "--evidence-root is required to validate private source custody",
    )
    config = json.loads(args.sources.read_bytes())
    roots = {"eval-lab": ROOT, "research-context": args.evidence_root}
    if any(source["repository"] == "eval-lab-results" for source in config["sources"]):
        require(
            args.results_root is not None,
            "--results-root is required for frozen original training evidence",
        )
        roots["eval-lab-results"] = args.results_root
    documents = load_sources(config, roots)
    heldout = [task_id(value) for value in documents["frozen_heldout"]["heldout"]]
    inv = inventory(documents["atlas"], heldout, args.package_root)
    if args.command == "inventory":
        binding = write_outputs(args.output_dir, {"inventory.json": inv})
        print(
            json.dumps(
                {"roster_tasks": len(inv["tasks"]), "counts": inv["counts"], "artifacts": binding},
                sort_keys=True,
            )
        )
        return
    classes = taxonomy_classes(documents[config["taxonomy_source_id"]])
    findings = atlas_findings(documents, classes)
    additions, repairs, controls, coverage = historical_findings(documents, config)
    findings = normalize_findings(findings + additions, classes, documents)
    catalog = documents["clean_v7"]
    require(
        sorted(catalog["usage"]["heldout_task_ids_excluded_from_training_all_versions"])
        == sorted(heldout),
        "catalog heldout exclusion list differs from the frozen all-version list",
    )
    repairs, reconciliation = reconcile_v7_repairs(
        catalog["tasks"],
        repairs,
        batch2_details(documents["batch2"], catalog["tasks"]),
        heldout,
        runtime_correction_details(documents, catalog["tasks"]),
    )
    repairs = classify_repairs(repairs, findings, classes)
    reconciliation["repair_class_status"] = dict(
        sorted(Counter(row["repair_class"]["status"] for row in repairs).items())
    )
    reconciliation["selected_repair_class_status"] = dict(
        sorted(
            Counter(
                row["repair_class"]["status"] for row in repairs if "source_catalog" in row
            ).items()
        )
    )
    coverage["batch2_v7"] = batch2_coverage(repairs, reconciliation, documents["batch2"])
    if args.scan_index is not None:
        predictions, matcher = scan_predictions(
            json.loads(args.scan_index.read_bytes()), inv["tasks"], classes
        )
    else:
        matcher = config["matcher"]
        require(
            matcher["status"] == "unavailable",
            "official matcher integration must bind its actual published interface; no substitute matcher is allowed",
        )
        predictions = [
            {
                "record_id": row["record_id"],
                "task_id": row["task_id"],
                "package_digest": row["package_digest"],
                "harbor_digest": row["harbor_digest"],
                "verifier_digest": row["verifier_digest"],
                "status": "matcher_unavailable",
                "class_keys": None,
                "raw_output": None,
                "invocation": None,
            }
            for row in inv["tasks"]
        ]
    dispositions, metrics, candidates = comparison(
        findings,
        inv["tasks"],
        predictions,
        {name: value["status"] for name, value in coverage.items()},
    )
    for category in REQUIRED_CATEGORIES:
        require(category in coverage, f"missing required historical source category: {category}")
        coverage[category]["finding_count"] = sum(row["category"] == category for row in findings)
    rounds = [coverage[f"round{number}"] for number in range(2, 7)]
    round_totals = {
        "rounds": [f"round{number}" for number in range(2, 7)],
        "source_programs": sum(row["source_program_count"] for row in rounds),
        "source_supported_programs": sum(row["source_supported_program_count"] for row in rounds),
        "source_unsupported_programs": sum(
            row["source_unsupported_program_count"] for row in rounds
        ),
        "source_repair_rows": sum(row["repair_count"] for row in rounds),
        "source_qualified_repair_rows": sum(row["source_qualified_repair_count"] for row in rounds),
        "source_unqualified_repair_rows": sum(
            row["source_unqualified_repair_count"] for row in rounds
        ),
        "complete_rounds_with_zero_programs": [
            name
            for name, row in zip((f"round{number}" for number in range(2, 7)), rounds, strict=True)
            if row["source_program_count"] == 0
        ],
    }
    values = {
        "inventory.json": inv,
        "findings.json": {"findings": findings},
        "repairs.json": {"repairs": repairs, "controls": controls},
        "predictions.json": {"matcher": matcher, "predictions": predictions},
        "dispositions.json": {"dispositions": dispositions},
        "new-predictions.json": {"candidates": candidates},
    }
    bindings = write_outputs(args.output_dir, values)
    manifest = {
        "schema": "mimo.defect-map/v1",
        "dataset_id": "mimo-defect-map-v1",
        "completion_state": (
            "matcher_measured_complete"
            if matcher["status"] == "measured_hackbook_v0"
            else "source_map_complete_matcher_blocked"
        ),
        "release_complete": matcher["status"] == "measured_hackbook_v0",
        "producer": {
            "path": "research/experiments/mimo-defect-map-v1/build_map.py",
            "sha256": digest(Path(__file__).read_bytes()),
        },
        "source_manifest": {"sha256": digest(args.sources.read_bytes()), "path": "sources.json"},
        "source_artifacts": config["sources"],
        "matcher": matcher,
        "taxonomy": {
            "source_id": config["taxonomy_source_id"],
            "purpose": "known_class_mapping_only",
        },
        "counts": {
            "roster_tasks": len(inv["tasks"]),
            "inventory": inv["counts"],
            "findings": len(findings),
            "distinct_finding_task_ids": len(
                {row["task_id"] for row in findings if row.get("task_id")}
            ),
            "repair_rows": len(repairs),
            "matcher_measured": sum(row["status"] == "measured" for row in predictions),
            "new_prediction_candidates": len(candidates),
        },
        "v7_repair_reconciliation": reconciliation,
        "source_coverage": coverage,
        "hardening_round_totals": round_totals,
        "recall": metrics,
        "pools": pools(inv["tasks"], findings, predictions, controls, heldout),
        "artifacts": bindings,
        "spend_usd": 0,
        "raw_private_evidence_exported": False,
        "training_or_dispatch_authorized": False,
    }
    binding = write_outputs(
        args.output_dir, {"manifest.json": manifest, "report.md": render_report(manifest)}
    )
    print(
        json.dumps(
            {
                "dataset_id": manifest["dataset_id"],
                "counts": manifest["counts"],
                "matcher_status": matcher["status"],
                "recall": metrics["source_supported"],
                "artifacts": binding,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
