#!/usr/bin/env python3
"""Freeze metadata-only TB4 sources after independent class/port-map review.

Run pm_source.py first. This importer reruns the auditor join against that final
catalog, verifies reviewed port symbols statically, and never executes task code.
Raw source prose and reproducer programs are deliberately not redistributed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml
from auditor_sources import annotate_catalog, load_envcheck_labels, parse_epoch_flags
from pm_source import attach_port_metadata
from views import public_taxonomy

from evallab.registry import task_directory_digest

HERE = Path(__file__).resolve().parent
TB_PIN = "452bf305c6daa62fc59061d22133a7cbc7c1572e"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _port_link(link: dict[str, Any], port: Path, revision: str) -> dict[str, Any]:
    """Retain a verified symbol address, never copied source/prose/payload text."""
    path = link["path"]
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("a port symbol leaves the pinned checkout")
    source = port / relative
    if source.is_symlink() or not source.resolve(strict=True).is_relative_to(port):
        raise ValueError("a port symbol points outside the pinned checkout")
    line = link["line"]
    symbol = link.get("symbol")
    if symbol is None:
        nodeid = link["nodeid"]
        prefixes = (f"{path}::", f"{path}:{line}:")
        prefix = next((prefix for prefix in prefixes if nodeid.startswith(prefix)), None)
        if prefix is None:
            raise ValueError("the port symbol address has no known source-location prefix")
        symbol = nodeid[len(prefix) :]
    base_symbol = symbol.rsplit("::", 1)[-1].split("[", 1)[0]
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=path)
    definitions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    strings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    if not any(
        node.name == base_symbol and node.lineno == line for node in definitions
    ) and not any(node.value in {symbol, base_symbol} and node.lineno == line for node in strings):
        raise ValueError(f"the cited port symbol does not exist: {path}:{symbol}")
    retained = {
        key: link[key]
        for key in (
            "nodeid",
            "path",
            "symbol",
            "line",
            "kind",
            "location",
            "match",
            "basis",
            "confidence",
            "parametrized",
            "claimed_param",
        )
        if key in link
    }
    retained["symbol"] = symbol
    retained["source_url"] = (
        f"https://github.com/preferencemodel/terminal-bench-4-karotte-port/blob/{revision}/{path}#L{line}"
    )
    retained["statically_verified"] = True
    return retained


def _packages(tasks: list[dict[str, Any]], root: Path) -> dict[str, dict[str, Any]]:
    root = root.resolve(strict=True)
    packages = {}
    for task in tasks:
        name = task["task"]
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"missing original upstream task package: {name}")
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise ValueError(f"upstream package has an unbound symlink: {name}")
        metadata = tomllib.loads((directory / "task.toml").read_text(encoding="utf-8"))
        task_id = metadata.get("task", {}).get("name") or f"terminal-bench/{name}"
        if task_id.rsplit("/", 1)[-1] != name:
            raise ValueError(f"upstream package identity differs from its source task: {name}")
        packages[name] = {
            "subject": {
                "kind": "task",
                "task_id": task_id,
                "task_digest": task_directory_digest(directory),
            },
            "package": {
                "name": name,
                "pin": TB_PIN,
                "root_hint": str(root).replace(str(Path.home()), "~", 1),
            },
        }
    return packages


def freeze(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _load(args.catalog)
    epoch = parse_epoch_flags(
        args.epoch_html.read_text(encoding="utf-8"), retrieved_at=args.epoch_retrieved_at
    )
    envcheck = load_envcheck_labels(args.envcheck_items, findings_path=args.tokenless_findings)
    annotations = annotate_catalog(catalog, epoch, envcheck)
    if not all(annotations["checks"].values()):
        raise ValueError("the final source join lost or changed evidence rows")
    review = _load(args.taxonomy_review)
    decisions = _load(args.class_decisions)
    reviewed = {row["finding_id"]: row for row in review["records"]}
    finding_ids = {row["finding_id"] for row in catalog["findings"]}
    if set(reviewed) != finding_ids or len(review["records"]) != len(finding_ids):
        raise ValueError("the class review must cover every PM finding exactly once")
    mapping = _load(args.port_review)
    mapped = {row["finding_id"]: row for row in mapping["findings"]}
    unresolved_ids = {
        row["finding_id"]
        for row in catalog["findings"]
        if row["port_test_mapping_status"] in {"candidate_only", "no_source_match"}
    }
    if set(mapped) != unresolved_ids or len(mapping["findings"]) != len(unresolved_ids):
        raise ValueError("the port review must cover every unresolved mapping exactly once")
    port = args.port_root.resolve(strict=True)
    revision = catalog["port"]["revision"]
    if mapping["port"]["revision"] != revision:
        raise ValueError("the reviewed port mapping names a different revision")
    checked_port = attach_port_metadata(catalog, port, revision=revision)
    if checked_port["port"]["tree_sha256"] != catalog["port"]["tree_sha256"]:
        raise ValueError("the port source tree changed after catalog acquisition")
    taxonomy = yaml.safe_load(args.taxonomy.read_text(encoding="utf-8"))
    packages = _packages(annotations["tasks"], args.task_root)
    rows = []
    for finding in annotations["findings"]:
        finding_id = finding["finding_id"]
        class_review = reviewed[finding_id]
        if (
            class_review["title"] != finding["title"]
            or class_review["page_ordinal"] != finding["page_ordinal"]
        ):
            raise ValueError("the class review is bound to a different finding snapshot")
        final = decisions["decisions"].get(finding_id)
        if class_review["decision"] != "unchanged" and final is None:
            raise ValueError("a changed/uncertain class has no independent final decision")
        class_metadata = {
            "original_class": class_review["original_class"],
            "reviewed_class": class_review["reviewed_class"],
            "final_class": final["class"] if final else class_review["reviewed_class"],
            "decision": class_review["decision"],
            "confidence": class_review["confidence"],
            "rationale": class_review["rationale"],
            "evidence_status": "suspected_source_claim",
            "parent_decision": final,
        }
        tests = [_port_link(link, port, revision) for link in finding["port_tests_explicit"]]
        port_review = mapped.get(finding_id)
        resolution = finding["port_test_mapping_status"]
        retained_review = None
        if port_review is not None:
            reviewed_mapping = port_review["review"]
            resolution = "reviewed_" + reviewed_mapping["resolution"]
            tests.extend(_port_link(link, port, revision) for link in reviewed_mapping["links"])
            retained_review = {
                key: reviewed_mapping.get(key)
                for key in ("resolution", "absent_reason", "notes", "method")
            }
        reward = None
        if finding["execution_claim_basis"] == "pm_legend_reward_hack":
            reward = 1
        elif finding["execution_claim_basis"] == "finding_text_upstream_result":
            claim = finding.get("execution_claim_text") or ""
            result = re.search(r"reward\s+(?:of\s+)?([01])\b", claim, flags=re.IGNORECASE)
            reward = int(result.group(1)) if result else None
        rows.append(
            {
                key: finding[key]
                for key in (
                    "finding_id",
                    "page_ordinal",
                    "task",
                    "scope",
                    "title",
                    "pm_types",
                    "port_status",
                    "port_status_label",
                    "attribution",
                    "github_refs",
                    "harbor_trial_urls",
                    "auditor_evidence",
                )
            }
            | {
                "source_url": catalog["source"]["url"] + "#" + finding["section_id"],
                "taxonomy_review": class_metadata,
                "port_tests": tests,
                "port_mapping_status": resolution,
                "port_mapping_review": retained_review,
                "pm_execution": {
                    "execution_claim": finding["execution_claim"],
                    "basis": finding["execution_claim_basis"],
                    "claimed_upstream_reward": reward,
                    "upstream_reward_one_claim": True
                    if reward == 1
                    else False
                    if reward == 0
                    else None,
                    "independently_reproduced": False,
                },
            }
        )
    tasks = []
    pm_tasks = {row["task"]: row for row in catalog["tasks"] if row["task"]}
    for task in annotations["tasks"]:
        original = pm_tasks[task["task"]]
        tasks.append(
            task
            | packages[task["task"]]
            | {
                "pm_review_status": original["none_text"],
                "pm_in_port": original["in_port"],
                "harbor_task_summary_urls": original["summary_harbor_urls"],
                "task_label_status": "reported_defect"
                if task["pm_finding_ids"] or task["epoch_flag_ids"] or task["tokenless_slugs"]
                else "unknown_not_certified_clean",
            }
        )
    source = {
        key: catalog["source"][key]
        for key in (
            "url",
            "sha256",
            "bytes",
            "checked_date",
            "retrieved_at",
            "legend_reward_hack_rule",
        )
    }
    return {
        "schema": "tb4.audit.sources/v1",
        "findings": rows,
        "tasks": tasks,
        "taxonomy": public_taxonomy(taxonomy),
        "pm": {"source": source, "counts": catalog["counts"], "port": catalog["port"]},
        "epoch": epoch,
        "envcheck": envcheck,
        "auditor_rows": {
            key: annotations[key]
            for key in ("epoch_rows", "envcheck_items", "tokenless_findings", "unresolved")
        },
        "agreement": annotations["agreement"],
        "retention_checks": annotations["checks"],
        "discrepancies": catalog["discrepancies"]
        + [
            {
                "id": "taxonomy_pm_only_counter_substring_error",
                "taxonomy_first_pass": 160,
                "parsed_source_attribution": 161,
                "reason": "not reported on GitHub is an upstream-status sentence, not GitHub source attribution",
            }
        ],
        "provenance": {
            "catalog_sha256": _digest(args.catalog),
            "taxonomy_yaml_sha256": _digest(args.taxonomy),
            "taxonomy_review_sha256": _digest(args.taxonomy_review),
            "class_decisions_sha256": _digest(args.class_decisions),
            "port_review_sha256": _digest(args.port_review),
            "class_review_method": decisions["method"],
            "scope": "Source-reported defects; task-name/source-pin comparisons are not native reproductions.",
            "pm_licence": "No explicit licence observed; only short titles and factual structured metadata redistributed. Raw PM snapshots/prose remain private.",
            "port_licence": "Apache-2.0; only symbol names, paths and hashes redistributed, not code.",
            "epoch_licence": "CC-BY-4.0; attribution Epoch AI, Terminal-Bench 4 review.",
            "tokenless_licence": "Data CC-BY-4.0, code MIT; attribution Tokenless AI, envcheck-findings at the retained source commit.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "catalog",
        "epoch-html",
        "taxonomy",
        "taxonomy-review",
        "class-decisions",
        "port-review",
        "port-root",
        "task-root",
        "tokenless-findings",
    ):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument(
        "--envcheck-items", type=Path, default=HERE.parent / "envcheck-tb4-39/items.jsonl"
    )
    parser.add_argument("--epoch-retrieved-at", required=True)
    parser.add_argument("--out", type=Path, default=HERE / "source.json")
    args = parser.parse_args()
    source = freeze(args)
    args.out.write_text(
        json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "source": str(args.out),
                "findings": len(source["findings"]),
                "tasks": len(source["tasks"]),
                "source_join_checks": source["retention_checks"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
