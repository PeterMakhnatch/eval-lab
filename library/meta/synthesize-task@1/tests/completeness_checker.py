"""Completeness checker for synthesized Terminal-Bench task packages."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def check_package_structure(task_dir: Path) -> dict[str, Any]:
    """Check that all required Terminal-Bench package files exist and parse cleanly."""
    task_dir = Path(task_dir)
    errors: list[str] = []

    required_files = [
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
    ]

    for rel in required_files:
        p = task_dir / rel
        if not p.is_file():
            errors.append(f"required file missing: {rel}")
        elif p.stat().st_size == 0:
            errors.append(f"required file is empty: {rel}")

    # Validate task.toml structure
    task_toml_path = task_dir / "task.toml"
    if task_toml_path.is_file() and task_toml_path.stat().st_size > 0:
        try:
            config = tomllib.loads(task_toml_path.read_text(encoding="utf-8"))
            if "task" not in config or not isinstance(config["task"], dict):
                errors.append("task.toml: missing [task] section")
            else:
                for field in ("name", "version", "description"):
                    if not config["task"].get(field):
                        errors.append(f"task.toml: [task].{field} is missing or empty")

            for sec in ("environment", "agent", "verifier"):
                if sec not in config or not isinstance(config[sec], dict):
                    errors.append(f"task.toml: missing [{sec}] section")
        except Exception as exc:
            errors.append(f"task.toml: parse error: {exc}")

    # Validate script permissions / shebang
    for script_rel in ("solution/solve.sh", "tests/test.sh"):
        p = task_dir / script_rel
        if p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            if not content.startswith("#!"):
                errors.append(f"{script_rel}: missing shebang (e.g. #!/bin/sh)")

    passed = len(errors) == 0
    msg = "package structure valid" if passed else f"structure errors: {'; '.join(errors)}"
    return {
        "check": "package_structure",
        "passed": passed,
        "errors": errors,
        "message": msg,
    }


def _extract_sensitive_spans(task_dir: Path) -> list[tuple[str, str]]:
    """Extract code/data lines from solution/ and tests/ that must not leak into environment."""
    spans: list[tuple[str, str]] = []
    roots = [task_dir / "solution", task_dir / "tests"]

    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.name in {"Dockerfile", "test.sh", "solve.sh"}:
                continue
            rel = p.relative_to(task_dir).as_posix()
            text = p.read_text(encoding="utf-8", errors="replace")

            for line in text.splitlines():
                normalized = " ".join(line.strip().split())
                # Ignore comments and generic short boilerplate lines
                if (
                    len(normalized) >= 24
                    and not normalized.startswith(("#", "//", "/*", "*", "import ", "from "))
                    and re.search(r"[A-Za-z0-9]", normalized)
                    and not normalized.startswith("def main():")
                    and not normalized.startswith('if __name__ == "__main__":')
                ):
                    spans.append((rel, normalized))

    return spans


def check_no_answer_leakage(task_dir: Path) -> dict[str, Any]:
    """Check that no solution code, golden data, or answer keys leak into agent-visible files."""
    task_dir = Path(task_dir)
    leaks: list[str] = []

    # 1. Check directory structure: no solution/tests in environment/
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            rel = p.relative_to(env_dir).as_posix()
            parts = p.relative_to(env_dir).parts
            if any(part in {"solution", "tests", "verifier"} for part in parts):
                leaks.append(f"environment/ contains hidden directory: {rel}")
            answer_keys = ("golden", "solution", "expected_summary", "answer_key")
            if p.is_file() and any(k in p.name.lower() for k in answer_keys):
                leaks.append(f"environment/ contains answer file: {rel}")

        # Check Dockerfile COPY instructions
        df = env_dir / "Dockerfile"
        if df.is_file():
            df_text = df.read_text(encoding="utf-8", errors="replace")
            for line in df_text.splitlines():
                line_clean = line.strip()
                if line_clean.startswith(("COPY", "ADD")) and any(
                    bad in line_clean for bad in ("solution", "tests", "verifier")
                ):
                    leaks.append(f"environment/Dockerfile copies hidden files: {line_clean}")

    # 2. Check visible text in instruction.md and environment/
    visible_files: list[Path] = []
    instr = task_dir / "instruction.md"
    if instr.is_file():
        visible_files.append(instr)
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            if p.is_file() and p.name != "Dockerfile":
                visible_files.append(p)

    visible_texts: list[str] = []
    for vf in visible_files:
        content = vf.read_text(encoding="utf-8", errors="replace")
        visible_texts.append(" ".join(content.split()))

    combined_visible = "\n".join(visible_texts)

    sensitive_spans = _extract_sensitive_spans(task_dir)
    for src, span in sensitive_spans:
        if span in combined_visible:
            leaks.append(f"sensitive span from {src} leaked in visible surface: {span[:40]}...")

    passed = len(leaks) == 0
    msg = "no answer leakage detected" if passed else f"leakage detected: {'; '.join(leaks)}"
    return {
        "check": "no_answer_leakage",
        "passed": passed,
        "leaks": leaks,
        "message": msg,
    }


def _prepare_workspace(task_dir: Path, workspace: Path) -> None:
    """Set up workspace with initial environment files simulating container launch."""
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for item in env_dir.iterdir():
            if item.name == "Dockerfile":
                continue
            dest = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    # If environment/ copied into input/ or root, also create /app/input structure locally
    input_dir = workspace / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    if env_dir.is_dir():
        for item in env_dir.iterdir():
            if item.name != "Dockerfile" and not item.is_dir():
                shutil.copy2(item, input_dir / item.name)

    (workspace / "output").mkdir(parents=True, exist_ok=True)


def check_oracle_solution_runs(
    task_dir: Path,
    timeout: float = 60.0,
    workspace: Path | None = None,
) -> dict[str, Any]:
    task_dir = Path(task_dir).resolve()
    sol_sh = (task_dir / "solution/solve.sh").resolve()
    sol_py = (task_dir / "solution/solve.py").resolve()

    if not sol_sh.is_file() and not sol_py.is_file():
        return {
            "check": "oracle_solution_runs",
            "passed": False,
            "message": "solution/solve.sh or solution/solve.py is missing",
        }

    use_temp = workspace is None
    target_workspace = Path(tempfile.mkdtemp(prefix="task_oracle_")) if use_temp else workspace
    assert target_workspace is not None

    try:
        if use_temp:
            _prepare_workspace(task_dir, target_workspace)

        cmd = ["bash", str(sol_sh)] if sol_sh.is_file() else [sys.executable, str(sol_py)]
        env = dict(os.environ)
        # Point paths to workspace
        env["APP_DIR"] = str(target_workspace)

        proc = subprocess.run(
            cmd,
            cwd=str(target_workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        passed = proc.returncode == 0
        err_tail = proc.stderr[-200:]
        msg = (
            "oracle solution executed successfully"
            if passed
            else f"oracle failed (rc={proc.returncode}): {err_tail}"
        )
        return {
            "check": "oracle_solution_runs",
            "passed": passed,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "message": msg,
        }
    except Exception as exc:
        return {
            "check": "oracle_solution_runs",
            "passed": False,
            "message": f"oracle execution raised exception: {exc}",
        }
    finally:
        if use_temp and target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)


def check_task_tests_pass(
    task_dir: Path,
    timeout: float = 60.0,
    workspace: Path | None = None,
) -> dict[str, Any]:
    task_dir = Path(task_dir).resolve()
    test_sh = (task_dir / "tests/test.sh").resolve()
    verify_py = (task_dir / "tests/verify.py").resolve()

    if not test_sh.is_file() and not verify_py.is_file():
        return {
            "check": "task_tests_pass",
            "passed": False,
            "message": "tests/test.sh or tests/verify.py is missing",
        }

    use_temp = workspace is None
    target_workspace = Path(tempfile.mkdtemp(prefix="task_verify_")) if use_temp else workspace
    assert target_workspace is not None

    try:
        if use_temp:
            _prepare_workspace(task_dir, target_workspace)
            # Run oracle first in temp workspace
            sol_sh = (task_dir / "solution/solve.sh").resolve()
            sol_py = (task_dir / "solution/solve.py").resolve()
            cmd = ["bash", str(sol_sh)] if sol_sh.is_file() else [sys.executable, str(sol_py)]

        cmd = ["bash", str(test_sh)] if test_sh.is_file() else [sys.executable, str(verify_py)]
        logs_dir = target_workspace / "logs/verifier"
        logs_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            cmd,
            cwd=str(target_workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        passed = proc.returncode == 0
        # Check reward.json if generated
        reward_file = logs_dir / "reward.json"
        if reward_file.is_file():
            try:
                reward_data = json.loads(reward_file.read_text(encoding="utf-8"))
                if isinstance(reward_data, dict) and reward_data.get("reward") != 1.0:
                    passed = False
            except Exception:
                pass

        err_tail = proc.stderr[-200:]
        msg = "task tests passed" if passed else f"tests failed (rc={proc.returncode}): {err_tail}"
        return {
            "check": "task_tests_pass",
            "passed": passed,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "message": msg,
        }
    except Exception as exc:
        return {
            "check": "task_tests_pass",
            "passed": False,
            "message": f"verifier raised exception: {exc}",
        }
    finally:
        if use_temp and target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)


def check_task_completeness(task_dir: Path, timeout: float = 60.0) -> dict[str, Any]:
    """Run all 4 completeness checks on a synthesized task package."""
    task_dir = Path(task_dir)

    # 1. Structure
    structure_res = check_package_structure(task_dir)

    # 2. No leakage
    leakage_res = check_no_answer_leakage(task_dir)

    # 3 & 4. Run oracle then tests in a shared workspace
    workspace = Path(tempfile.mkdtemp(prefix="task_completeness_"))
    try:
        _prepare_workspace(task_dir, workspace)
        oracle_res = check_oracle_solution_runs(task_dir, timeout=timeout, workspace=workspace)
        tests_res = check_task_tests_pass(task_dir, timeout=timeout, workspace=workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    checks = {
        "package_structure": structure_res,
        "no_answer_leakage": leakage_res,
        "oracle_solution_runs": oracle_res,
        "task_tests_pass": tests_res,
    }

    all_passed = all(c["passed"] for c in checks.values())
    rewards = {"reward": 1.0 if all_passed else 0.0}

    return {
        "passed": all_passed,
        "checks": checks,
        "rewards": rewards,
    }


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/output/task")
    res = check_task_completeness(target)
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["passed"] else 1)
