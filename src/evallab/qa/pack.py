"""Static regression-pack admission and native QA replay; inputs stay immutable."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import math
import re
import shutil
import tempfile
import tomllib
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.qa.models import Finding
from evallab.qa.task import finite_reward, utcnow, write_json
from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _path(root: Path, value: Any, *, exists: bool = True) -> Path:
    _require(_text(value), "pack path must be a nonempty relative path")
    raw = PurePosixPath(value)
    _require(not raw.is_absolute() and ".." not in raw.parts and "\\" not in value
             and ":" not in value and raw.parts, f"unsafe relative path: {value}")
    path = root.joinpath(*raw.parts)
    _require(path.resolve().is_relative_to(root.resolve()), f"path escapes root: {value}")
    if exists:
        _require(path.is_file(), f"missing pack/task file: {value}")
    return path


def _tree(root: Path) -> None:
    _require(root.is_dir(), f"directory is missing: {root}")
    for path in root.rglob("*"):
        _require(path.resolve().is_relative_to(root), f"symlink escapes directory: {path}")
        _require(path.is_file() or path.is_dir(), f"unsupported file: {path}")


def _separate(dest: Path, *sources: Path) -> None:
    for source in sources:
        _require(not dest.is_relative_to(source) and not source.is_relative_to(dest),
                 "staging/run directory must not overlap pack or task")
    _require(not dest.exists() and not dest.is_symlink(), "destination must be fresh")


def _patch_files(patch: Path) -> list[tuple[str | None, str, list[str]]]:
    lines = patch.read_text(encoding="utf-8").splitlines(keepends=True)
    _require(lines and not any("\x00" in line for line in lines), "empty or binary patch")
    files: list[tuple[str | None, str, list[str]]] = []
    index = 0
    pending: str | None = None
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            parts = line.rstrip().split()
            _require(len(parts) == 4 and parts[2].startswith("a/")
                     and parts[3] == "b/" + parts[2][2:], "rename or unsafe patch header")
            pending = parts[2][2:]
            name = PurePosixPath(pending)
            _require(name.parts and name.parts[0] == "tests" and len(name.parts) > 1
                     and ".." not in name.parts and "\\" not in pending and ":" not in pending,
                     "diff header must touch only relative tests/ files")
            index += 1
            continue
        if re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644| 100755)?\n?", line):
            index += 1
            continue
        _require(line.startswith("--- "), "unsupported patch metadata (mode/binary/rename)")
        _require(index + 1 < len(lines) and lines[index + 1].startswith("+++ "),
                 "patch needs paired file headers")
        old = line[4:].rstrip("\r\n").split("\t")[0]
        new = lines[index + 1][4:].rstrip("\r\n").split("\t")[0]
        old = None if old == "/dev/null" else old.removeprefix("a/")
        new = new.removeprefix("b/")
        _require(new != "/dev/null" and (old is None or old == new),
                 "file deletion and rename are forbidden")
        _require(pending is None or new == pending, "diff and unified file headers disagree")
        pending = None
        for name in (old, new):
            if name is not None:
                pure = PurePosixPath(name)
                _require(pure.parts and pure.parts[0] == "tests" and len(pure.parts) > 1
                         and ".." not in pure.parts and "\\" not in name and ":" not in name,
                         "patch must touch only relative tests/ files")
        index += 2
        start = index
        while index < len(lines) and not lines[index].startswith(("diff --git ", "--- ")):
            index += 1
        _require(start < index, "patch file has no hunks")
        files.append((old, new, lines[start:index]))
    _require(pending is None and files and len({new for _, new, _ in files}) == len(files),
             "patch must change each file only once")
    return files


def _no_weakening(files: list[tuple[str | None, str, list[str]]], claim: str) -> None:
    reward_defect = bool(re.search(r"reward[ -]+writ(?:er|ing)", claim, re.I)
                         and re.search(r"\b(?:defect|bug|incorrect|wrong|broken)\b", claim, re.I))
    protected = re.compile(
        r"\bassert\w*\b|\bdef\s+test\w*|\btest\b|\btest_\w+|^\s*(?:if\s+)?\[|\b(?:cmp|diff)\s"
        r"|\b(?:pytest|unittest|nose2|nosetests|ctest|tox|nox|jest|mocha|vitest|bats|prove)\b", re.I)
    bypass = re.compile(
        r"\bskip\w*\b|\bxfail\w*\b|\|\|\s*(?:true|:)|\breturn\b"
        r"|\bexit\s+(?:0+|\"0+\"|'0+')(?:\s|;|$)"
        r"|\b(?:sys|os)\s*\.\s*_?exit\s*\(\s*0+\s*\)", re.I)
    tolerance = re.compile(r"\b(?:\w*(?:tol|threshold|precision|limit)\w*|epsilon|delta|places|approx|isclose)\b", re.I)
    for _, _, hunks in files:
        removed = [line[1:].rstrip() for line in hunks if line.startswith("-")]
        added = [line[1:].rstrip() for line in hunks if line.startswith("+")]
        deleted, inserted = removed, added
        _require(not any(protected.search(line) for line in deleted),
                 "patch deletes an assertion or test")
        _require(not any(bypass.search(line) for line in inserted),
                 "patch adds skip, xfail, || true, early return, or early exit 0")
        _require(not any(tolerance.search(line) for line in deleted + inserted),
                 "patch changes a tolerance; broadening is forbidden")
        reward_context = any(re.search(r"reward", line, re.I) for line in hunks)
        writer = re.compile(r"reward|\b(?:write\w*|echo|printf|tee|open)\b", re.I)
        _require(reward_defect or not any(re.search(r"reward", line, re.I)
                 or (reward_context and writer.search(line)) for line in deleted + inserted),
                 "patch changes reward writer without an explicit reward writer defect claim")


def _assertions(path: Path) -> Counter[str]:
    """Keep multiline assertions and assertion helpers intact, not just their first lines."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise ValueError(f"invalid Python test file: {path}") from exc
    result: Counter[str] = Counter()
    for node in ast.walk(tree):
        assertion = isinstance(node, ast.Assert)
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            assertion = name.startswith("assert") or name in {"raises", "fail"}
        if assertion:
            result[ast.dump(node)] += 1
    return result


def _apply(task: Path, patch: Path) -> None:
    for old, new, hunks in _patch_files(patch):
        target = _path(task, new, exists=old is not None)
        _require(not any(part.is_symlink() for part in [target, *target.parents]
                         if part.is_relative_to(task)), "patch target must not be a symlink")
        _require(old is not None or not target.exists(), "new patch file already exists")
        source = target.read_text(encoding="utf-8").splitlines(keepends=True) if old else []
        output: list[str] = []
        cursor = index = 0
        while index < len(hunks):
            header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?", hunks[index])
            _require(header is not None, "invalid unified patch hunk")
            assert header is not None
            old_start, old_count, new_start, new_count = (
                int(value) if value is not None else 1 for value in header.groups())
            position = old_start - 1 if old_count else old_start
            _require(cursor <= position <= len(source), "patch hunk offset does not apply")
            output.extend(source[cursor:position])
            cursor = position
            _require(len(output) == (new_start - 1 if new_count else new_start),
                     "patch new-file offset does not apply")
            index += 1
            consumed = produced = 0
            while index < len(hunks) and not hunks[index].startswith("@@ "):
                line = hunks[index]
                _require(line[:1] in (" ", "+", "-"), "invalid patch hunk line")
                content = line[1:]
                if index + 1 < len(hunks) and hunks[index + 1].startswith("\\ No newline at end of file"):
                    content = content.removesuffix("\n")
                    index += 1
                if line[0] in " -":
                    _require(cursor < len(source) and source[cursor] == content,
                             "patch context does not apply exactly")
                    cursor += 1
                    consumed += 1
                if line[0] in " +":
                    output.append(content)
                    produced += 1
                index += 1
            _require((consumed, produced) == (old_count, new_count), "patch hunk counts differ")
        output.extend(source[cursor:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(output), encoding="utf-8")


def stage_fixed(task_dir: Path, patch: Path, dest: Path) -> Path:
    """Apply a safe unified patch to a fresh copy, never to its source package."""
    task, patch, dest = Path(task_dir).resolve(), Path(patch).resolve(), Path(dest).resolve()
    pack = patch.parent.parent if patch.parent.name == "fix" else patch.parent
    _separate(dest, task, pack)
    _tree(task)
    _require(not any(path.is_symlink() and path.is_dir() for path in task.rglob("*")),
             "cannot stage task directory symlinks")
    for old, new, _ in _patch_files(patch):
        target = _path(task, new, exists=old is not None)
        _require(not any(part.is_symlink() for part in [target, *target.parents]
                         if part.is_relative_to(task)), "patch target must not be a symlink")
    shutil.copytree(task, dest)
    _apply(dest, patch)
    return dest


def _required(manifest: dict[str, Any], witness: str) -> set[str]:
    cells = manifest["cells"]
    required: set[str] = set()

    def need(submission: str, grader: str, expect: Any, check: bool = False, count: int = 1) -> None:
        def same_submission(value: str) -> bool:
            if submission in {"oracle", "nop"} or value in {"oracle", "nop"}:
                return submission == value
            return PurePosixPath(submission) == PurePosixPath(value)

        matches = [cell["id"] for cell in cells if same_submission(cell["submission"])
                   and cell["grader"] == grader and cell["expect"] == expect
                   and bool(cell["check"]) == check
                   and (not check or PurePosixPath(cell["check"]) == PurePosixPath("check.sh"))]
        _require(len(matches) >= count,
                 f"missing required cell: {submission}/{grader}/{expect} (need {count})")
        required.update(matches)

    defect = manifest["defect_class"]
    need("oracle", "original", 0 if defect == "reference_fails" else 1,
         count=3 if defect == "reference_fails" else 1)
    if defect != "reference_fails":
        need("nop", "original", 1 if defect == "nop_passes" else 0)
    if defect in {"wrong_pass", "answer_reachable", "verifier_tamper"}:
        need(witness, "original", 1)
    if defect == "wrong_pass":
        need("oracle", "none", "pass", True)
        need(witness, "none", "fail", True)
    if manifest["fix"] is not None and defect in {"wrong_pass", "nop_passes"}:
        need("oracle", "fixed", 1)
        need("nop" if defect == "nop_passes" else witness, "fixed", 0)
        if defect == "wrong_pass":
            need("alt.sh", "original", 1)
            need("alt.sh", "fixed", 1)
            need("alt.sh", "none", "pass", True)
    return required


def validate_pack(pack_dir: Path, task_dir: Path) -> dict[str, Any]:
    """Validate the complete pack, including an exact static patch application."""
    pack, task = Path(pack_dir).resolve(), Path(task_dir).resolve()
    _tree(pack)
    _tree(task)
    manifest = json.loads(_path(pack, "pack.json").read_text(encoding="utf-8"))
    fields = {"schema", "task", "defect_class", "layer", "claim", "requirement", "prior_reports",
              "cells", "fix", "authors", "status"}
    _require(isinstance(manifest, dict) and set(manifest) == fields, "invalid pack manifest fields")
    _require(manifest["schema"] == "qa.pack/v1", "invalid pack schema")
    _require(_text(manifest["defect_class"]) and manifest["defect_class"] in {
        "wrong_pass", "answer_reachable", "verifier_tamper", "reference_fails", "nop_passes"},
        "invalid defect class")
    _require(_text(manifest["layer"]) and manifest["layer"] in {
        "task_package", "adapter", "harness", "host"}, "invalid layer")
    _require(_text(manifest["status"]) and manifest["status"] in {
        "candidate", "confirmed", "verified", "refuted"}, "invalid status")
    _require(_text(manifest["claim"]), "claim is required")
    identity = manifest["task"]
    _require(isinstance(identity, dict) and set(identity) == {"dataset", "version", "task_id",
             "package_digest", "source_url", "source_commit"}
             and all(_text(value) for value in identity.values()), "invalid task identity")
    _require(identity["package_digest"] == task_directory_digest(task), "task package digest mismatch")
    requirement = manifest["requirement"]
    _require(isinstance(requirement, dict) and set(requirement) == {"file", "lines", "quote"}
             and _text(requirement["file"]) and requirement["file"] in {"instruction.md", "task.toml"}
             and _text(requirement["quote"]), "requirement must quote instruction.md or task.toml")
    bounds = requirement["lines"]
    _require(isinstance(bounds, list) and len(bounds) == 2
             and all(type(value) is int for value in bounds) and 1 <= bounds[0] <= bounds[1],
             "invalid requirement line range")
    source = _path(task, requirement["file"]).read_text(encoding="utf-8").splitlines()
    _require(bounds[1] <= len(source) and requirement["quote"] in "\n".join(source[bounds[0]-1:bounds[1]]),
             "requirement quote does not match the cited task lines")
    _path(pack, "requirement.md")
    _path(pack, "REPLAY.md")
    reports = manifest["prior_reports"]
    _require(isinstance(reports, list) and all(isinstance(report, dict)
             and set(report) == {"url", "date", "note"} and all(_text(v) for v in report.values())
             for report in reports), "invalid prior reports")
    authors = manifest["authors"]
    _require(isinstance(authors, dict) and set(authors) == {"witness", "fix", "verifier"}, "invalid authors")
    for role, author in authors.items():
        _require((author is None and role != "witness") or (isinstance(author, dict)
                 and set(author) == {"model", "lane"} and all(_text(v) for v in author.values())),
                 "invalid author identity")
    finding_raw = json.loads(_path(pack, "finding.json").read_text(encoding="utf-8"))
    _require(isinstance(finding_raw, dict) and finding_raw.get("schema") == "qa.finding/v1",
             "finding must explicitly use qa.finding/v1")
    finding = Finding.model_validate(finding_raw)
    subject = finding.subject
    _require(subject.kind == "task" and subject.task_id == identity["task_id"]
             and subject.task_digest == identity["package_digest"] and subject.trial_id is None,
             "finding subject does not match pack task")
    _require(finding.reproducer is not None, "finding requires a witness reproducer")
    assert finding.reproducer is not None
    witness = finding.reproducer.path
    _path(pack, witness)
    _require(PurePosixPath(witness).as_posix() not in {"oracle", "nop"},
             "finding reproducer must identify a witness script, not a reserved submission")
    for ref in finding.evidence_refs:
        _path(pack, ref)
    cells = manifest["cells"]
    _require(isinstance(cells, list) and cells, "pack cells must be nonempty")
    ids: set[str] = set()
    for cell in cells:
        _require(isinstance(cell, dict) and set(cell) == {"id", "submission", "grader", "check", "expect"},
                 "invalid cell fields")
        _require(_text(cell["id"]) and re.fullmatch(r"[A-Za-z0-9_.-]+", cell["id"])
                 and cell["id"] not in {".", ".."} and cell["id"] not in ids, "invalid/duplicate cell id")
        ids.add(cell["id"])
        _require(_text(cell["grader"]) and cell["grader"] in {"original", "fixed", "none"}, "invalid grader")
        _require(_text(cell["submission"]), "submission is required")
        if cell["submission"] not in {"oracle", "nop"}:
            _path(pack, cell["submission"])
        if cell["grader"] == "none":
            _path(pack, cell["check"])
            _require(_text(cell["expect"]) and cell["expect"] in {"pass", "fail"},
                     "check expectation must be pass/fail")
        else:
            _require(cell["check"] is None and type(cell["expect"]) is int
                     and cell["expect"] in {0, 1}, "grader expectation must be 0/1 without check")
    fix = manifest["fix"]
    if fix is None:
        _require(not any(cell["grader"] == "fixed" for cell in cells), "fixed cell requires a fix")
    else:
        _require(isinstance(fix, dict) and set(fix) == {"patch"}, "invalid fix")
        _require(authors["fix"] is not None, "a fix requires its author identity")
        patch = _path(pack, fix["patch"])
        files = _patch_files(patch)
        _no_weakening(files, manifest["claim"])
        with tempfile.TemporaryDirectory(prefix="qa-pack-validate-") as temporary:
            fixed = stage_fixed(task, patch, Path(temporary) / "task")
            for old, new, _ in files:
                if old is not None and new.endswith(".py"):
                    _require(not (_assertions(task / old) - _assertions(fixed / new)),
                             "patch removes or changes a Python assertion")
    _required(manifest, witness)
    return manifest


def _hashes(directory: Path) -> list[dict[str, str]]:
    return [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink()]


def _check_script(pack: Path, cell: dict[str, Any]) -> str:
    submission = cell["submission"]
    command = "bash /solution/solve.sh" if submission == "oracle" else ":"
    script = "#!/bin/bash\nset -e\n"
    if submission not in {"oracle", "nop"}:
        encoded = base64.b64encode(_path(pack, submission).read_bytes()).decode("ascii")
        script += f"printf %s '{encoded}' | base64 -d > /tmp/replay-submission.sh\n"
        command = "bash /tmp/replay-submission.sh"
    encoded = base64.b64encode(_path(pack, cell["check"]).read_bytes()).decode("ascii")
    script += f"{command}\nprintf %s '{encoded}' | base64 -d > /tmp/replay-check.sh\n"
    script += "set +e\nbash /tmp/replay-check.sh\ncode=$?\n"
    script += "printf '%s\\n' \"$code\" > /logs/agent/replay-check-exit-code.txt\nexit 0\n"
    return script


def _measure(observation: Any, trial: Path, check: bool) -> tuple[Any, str | None]:
    facts = observation.facts
    reason = observation.reason or facts.get("exception")
    raw = trial / "result.json"
    if raw.is_file():
        payload = json.loads(raw.read_text(encoding="utf-8"))
        reason = reason or payload.get("exception_info")
    if observation.status != "executed" or reason:
        return "unmeasured", str(reason or "native trial did not execute")
    if check:
        marker = trial / "agent" / "replay-check-exit-code.txt"
        if not marker.is_file():
            return "unmeasured", "check produced no exit-code marker"
        value = marker.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"\d+", value) or int(value) > 255 or int(value) in {124, 137, 143}:
            return "unmeasured", "check exit code is invalid or indicates timeout/termination"
        return ("pass" if int(value) == 0 else "fail"), None
    reward = finite_reward(facts.get("reward"))
    return (reward, None) if reward is not None else ("unmeasured", "native trial produced no usable reward")


def replay(pack_dir: Path, task_dir: Path, *, plan: bool = False, environment: str = "modal",
           max_usd: float | None = None, approve: str | None = None, usd_per_execution: float = 0.0025,
           run_dir: Path | None = None, cells: Any = None, executor_factory: Any = None) -> dict[str, Any]:
    """Replay selected cells with one spend gate and one fresh native trial per cell."""
    pack, task = Path(pack_dir).resolve(), Path(task_dir).resolve()
    if run_dir is not None:
        run_dir = Path(run_dir).resolve()
        _separate(run_dir, pack, task)
    manifest = validate_pack(pack, task)
    _require(environment == "modal", "pack replay supports only the native modal environment")
    _require(type(usd_per_execution) in {int, float} and math.isfinite(usd_per_execution)
             and usd_per_execution > 0, "usd_per_execution must be positive and finite")
    _require(max_usd is None or (type(max_usd) in {int, float} and math.isfinite(max_usd)
                                and max_usd > 0), "max_usd must be positive and finite")
    selected = cells.split(",") if isinstance(cells, str) else list(cells) if cells is not None else None
    known = {cell["id"] for cell in manifest["cells"]}
    _require(selected is None or (selected and len(set(selected)) == len(selected)
                                 and set(selected) <= known), "unknown, duplicate, or empty cell selection")
    chosen = [cell for cell in manifest["cells"] if selected is None or cell["id"] in selected]
    verifier = tomllib.loads(_path(task, "task.toml").read_text(encoding="utf-8")).get("verifier") or {}
    _require(isinstance(verifier, dict), "invalid task verifier configuration")
    mode = verifier.get("environment_mode")
    separate = mode == "separate" or (mode is None and verifier.get("environment") is not None)
    planned = [{**cell, "execution_upper_bound": 6 if separate and cell["grader"] != "none" else 3}
               for cell in chosen]
    units = sum(cell["execution_upper_bound"] for cell in planned)
    estimate = units * usd_per_execution
    refusals = []
    if not _text(approve):
        refusals.append("recorded --approve text is required")
    if max_usd is None or estimate > max_usd + 1e-9:
        refusals.append("reservation estimate exceeds --max-usd or cap is missing")
    cell_plan = {"schema": "evallab.qa_pack.plan/v1", "environment": environment, "cells": planned,
                 "task": manifest["task"], "execution_upper_bound": units, "estimated_usd": estimate,
                 "usd_per_execution": usd_per_execution, "max_usd": max_usd, "approval": approve,
                 "fits_cap": max_usd is not None and estimate <= max_usd + 1e-9,
                 "runnable": not refusals, "refusals": refusals, "model_calls": 0,
                 "reserved_usd": 0.0, "executed": False,
                 "budget_basis": "reservation estimates, not provider billing or a billing hard kill"}
    if plan:
        return cell_plan
    _require(not refusals, "; ".join(refusals))
    run_dir = run_dir or Path(tempfile.gettempdir()) / f"qa-pack-replay-{uuid.uuid4().hex}"
    _separate(run_dir, pack, task)
    gate = CloudSpendGate(approval=approve or "", cap_usd=max_usd or 0, usd_per_execution=usd_per_execution)
    run_dir.mkdir(parents=True)
    variants = {"original": task, "none": task}
    if manifest["fix"] is not None:
        variants["fixed"] = stage_fixed(task, _path(pack, manifest["fix"]["patch"]), run_dir / "fixed-task")
    digests = {key: task_directory_digest(path) for key, path in variants.items()}
    backend_error = None
    backend = None
    try:
        if executor_factory is None:
            from evallab.qa.task_modal import ModalBackend

            executor_factory = ModalBackend
        backend = executor_factory(repo_root=Path(__file__).resolve().parents[3], spend_gate=gate)
    except Exception as exc:
        backend_error = f"{type(exc).__name__}: {exc}"
    results = []
    for cell in chosen:
        trial = run_dir / "cells" / cell["id"]
        trial.mkdir(parents=True)
        check = cell["grader"] == "none"
        submission = cell["submission"]
        mode = "shortcut" if check or submission not in {"oracle", "nop"} else submission
        try:
            _require(backend is not None, backend_error or "native backend is unavailable")
            assert backend is not None
            script = _check_script(pack, cell) if check else (
                _path(pack, submission).read_text(encoding="utf-8") if mode == "shortcut" else None)
            observation = backend._native_trial(
                variants[cell["grader"]], digest=digests[cell["grader"]], mode=mode, script=script,
                trial_dir=trial, timeout_seconds=1800, solve_override=None,
                with_solution=check and submission == "oracle", capture_logs=True, verifier=not check)
            result, reason = _measure(observation, trial, check)
        except Exception as exc:
            result, reason = "unmeasured", f"{type(exc).__name__}: {exc}"
        row = {**cell, "result": result, "measurement": "unmeasured" if result == "unmeasured" else "measured",
               "reason": reason, "matched": result != "unmeasured" and result == cell["expect"],
               "variant_digest": digests[cell["grader"]], "native_results": _hashes(trial),
               "cloud_spend": gate.to_json()}
        results.append(row)
        write_json(run_dir / "replay.json", {"matched": False, "cells": results, "cloud_spend": gate.to_json()})
    finding = Finding.model_validate_json(_path(pack, "finding.json").read_text(encoding="utf-8"))
    assert finding.reproducer is not None
    required = _required(manifest, finding.reproducer.path)
    omitted = required - {cell["id"] for cell in chosen}
    for cell in manifest["cells"]:
        if cell["id"] in omitted:
            results.append({**cell, "result": "unmeasured", "measurement": "unmeasured",
                            "reason": "required cell omitted by --cells", "matched": False,
                            "variant_digest": digests[cell["grader"]], "native_results": [],
                            "cloud_spend": gate.to_json()})
    receipt = {"schema": "evallab.qa_pack.replay/v1", "at": utcnow(), "pack_dir": str(pack),
               "task_dir": str(task), "run_dir": str(run_dir), "plan": cell_plan, "cells": results,
               "package_digest": manifest["task"]["package_digest"], "variant_digests": digests,
               "pack_files": _hashes(pack), "cloud_spend": gate.to_json(),
               "omitted_required_cells": sorted(omitted),
               "matched": not omitted and all(row["matched"] for row in results)}
    write_json(run_dir / "replay.json", receipt)
    return receipt


def replay_command(args: Any, root: Path, **kwargs: Any) -> int:
    """CLI callback; a partial or unmeasured replay never returns success."""
    def resolve(value: Any) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (root / path).resolve()

    try:
        result = replay(resolve(args.pack_dir), resolve(args.task), plan=getattr(args, "plan", False),
                        environment=getattr(args, "environment", "modal"), max_usd=getattr(args, "max_usd", None),
                        approve=getattr(args, "approve", None),
                        usd_per_execution=getattr(args, "usd_per_execution", 0.0025),
                        run_dir=resolve(args.run_dir) if getattr(args, "run_dir", None) else None,
                        cells=getattr(args, "cells", None), executor_factory=kwargs.get("executor_factory"))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if getattr(args, "plan", False) or result["matched"] else 1
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc), "matched": False}, sort_keys=True))
        return 1
