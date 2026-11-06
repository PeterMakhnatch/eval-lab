#!/usr/bin/env python3
"""DEV-only strict candidate extension and sequential, non-agent Docker controls."""
from __future__ import annotations

import argparse
import ast
import difflib
import importlib.util
import json
import re
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREVIOUS = ROOT / "research/experiments/ov1106-impscale/strict/extend.py"
SAVED = Path("/private/tmp/ov1106/impscale/helpers/strict/work")
SAVED_GENERATOR = Path("/private/tmp/ov1106/impscale/source/extend.py")
OWNED = Path("/private/tmp/ev1105/impmore/generator")
DEFAULT_OUT = OWNED / "work"
GENERATION = "ev1105-impmore-strict-bool-v1"


def load_previous():
    spec = importlib.util.spec_from_file_location("impmore_strict", PREVIOUS)
    strict = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strict)
    return strict


STRICT = load_previous()
digest, save = STRICT.digest, STRICT.save


def functions(tree):
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    found = [(node, "") for node in tree.body if isinstance(node, kinds)]
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            found.extend((node, cls.name) for node in cls.body if isinstance(node, kinds))
    return [(node, cls) for node, cls in found if node.name.startswith("test")]


def span(fn):
    return min([fn.lineno, *(d.lineno for d in fn.decorator_list)]), fn.end_lineno


def touched(before: str, after: str) -> set[tuple[str, str]]:
    """Include decorator edits and deletion-only edits without descending suites."""
    try:
        old, new = functions(ast.parse(before)), functions(ast.parse(after))
    except SyntaxError:
        return set()
    selected = set()
    changes = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    for tag, i1, i2, j1, j2 in changes.get_opcodes():
        if tag == "equal":
            continue
        for nodes, start, end in ((old, i1, i2), (new, j1, j2)):
            for fn, cls in nodes:
                first, last = span(fn)
                if start < last and end >= first and start != end:
                    selected.add((cls, fn.name))
    return selected


def candidates(src: str, selected: set[tuple[str, str]]) -> list[dict]:
    """Use the previous expression whitelist, but admit touched async functions."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines, found = src.splitlines(), []
    for fn, cls in functions(tree):
        if (cls, fn.name) not in selected:
            continue
        for stmt in fn.body:
            if lines[stmt.lineno - 1][:stmt.col_offset].strip():
                continue
            expression, shape = None, None
            if isinstance(stmt, ast.Assert):
                expression, shape = stmt.test, "assert-condition"
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call, target = stmt.value, stmt.value.func
                if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id == "self" and not any(isinstance(a, ast.Starred) for a in call.args)
                        and all(k.arg == "msg" for k in call.keywords)):
                    continue
                if target.attr in ("assertTrue", "assertFalse") and 1 <= len(call.args) <= 2:
                    expression = call.args[0]
                    if target.attr == "assertFalse":
                        expression = ast.UnaryOp(op=ast.Not(), operand=expression)
                    shape = target.attr
                elif target.attr in ("assertEqual", "assertEquals") and 2 <= len(call.args) <= 3:
                    expression = ast.Compare(left=call.args[0], ops=[ast.Eq()], comparators=[call.args[1]])
                    shape = target.attr
            if expression is None or any(isinstance(n, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr))
                                         for n in ast.walk(expression)):
                continue
            found.append({"func": fn.name, "class": cls, "shape": shape,
                          "stmt": [stmt.lineno, stmt.col_offset, stmt.end_lineno, stmt.end_col_offset],
                          "expression": ast.unparse(expression),
                          "decorators": [ast.unparse(d) for d in fn.decorator_list],
                          "async": isinstance(fn, ast.AsyncFunctionDef)})
    return found


# Observation only: the plugin sets the standard JUnit output option before the
# built-in pytest plugin configures it. It changes no items, reports, or exit code.
# A separate XML per pytest process avoids overwrites in multi-command scripts.
JUNIT_PLUGIN = '''import uuid
import pytest

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.option.xmlpath = "/work/junit/" + uuid.uuid4().hex + ".xml"
'''


def grade(module, meta: dict, tree: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    (out / "junit").mkdir()
    instrumentation = out / "instrumentation"
    instrumentation.mkdir()
    (instrumentation / "impmore_junit.py").write_text(JUNIT_PLUGIN)
    module.tar_tree(tree, out / "tree.tar")
    script = ('export PYTHONPATH="/work/instrumentation${PYTHONPATH:+:$PYTHONPATH}"\n'
              'export PYTEST_PLUGINS="impmore_junit${PYTEST_PLUGINS:+,$PYTEST_PLUGINS}"\n'
              + module.grade_script([], meta["test_command"], "G1", meta.get("workdir", "/testbed")))
    (out / "grade.sh").write_text(script)
    name = "impmore-control-" + uuid.uuid4().hex[:16]
    argv = ["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "3g", "--cpus", "2",
            "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{out}:/work", meta["image"], "bash", "/work/grade.sh"]
    save(out / "launch.json", {"argv": argv, "kind": "local-non-agent-control",
                              "instrumentation_sha256": digest(JUNIT_PLUGIN),
                              "test_command": meta["test_command"], "command_changed": False})
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=960)
        rc_path = out / "rc"
        result = {"rc": int(rc_path.read_text().strip()) if rc_path.exists() else None,
                  "docker_rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except (subprocess.TimeoutExpired, ValueError) as exc:
        result = {"rc": None, "docker_rc": None, "error": type(exc).__name__}
        cleanup = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)
        save(out / "timeout-cleanup.json", {"rc": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr})
    result.update({"elapsed_s": round(time.monotonic() - started, 3), "test_cases": [],
                   "test_command_sha256": digest(meta["test_command"]), "image": meta["image"],
                   "instrumentation_sha256": digest(JUNIT_PLUGIN), "junit_files": {}})
    try:
        xml_files = sorted((out / "junit").glob("*.xml"))
        if not xml_files:
            raise FileNotFoundError("No actual JUnit files")
        for junit in xml_files:
            root = ET.parse(junit).getroot()
            if root.tag not in ("testsuites", "testsuite"):
                raise ValueError("Not a JUnit document")
            result["junit_files"][str(junit.relative_to(out))] = digest(junit.read_bytes())
            for case in root.iter("testcase"):
                failure = case.find("failure")
                result["test_cases"].append({"name": case.get("name", ""), "classname": case.get("classname", ""),
                    "failed": failure is not None, "error": case.find("error") is not None,
                    "skipped": case.find("skipped") is not None,
                    "assertion_failure": failure is not None and (failure.get("type") == "AssertionError"
                        or (failure.get("message") or "").startswith(("assert ", "AssertionError"))),
                    "failure_text": "" if failure is None else " ".join([failure.get("message", ""), failure.text or ""])})
        result["junit_error"] = None
    except (OSError, ET.ParseError, ValueError) as exc:
        result["junit_error"] = f"{type(exc).__name__}: {exc}"
    (out / "tree.tar").unlink(missing_ok=True)
    save(out / "grade.json", result)
    return result


def bindings(module, row):
    meta = row["meta"]
    return {"test_command_sha256": digest(meta["test_command"]),
            "reference_sha256": digest(Path(meta["reference"]).read_bytes()),
            "source_patch_sha256": digest((Path(meta["package"]) / "tests/test.patch").read_bytes()),
            "specification_sha256": digest((Path(meta["package"]) / "instruction.md").read_bytes()),
            "task_config_sha256": digest((Path(meta["package"]) / "task.toml").read_bytes())}


def inspect(module, out):
    # Authority must precede every task-content read, including saved evidence.
    authority = module.candidates()
    rows = []
    for current in authority:
        tid = current["task_id"]
        source = STRICT.SOURCE / tid
        path = source / "meta.json"
        row = {"meta": current, "source": str(source), "candidates": [], "old_generated": False}
        if not path.exists():
            row["unavailable"] = "missing-extraction"
        else:
            old = json.loads(path.read_text())
            row.update({"meta": {**old, **current}, "source_meta_sha256": digest(path.read_bytes())})
            if old.get("split") != "dev" or not old.get("extract_ok"):
                row["unavailable"] = "extraction-failed" if not old.get("extract_ok") else "source-not-dev"
            else:
                for rel in module.patched_files((Path(current["package"]) / "tests/test.patch").read_text()):
                    patched, base = source / "patched" / rel, source / "base" / rel
                    if patched.is_file() and patched.suffix == ".sh":
                        for number, line in enumerate(patched.read_text().splitlines(), 1):
                            for assignment in re.finditer(r"\bPYTHONPATH\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s;]+)", line):
                                if "PYTHONPATH" not in assignment.group(1):
                                    row["instrumentation_unavailable"] = (
                                        f"{rel}:{number} replaces PYTHONPATH; refusing plugin injection "
                                        "rather than changing the task exit behavior")
                    if not patched.is_file() or patched.suffix != ".py":
                        continue
                    src = patched.read_text()
                    before = base.read_text() if base.exists() else ""
                    for candidate in candidates(src, touched(before, src)):
                        row["candidates"].append({**candidate, "file": rel, "source_sha256": digest(src)})
                generated = source / "generated.json"
                row["old_generated"] = generated.exists() and json.loads(generated.read_text()).get("ok", False)
                row["bindings"] = bindings(module, row)
        rows.append(row)
    save(out / "inventory.json", {"generation": GENERATION, "extension_sha256": digest(Path(__file__).read_bytes()),
        "previous_generator_sha256": digest(PREVIOUS.read_bytes()),
        "original_generator_sha256": digest(STRICT.ORIGINAL.read_bytes()),
        "authority_count": len(authority), "tasks": rows})
    return rows


def reuse(module, row):
    """Recheck saved admission bindings and proof, without rerunning old controls."""
    tid = row["meta"]["task_id"]
    dest = SAVED / "tasks-v2" / tid
    generated = json.loads((dest / "generated.json").read_text())
    controls = json.loads((dest / "controls.json").read_text())
    if generated["generator_sha256"] != digest(SAVED_GENERATOR.read_bytes()):
        raise ValueError("Saved generator snapshot changed")
    proof_functions = {"transform", "identity", "selected", "clean", "grade", "prepare_task"}
    def proof_ast(path):
        return {node.name: ast.dump(node, include_attributes=False)
                for node in ast.parse(path.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name in proof_functions}
    if proof_ast(SAVED_GENERATOR) != proof_ast(PREVIOUS):
        raise ValueError("Saved and current proof functions differ")
    if generated["original_generator_sha256"] != digest(STRICT.ORIGINAL.read_bytes()):
        raise ValueError("Original generator source changed")
    if generated["source_meta_sha256"] != row["source_meta_sha256"]:
        raise ValueError("Saved extraction changed")
    for key, value in row["bindings"].items():
        if generated[key] != value:
            raise ValueError(f"Saved binding changed: {key}")
    image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", row["meta"]["image"]],
                           capture_output=True, text=True, check=True).stdout.strip()
    if generated["label_evidence_template"]["image"] != image:
        raise ValueError("Saved image changed")
    if controls["generated_sha256"] != digest((dest / "generated.json").read_bytes()):
        raise ValueError("Saved control binding changed")
    for variant, hashes in generated["variant_test_sha256"].items():
        for rel, expected in hashes.items():
            if digest((dest / "variants" / variant / rel).read_bytes()) != expected:
                raise ValueError("Saved variant bytes changed")
            if variant == "original" and digest((Path(row["source"]) / "patched" / rel).read_bytes()) != expected:
                raise ValueError("Original patched bytes changed")
    receipts = [dest / "controls/original-oracle/grade.json", dest / "controls/original-nop/grade.json",
                dest / controls["conflict_control"]]
    reference, nop, conflict = [json.loads(p.read_text()) for p in receipts]
    for path, receipt in zip(receipts, (reference, nop, conflict), strict=True):
        if receipt["junit_sha256"] != digest((path.parent / "control-junit.xml").read_bytes()):
            raise ValueError("Saved JUnit bytes changed")
        if receipt["test_command_sha256"] != row["bindings"]["test_command_sha256"] or receipt["image"] != image:
            raise ValueError("Saved receipt identity changed")
    candidate = generated["candidate"]
    expected = STRICT.selected(reference["test_cases"], candidate)
    actual = STRICT.selected(conflict["test_cases"], candidate)
    marker = controls["selected_test_proof"]["marker"]
    if not (STRICT.clean(reference, 0) and not any(c["failed"] for c in reference["test_cases"])
            and STRICT.clean(nop, 1) and any(c["assertion_failure"] for c in nop["test_cases"])
            and STRICT.clean(conflict, 1) and expected
            and sorted(map(STRICT.identity, expected)) == sorted(map(STRICT.identity, actual))
            and all(c["assertion_failure"] and marker + ":second" in c["failure_text"] for c in actual)):
        raise ValueError("Saved admission proof failed")
    return {"task_id": tid, "admitted": True, "image": image, "shape": candidate["shape"],
            "class": candidate["class"], "reused": True, "source": str(dest),
            "saved_generator_source": str(SAVED_GENERATOR),
            "saved_generator_sha256": generated["generator_sha256"],
            "current_generator_sha256": digest(PREVIOUS.read_bytes()),
            "proof_functions_ast_identical": True,
            "source_generated_sha256": digest((dest / "generated.json").read_bytes()),
            "source_controls_sha256": digest((dest / "controls.json").read_bytes()),
            "source_receipts": {str(p): digest(p.read_bytes()) for p in receipts},
            "current_split_assignments_digest": row["meta"]["split_assignments_digest"],
            "construction_control": "not-executed-for-reused-admissions; parent-review-required"}


def construction(module, row, out):
    """Known test edit: change only the contradictory second literal to True."""
    dest = out / "tasks-v2" / row["meta"]["task_id"]
    generated = json.loads((dest / "generated.json").read_text())
    meta = json.loads((dest / "meta.json").read_text())
    candidate = generated["candidate"]
    tree = dest / "construction-tree"
    shutil.copytree(Path(row["source"]) / "base", tree, symlinks=True)
    try:
        subprocess.run(["git", "apply", "--whitespace=nowarn", meta["reference"]], cwd=tree,
                       capture_output=True, text=True, check=True)
        for rel in generated["test_files"]:
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dest / "variants" / STRICT.VARIANT / rel, tree / rel)
        target = tree / candidate["file"]
        strict_source = target.read_text()
        marker = generated["label_evidence_template"]["proof"]["marker"]
        second = next(line for line in generated["conflict_stmt"].splitlines() if marker + ":second" in line).strip()
        if strict_source.count(second) != 1 or " == False," not in second:
            raise ValueError("Construction edit is not unique")
        submitted = strict_source.replace(second, second.replace(" == False,", " == True,"), 1)
        target.write_text(submitted)
        positive = grade(module, meta, tree, dest / "controls/construction-submitted")
        target.write_text(strict_source)
        restored = grade(module, meta, tree, dest / "controls/construction-restored")
        baseline = json.loads((dest / "controls/original-oracle/grade.json").read_text())
        identities = sorted(map(STRICT.identity, STRICT.selected(baseline["test_cases"], candidate)))
        actual = STRICT.selected(restored["test_cases"], candidate)
        valid = (STRICT.clean(positive, 0) and not any(c["failed"] for c in positive["test_cases"])
                 and sorted(map(STRICT.identity, STRICT.selected(positive["test_cases"], candidate))) == identities
                 and STRICT.clean(restored, 1) and identities
                 and sorted(map(STRICT.identity, actual)) == identities
                 and all(c["assertion_failure"] and marker + ":second" in c["failure_text"] for c in actual))
        receipt = {"verified": bool(valid), "agent_run": False, "mutated_file": candidate["file"],
                   "edit": "second assertion literal False -> True", "submitted_sha256": digest(submitted),
                   "restored_sha256": digest(strict_source), "selected_identities": identities,
                   "submitted_receipt": "controls/construction-submitted/grade.json",
                   "restored_receipt": "controls/construction-restored/grade.json"}
        save(dest / "construction-control.json", receipt)
        return bool(valid)
    finally:
        shutil.rmtree(tree)


def controls(module, out, max_candidates, task_ids):
    inventory = json.loads((out / "inventory.json").read_text())
    if inventory["extension_sha256"] != digest(Path(__file__).read_bytes()):
        raise ValueError("Generator changed; inspect into a fresh output directory")
    authority = {r["task_id"]: r for r in module.candidates()}
    saved = {r["task_id"]: r for r in json.loads((SAVED / "results.json").read_text())}
    results = []
    STRICT.GENERATION = GENERATION
    STRICT.grade = grade
    # Existing prepare_task keeps its original selection/marker/primitive proof.
    # Bind emitted generator hashes to this extension, not to the imported file.
    STRICT.__file__ = str(Path(__file__).resolve())
    for row in inventory["tasks"]:
        tid = row["meta"]["task_id"]
        if tid not in authority or authority[tid]["split_assignments_digest"] != row["meta"]["split_assignments_digest"]:
            raise ValueError("DEV authority changed")
        result_path = out / "task-results" / (tid + ".json")
        if result_path.exists():
            results.append(json.loads(result_path.read_text()))
            continue
        if task_ids and tid not in task_ids and not saved.get(tid, {}).get("admitted"):
            results.append({"task_id": tid, "admitted": False, "reason": "not-executed-by-task-filter"})
            continue
        try:
            if row.get("unavailable"):
                result = {"task_id": tid, "admitted": False, "reason": row["unavailable"]}
            elif saved.get(tid, {}).get("admitted"):
                result = reuse(module, row)
                target = out / "tasks-v2" / tid
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(result["source"], target_is_directory=True)
            elif not row["candidates"]:
                result = {"task_id": tid, "admitted": False, "reason": "no-supported-direct-assertion-in-touched-test"}
            elif row.get("instrumentation_unavailable"):
                result = {"task_id": tid, "admitted": False, "reason": "junit-instrumentation-unavailable",
                          "detail": row["instrumentation_unavailable"]}
            else:
                if bindings(module, row) != row["bindings"]:
                    raise ValueError("Task bindings changed since inspection")
                result = STRICT.prepare_task(module, row, out, max_candidates)
                result["reused"] = False
                if result["admitted"]:
                    result["construction_control_verified"] = construction(module, row, out)
                    if not result["construction_control_verified"]:
                        result.update({"admitted": False, "reason": "construction-control-failed"})
        except Exception as exc:
            result = {"task_id": tid, "admitted": False, "reason": type(exc).__name__, "detail": str(exc)}
        results.append(result)
        save(result_path, result)
        print(json.dumps(result, sort_keys=True), flush=True)
    # A fresh snapshot name permits bounded --task passes without overwriting evidence.
    summary = {"authority_count": inventory["authority_count"], "admitted_union": sum(r["admitted"] for r in results),
               "reused_admissions": sum(r["admitted"] and r.get("reused", False) for r in results),
               "new_ids": [r["task_id"] for r in results if r["admitted"] and not r.get("reused")],
               "failure_counts": dict(Counter(r.get("reason") for r in results if not r["admitted"])),
               "results": results}
    path = out / ("summary-" + uuid.uuid4().hex[:12] + ".json")
    save(path, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, sort_keys=True), flush=True)
    print(str(path), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("inspect", "controls"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument("--task", action="append", default=[], help="Execute only these new task IDs; reuse all saved admissions")
    args = parser.parse_args()
    out = args.out.resolve()
    if not out.is_relative_to(OWNED) or not 1 <= args.max_candidates <= 10:
        parser.error("Outputs must stay under the owned generator directory; candidates must be 1..10")
    module = STRICT.original_module()
    if args.stage == "inspect":
        rows = inspect(module, out)
        print(json.dumps({"tasks": len(rows), "with_candidates": sum(bool(r["candidates"]) for r in rows),
                          "candidates": sum(len(r["candidates"]) for r in rows),
                          "unavailable": {r["meta"]["task_id"]: r["unavailable"] for r in rows if r.get("unavailable")}}, sort_keys=True))
    else:
        controls(module, out, args.max_candidates, set(args.task))


if __name__ == "__main__":
    main()
