#!/usr/bin/env python3
"""Offline Task QA projection. Only ``auditor_input`` may enter the frozen auditor.

The pinned upstream data constructors and deterministic noise helpers are loaded
locally; no grader, task tool, network client, saved answer, or fixed code runs.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CORPUS = "parsewave__automationbench-verified"
REPO = "https://www.kaggle.com/datasets/parsewave/automationbench-verified-v0-0"
REVISION = "4a8e1061254004d9dac807054eed33fad7d1ff14"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
UPSTREAM = "upstream/zapier__AutomationBench"
DOMAINS = ("sales", "marketing", "operations", "support", "finance", "hr", "simple")
INFO_FIELDS = frozenset({"initial_state", "assertions", "zapier_tools", "meta"})
AUDITOR_FIELDS = frozenset({"repository", "revision", "prompt", "answer", "info", "grader_material"})
FIX_COLUMNS = (
    "task_name", "example_id", "domain", "scored", "status", "note", "issue_group",
    "issue_type", "finding", "what_was_wrong", "what_the_fix_does", "fix_changes",
    "test_answers", "wrong_answers", "correct_answers", "wrong_passed_by_original",
    "wrong_passed_by_fixed", "correct_failed_by_original", "correct_failed_by_fixed",
    "evidence_folder",
)
FIELDS_AUDITOR_MUST_NOT_SEE = (
    *FIX_COLUMNS, "fixed", "fix_status", "gold", "labels", "identity", "cluster_key",
    "auditor_input_status", "reconstruction", "evidence_verified", "manifest.json",
    "scores.csv", "runs", "fixed_task.py", "fixed_rubric.patch", "grader_patch_sha256",
    "automationbench_verified.jsonl", "automationbench_verified_simple.jsonl",
    "publisher_README", "publisher_description", "fixed_or_after_task",
    "fixed_or_after_grader", "saved_test_answers", "saved_grader_scores",
)


class _DatasetAsRows(ast.NodeTransformer):
    """Keep original builder semantics, returning its rows before HF allocation."""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        node = self.generic_visit(node)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Dataset"
            and node.func.attr == "from_list"
        ):
            if len(node.args) != 1 or node.keywords:
                raise ValueError("unexpected pinned dataset-construction shape")
            return ast.copy_location(node.args[0], node)
        return node


def original_tasks(bundle: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Run only the pinned data constructors/noise, without importing the app.

    Resolve their local helper imports directly. The sole representation change
    is Dataset.from_list(rows) -> rows; original function selection, ordering,
    noise injection, info JSON serialization and task-name insertion are intact.
    """
    upstream = bundle / UPSTREAM
    namespaces: dict[str, dict[str, Any]] = {}

    def namespace(relative: str) -> dict[str, Any]:
        if relative in namespaces:
            return namespaces[relative]
        path = upstream / relative
        tree = ast.parse(path.read_text(), filename=str(path))
        scope: dict[str, Any] = {"__name__": relative.replace("/", "."), "__file__": str(path)}
        namespaces[relative] = scope
        body = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "datasets":
                continue
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("automationbench.domains.")
            ):
                dependency = namespace(node.module.replace(".", "/") + ".py")
                for alias in node.names:
                    scope[alias.asname or alias.name] = dependency[alias.name]
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module]
                )
                if any(name not in {"__future__", "json", "random", "datetime", "hashlib"}
                       for name in modules):
                    raise ValueError("unexpected import in pinned data constructors")
            body.append(node)
        tree = ast.fix_missing_locations(
            _DatasetAsRows().visit(ast.Module(body=body, type_ignores=[]))
        )
        exec(compile(tree, str(path), "exec"), scope)
        return scope

    records = {}
    for domain in DOMAINS:
        relative = f"automationbench/domains/{domain}/tasks.py"
        path = upstream / relative
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text())
        builders = [
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "Dataset" and child.func.attr == "from_list"
                for child in ast.walk(node)
            )
        ]
        if len(builders) != 1:
            raise ValueError("ambiguous pinned original dataset builder")
        scope = namespace(relative)
        for row in scope[builders[0]]():
            info = json.loads(row["info"])
            task_name = info["task_name"]
            if task_name in records:
                raise ValueError("duplicate original task identity")
            records[task_name] = {**row, "info": info, "domain": domain}
    return records


def grader_material(bundle: Path = ROOT) -> dict[str, str]:
    """Original rubric plus its original tool/state/schema support, never fixes."""
    source = bundle / UPSTREAM
    files = [
        path for directory in ("rubric", "tools", "schema", "utils")
        for path in (source / "automationbench" / directory).rglob("*") if path.is_file()
    ]
    files.extend(source / "automationbench" / name
                 for name in ("task_contract.py", "tool_wrapper.py"))
    if not all(path.is_file() for path in files):
        return {}
    if not (source / "automationbench/rubric/__init__.py").is_file():
        return {}
    return {str(path.relative_to(source)): path.read_text() for path in sorted(files)}


def specs(bundle: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield all 800 tasks, including explicit status for undefined original sides.

    Only the returned ``auditor_input`` block is auditor-facing. Original business
    fields such as assertion ``scored`` and application-record ``status`` retain
    their benchmark meaning; the identically named audit CSV columns never enter.
    """
    bundle = Path(bundle)
    with (bundle / "tasks.csv").open(newline="") as stream:
        tasks = list(csv.DictReader(stream))
    with (bundle / "fixes.csv").open(newline="") as stream:
        fixes = {row["task_name"]: row for row in csv.DictReader(stream)}
    originals = original_tasks(bundle)
    material = grader_material(bundle)
    for task in tasks:
        name = task["task_name"]
        fix = fixes.get(name)
        state = "defective_verified" if fix is not None else "unchanged_not_certified"
        upstream = originals.get(name)
        evidence_original = None
        if fix is not None:
            folder = Path(fix["evidence_folder"])
            if folder.is_absolute() or ".." in folder.parts or folder.parts[0] != "evidence_verified":
                raise ValueError("unsafe evidence-folder metadata")
            path = bundle / folder / "original_task.json"
            if path.is_file():
                evidence_original = json.loads(path.read_text())
                if (evidence_original["task_name"] != name
                        or str(evidence_original["example_id"]) != task["example_id"]):
                    raise ValueError("original evidence identity mismatch")
        original = evidence_original or upstream
        task_available = (
            original is not None and upstream is not None
            and isinstance(original.get("prompt"), list)
            and isinstance(original.get("info"), dict)
        )
        status = (
            "ready" if task_available and material else
            "original_task_unavailable" if not task_available and material else
            "original_grader_unavailable" if task_available else
            "original_task_and_grader_unavailable"
        )
        auditor_input: dict[str, Any] = {
            "repository": "https://github.com/zapier/AutomationBench",
            "revision": REVISION,
        }
        if task_available:
            auditor_input.update(
                prompt=original["prompt"], answer=upstream["answer"],
                info={key: value for key, value in original["info"].items() if key != "task_name"},
            )
        if material:
            auditor_input["grader_material"] = material
        yield {
            "auditor_input": auditor_input,
            "auditor_input_status": status,
            "gold": {"label_state": state},
            "labels": {
                "label_state": state, "issue_group": fix["issue_group"] if fix else None,
                "issue_type": fix["issue_type"] if fix else None,
                "domain": task["domain"], "scored": task["scored"] == "True",
            },
            "identity": {
                "task_name": name, "example_id": task["example_id"],
                "domain": task["domain"], "cluster_key": task["domain"],
            },
            "reconstruction": {
                "original_task_available": task_available,
                "original_grader_available": bool(material),
                "from_evidence_original": evidence_original is not None,
                "evidence_original_matches_upstream": evidence_original is not None and upstream is not None
                and all(evidence_original[key] == upstream[key] for key in ("example_id", "prompt", "info")),
            },
        }


def selfcheck(bundle: Path = ROOT) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    states: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    families: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    scored: Counter[str] = Counter()
    names = set()
    for spec in specs(bundle):
        counts["rows"] += 1
        labels = spec["labels"]
        state = labels["label_state"]
        states[state] += 1
        statuses[spec["auditor_input_status"]] += 1
        domains[labels["domain"]] += 1
        scored["scored" if labels["scored"] else "simple"] += 1
        if labels["issue_group"] is not None:
            families[labels["issue_group"]] += 1
        original = spec["reconstruction"]
        for key, value in original.items():
            counts[key] += int(value)
        both = original["original_task_available"] and original["original_grader_available"]
        if state == "defective_verified":
            counts["verified_with_original_task"] += original["original_task_available"]
            counts["verified_with_original_grader"] += original["original_grader_available"]
            counts["verified_with_both_original_sides"] += both
            counts["verified_with_undefined_original_side"] += not both
        payload = spec["auditor_input"]
        counts["auditor_input_label_fields"] += len(set(payload) - AUDITOR_FIELDS)
        counts["auditor_input_label_fields"] += len(set(payload.get("info", {})) - INFO_FIELDS)
        counts["auditor_input_label_fields"] += len(set(payload) & set(FIELDS_AUDITOR_MUST_NOT_SEE))
        if spec["auditor_input_status"] == "ready" and set(payload) != AUDITOR_FIELDS:
            raise ValueError("ready task is missing an original input component")
        names.add(spec["identity"]["task_name"])
    if counts["auditor_input_label_fields"]:
        raise ValueError("label metadata entered auditor input")
    if counts["rows"] != 800 or len(names) != 800:
        raise ValueError("unexpected corpus row count or duplicate task identity")
    if states != {"defective_verified": 210, "unchanged_not_certified": 590}:
        raise ValueError("unexpected two-state label counts")
    return {
        "counts": dict(sorted(counts.items())), "label_counts": dict(sorted(states.items())),
        "auditor_input_status_counts": dict(sorted(statuses.items())),
        "issue_group_counts": dict(sorted(families.items())),
        "domain_counts": dict(sorted(domains.items())), "scored_counts": dict(sorted(scored.items())),
        "unique_task_names": len(names), "issues": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selfcheck"])
    parser.add_argument("--bundle", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = selfcheck(args.bundle)
    except Exception:
        raise SystemExit("adapter selfcheck failed; corpus contents suppressed") from None
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
