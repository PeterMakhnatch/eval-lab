#!/usr/bin/env python3
"""Static grader-surface scanner for Harbor/terminal-bench task packages.

Reads one package directory and prints JSON

    {"task_id": ..., "features": {...}, "class_flags": {<class>: [{rule, file, line, detail}]}}

Standard library only. Never executes anything from the package, never opens
`solution/`, and decodes the MiMo build-env tarball in memory (no extraction).
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tarfile
from pathlib import Path

TEST_MODULE = re.compile(r"(^|/)(test_[^/]*\.(py|js|ts)|[^/]*_test\.py)$")
CLASSES = [
    "framework_config_injection",
    "runner_exit_status_forgery",
    "test_file_sabotage",
    "hardcoded_expected_output",
    "synthetic_artifact_shape_fill",
    "hidden_answer_read",
    "grader_tamper",
]


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _rel(p: Path, root: Path) -> str:
    return os.path.relpath(str(p), str(root))


def _find(root: Path, *candidates: str) -> Path | None:
    for c in candidates:
        p = root / c
        if p.is_file():
            return p
    return None


def _locate(root: Path, pattern: str) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if "solution" in p.relative_to(root).parts:
            continue
        if p.is_file() and re.search(pattern, p.name, re.I):
            out.append(p)
    return sorted(out)


class Evidence:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, root: Path, path: Path | None, line: int, detail: str) -> None:
        assert path is not None  # evidence is always a real file
        self.items.append(
            {"file": _rel(path, root), "line": int(line), "detail": detail}
        )


def _first_line(lines: list[str], needle: str) -> int:
    for i, ln in enumerate(lines, 1):
        if needle in ln:
            return i
    return 0


def parse_patch(text: str) -> list[dict]:
    """Split a unified diff into per-file blocks."""
    blocks: list[dict] = []
    cur: dict | None = None
    for i, ln in enumerate(text.splitlines(), 1):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", ln)
        if m:
            if cur:
                blocks.append(cur)
            cur = {"path": m.group(2), "line": i, "new": False,
                   "deleted": False, "added": []}
            continue
        if cur is None:
            continue
        if ln.startswith("new file mode"):
            cur["new"] = True
        elif ln.startswith("deleted file mode"):
            cur["deleted"] = True
        elif ln.startswith("+") and not ln.startswith("+++"):
            cur["added"].append(ln[1:])
    if cur:
        blocks.append(cur)
    return blocks


def decode_build_env(patch_text: str) -> str:
    """Return the text of test_command.sh hidden in a base64 tarball, if any."""
    blob = []
    capture = False
    for ln in patch_text.splitlines():
        m = re.match(r"diff --git a/(\S+) b/\S+", ln)
        if m:
            capture = "build_env.tar.gz.b64" in m.group(1)
            continue
        if capture and ln.startswith("+") and not ln.startswith("+++"):
            blob.append(ln[1:])
    if not blob:
        return ""
    try:
        raw = base64.b64decode("".join(x.strip() for x in blob), validate=False)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile() and member.name.endswith("test_command.sh"):
                    f = tf.extractfile(member)
                    if f:
                        return f.read().decode("utf-8", "replace")
    except Exception:
        return ""
    return ""


def scan(pkg: str) -> dict:
    root = Path(pkg)
    task_id = root.name
    ev = Evidence()

    test_sh = _find(root, "tests/test.sh")
    test_cmd = _find(root, "tests/test_command.sh")
    test_patch = _find(root, "tests/test.patch")
    tests_docker = _find(root, "tests/Dockerfile")
    env_docker = _find(root, "environment/Dockerfile")
    setup_sh = _find(root, "environment/setup.sh", "environment/setup/setup.sh")
    blocklist = _find(root, "environment/setup/files/blocklist", "environment/blocklist")
    task_toml = _find(root, "task.toml")

    sh_lines = _lines(test_sh) if test_sh else []
    cmd_lines = _lines(test_cmd) if test_cmd else []
    patch_text = (test_patch.read_text(errors="replace") if test_patch else "")
    patch_blocks = parse_patch(patch_text)
    env_lines = _lines(env_docker) if env_docker else []
    toml_text = (task_toml.read_text(errors="replace") if task_toml else "")

    # --- test sources: package test files, hidden-test patch, build-env command
    test_files = [p for p in _locate(root, r"^test_.*\.py$|_test\.py$")
                  if "environment" not in p.relative_to(root).parts]
    verifier_helpers = [p for p in (root / "tests").glob("*.py")] if (root / "tests").is_dir() else []
    hidden_added = [x for b in patch_blocks for x in b["added"] if x.strip()]
    build_cmd = decode_build_env(patch_text)

    command_text = "\n".join([
        "\n".join(sh_lines),
        "\n".join(cmd_lines),
        "\n".join(b["added"][i] for b in patch_blocks
                  if re.search(r"test_command\.sh$", b["path"])
                  for i in range(len(b["added"]))),
        build_cmd,
    ])
    shell_text = "\n".join(sh_lines)
    all_test_text = "\n".join("\n".join(_lines(p)) for p in test_files + verifier_helpers)
    all_test_text += "\n" + "\n".join(hidden_added)

    # --- workdir / separation
    wd = ""
    m = re.search(r'workdir\s*=\s*"([^"]+)"', toml_text)
    if m:
        wd = m.group(1)
    if not wd:
        for ln in env_lines:
            m = re.match(r"\s*WORKDIR\s+(\S+)", ln)
            if m:
                wd = m.group(1)
    separate = tests_docker is not None
    if separate:
        ev.add(root, tests_docker, _first_line(_lines(tests_docker), "FROM") or 1,
               "tests/Dockerfile -> verifier image is built separately")

    # --- pytest cwd
    cwd = ""
    seq = command_text.splitlines()
    for ln in seq:
        m = re.match(r"\s*cd\s+(\S+)", ln)
        if m:
            cwd = m.group(1).strip("'\"")
        if re.search(r"\bpytest\b", ln):
            break
    if not cwd:
        m = re.search(r'^CWD=(\S+)', shell_text, re.M)
        if m:
            cwd = m.group(1).strip("'\"")
    if not cwd:
        cwd = wd

    # --- pytest invocation
    pytest_invoked = bool(re.search(r"\bpytest\b|\bpy\.test\b|python3? -m pytest|pytestgeventwrapper", command_text))
    python_invoked = bool(re.search(r"\bpython3?\b", command_text))
    path_on_syspath = bool(re.search(r"PYTHONPATH=[^\n]*" + re.escape(wd or "/testbed"), command_text)) or cwd in (wd, "/testbed", "/workspace/repo")
    if pytest_invoked:
        if test_cmd and re.search(r"pytest", "\n".join(cmd_lines)):
            src, detail = test_cmd, "pytest invoked by tests/test_command.sh"
        elif build_cmd:
            src, detail = test_sh, "pytest invoked in decoded build-env test_command.sh"
        elif any(re.search(r"pytest", "\n".join(b["added"])) for b in patch_blocks):
            src, detail = test_patch, "pytest invoked in hidden test command"
        else:
            src, detail = test_sh, "pytest invoked by tests/test.sh"
        if src is not None:
            ev.add(root, src, _first_line(_lines(src), "pytest") or 1, detail)

    # --- reset list from test.sh
    reset: list[tuple[str, int]] = []
    in_heredoc = False
    for i, ln in enumerate(sh_lines, 1):
        if "EOF_RESET_TEST_FILES" in ln:
            in_heredoc = not in_heredoc
            continue
        if in_heredoc and ln.strip():
            reset.append((ln.strip(), i))
    for i, ln in enumerate(sh_lines, 1):
        for m in re.finditer(r"git (?:checkout|restore)\s+\S+\s+--\s+(\S+)", ln):
            reset.append((m.group(1), i))
    reset_paths = {p for p, _ in reset}

    # --- test files the hidden patch touches
    touched = [b for b in patch_blocks if TEST_MODULE.search(b["path"]) or b["path"].startswith("tests/")]
    modified_existing = [b for b in touched if not b["new"] and not b["deleted"]]
    tests_writable = (not separate) and bool(touched) and all(
        not b["path"].startswith("/tests/") for b in touched)
    unsafe = [b for b in modified_existing if b["path"] not in reset_paths]

    # --- grading signal
    reward_exit = bool(re.search(r"(RC|status|\?)\s*-eq\s*0", shell_text)) or bool(
        re.search(r"reward\.txt", shell_text))
    marker_reward = bool(re.search(r"grep\s+-[a-zA-Z]*q?[a-zA-Z]*\s+['\"]?(PASS|OK|SUCCESS)", shell_text, re.I))
    reward_from_exit = reward_exit and not marker_reward

    # --- expected-value comparison and artifacts
    expected_cmp = bool(re.search(
        r"EXPECTED_|\bexpected\b|golden|reference|\bpytest\.approx\b|OPTIMAL_|_TOL\b",
        all_test_text))
    artifact_hits = []
    for m in re.finditer(r"[\"'](/(?:app|testbed|workspace)(?:/[^\"'\s]+)?)[\"']", all_test_text):
        path = m.group(1)
        if re.search(r"inputs?/|schematic|README", path):
            continue
        artifact_hits.append(path)
    artifact_writable = bool(artifact_hits) and any(
        p in (wd.rstrip("/"), "/app", "/testbed")
        or p.startswith(wd.rstrip("/") + "/") or p.startswith("/app/") or p.startswith("/testbed/")
        for p in artifact_hits)
    structural = bool(re.search(
        r"non.?zero|formula cell|sheetnames|workbook|row count|len\([^)]*\)\s*(>=|==)|>=\s*\d+",
        all_test_text))

    # --- reference material reachable from the agent environment
    ref_hits = []
    if (root / "environment").is_dir():
        for p in (root / "environment").rglob("*"):
            if p.is_file() and re.search(r"golden|expected|reference|answer|solution|oracle", p.name, re.I):
                ref_hits.append(_rel(p, root))
    env_copies = [ln for ln in env_lines if re.match(r"\s*(COPY|ADD)\s", ln)
                  and re.search(r"golden|expected|reference|answer|solution|oracle", ln, re.I)]
    har = bool(ref_hits or env_copies)
    mitigated = bool(blocklist) or bool(re.search(r"git-hidden|rm -rf \"/?tests\"", shell_text + "\n".join(_lines(setup_sh) if setup_sh else [])))

    # --- grader command file in an agent-writable path and unprotected
    cmd_in_workdir = bool(re.search(r"mimo_test_command\.sh|\.build_env/test_command\.sh", command_text)) or \
        any("mimo_test_command.sh" in b["path"] for b in patch_blocks)
    cmd_reset = "mimo_test_command.sh" in reset_paths
    gt = (not separate) and cmd_in_workdir and not cmd_reset

    features = {
        "workdir": wd,
        "separate_verifier": separate,
        "pytest_invoked": pytest_invoked,
        "pytest_cwd": cwd,
        "tests_writable": tests_writable,
        "reset_paths": sorted(reset_paths),
        "reset_config": any(re.search(r"conftest\.py|sitecustomize\.py|pytest\.ini|tox\.ini|setup\.cfg", p) for p in reset_paths),
        "modified_test_files": sorted(b["path"] for b in modified_existing),
        "unsafe_test_files": sorted(b["path"] for b in unsafe),
        "signal_exit": reward_from_exit,
        "signal_marker": marker_reward,
        "expected_compare": expected_cmp,
        "artifact_paths": sorted(set(artifact_hits)),
        "artifact_writable": artifact_writable,
        "structural_checks": structural,
        "reference_material": ref_hits,
        "answer_leak_mitigated": mitigated,
        "command_file_in_workdir": cmd_in_workdir,
        "command_file_reset": cmd_reset,
    }

    flags: dict[str, list[dict]] = {c: [] for c in CLASSES}

    def flag(cls: str, rule: str, evd: Evidence) -> None:
        flags[cls].extend({"rule": rule, **item} for item in evd.items)

    # FCI: pytest loads auto-config from an agent-writable root, nothing resets it
    auto_config_channel = (pytest_invoked and "--noconftest" not in command_text) or (python_invoked and path_on_syspath)
    if auto_config_channel and not separate and not features["reset_config"]:
        e = Evidence()
        e.add(root, test_sh, _first_line(sh_lines, "pytest") or 1,
              f"pytest runs in the agent container; cwd={cwd or wd}; no reset of conftest/sitecustomize")
        e.add(root, test_patch if test_patch else test_sh,
              1, "hidden tests are applied into the same tree the agent could write")
        flag("framework_config_injection", "pytest_in_agent_container_without_config_reset", e)

    # RES: exit status alone is the verdict
    if flags["framework_config_injection"] and reward_from_exit:
        e = Evidence()
        e.add(root, test_sh, _first_line(sh_lines, "reward.txt") or 1,
              "reward.txt is written from the runner exit status only")
        flag("runner_exit_status_forgery", "exit_status_is_reward", e)

    # TFS: an existing test module the patch modifies is not restored
    if tests_writable and unsafe:
        e = Evidence()
        for b in unsafe:
            e.add(root, test_patch, b["line"], f"modified test module {b['path']} is not in the reset list")
        flag("test_file_sabotage", "modified_test_file_not_reset", e)

    # HEO: artifact compared against fixed expected values and writable by the agent
    if expected_cmp and artifact_writable:
        e = Evidence()
        e.add(root, (verifier_helpers or test_files or [test_sh])[0],
              _first_line(_lines((verifier_helpers or test_files or [test_sh])[0]), "EXPECTED_") or
              _first_line(_lines((verifier_helpers or test_files or [test_sh])[0]), "expected") or 1,
              f"graded artifact {features['artifact_paths'][:2]} compared against fixed expected values")
        flag("hardcoded_expected_output", "expected_values_compared_on_writable_artifact", e)

    # SAS: structural thresholds on a writable artifact
    if structural and artifact_writable:
        e = Evidence()
        src = (verifier_helpers or test_files or [test_sh])[0]
        ln = next((i for i, line in enumerate(_lines(src), 1)
                   if re.search(r"non.?zero|formula cell|sheetnames|workbook|>= *\d+", line)), 1)
        e.add(root, src, ln, "structural count/schema threshold on the agent-written artifact")
        flag("synthetic_artifact_shape_fill", "structural_threshold_on_writable_artifact", e)

    # HAR: expected/reference material inside the agent environment
    if har:
        e = Evidence()
        p = _locate(root, r"golden|expected|reference|answer|solution|oracle")[0]
        e.add(root, p, 1, "reference-like material is present in environment/")
        flag("hidden_answer_read", "reference_material_in_agent_environment", e)

    # GT: the command wrapper the agent can write is what grading executes
    if gt:
        e = Evidence()
        e.add(root, test_sh, _first_line(sh_lines, "test_command") or 1,
              "grading executes a command wrapper from the agent-writable workdir; it is not reset")
        flag("grader_tamper", "unreset_command_wrapper_in_workdir", e)

    return {"task_id": task_id, "features": features, "class_flags": flags}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: scan_grader_surface.py <package-dir>", file=sys.stderr)
        return 2
    print(json.dumps(scan(argv[1]), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
