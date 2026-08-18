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


def _get_input_file_contents(task_dir: Path) -> set[str]:
    """Return set of normalized text contents for all agent input files under environment/."""
    contents: set[str] = set()
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            if p.is_file() and p.name != "Dockerfile":
                try:
                    text = p.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        contents.add(" ".join(text.split()))
                except Exception:
                    pass
    return contents


def _extract_sensitive_spans(task_dir: Path) -> list[tuple[str, str]]:
    """Extract code, data literals, and logic lines from solution/ and tests/ that must not leak.

    Threshold rationale:
    - Minimum length: 16 characters for single-line statements (or 12 characters when containing
      code/data syntax like '=', '{', '}', '[', ']', ':'). This filters out short language tokens
      (e.g., 'pass', 'n = 0', 'return res') that could collide by chance, while reliably catching
      task-specific formulas, data transformations, and answer constants.
    - Input fixtures in tests/ that identically match files in environment/ are legitimate mirrored
      verifier fixtures and are excluded to avoid false positives.
    """
    spans: list[tuple[str, str]] = []
    known_inputs = _get_input_file_contents(task_dir)
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
            try:
                raw_text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            # Skip mirrored input fixtures
            norm_full = " ".join(raw_text.strip().split())
            if norm_full in known_inputs:
                continue

            lines = raw_text.splitlines()
            # 1. Single lines
            for line in lines:
                normalized = " ".join(line.strip().split())
                if not normalized:
                    continue
                # Ignore comments and generic boilerplate
                if normalized.startswith(("#", "//", "/*", "*", "import ", "from ", "#!")):
                    continue
                if normalized in {
                    "def main():",
                    "def main() -> None:",
                    'if __name__ == "__main__":',
                    "set -eu",
                    "pass",
                    "return 0",
                    "try:",
                    "except Exception:",
                    "finally:",
                }:
                    continue

                has_code_syntax = any(
                    ch in normalized for ch in ("=", "{", "}", "[", "]", ":", "(", ")")
                )
                min_len = 12 if has_code_syntax else 16

                if len(normalized) >= min_len and re.search(r"[A-Za-z0-9]", normalized):
                    spans.append((rel, normalized))

            # 2. Multi-line blocks (sliding window of 2 non-empty code lines)
            clean_lines = [
                " ".join(line.strip().split())
                for line in lines
                if line.strip()
                and not line.strip().startswith(("#", "//", "/*", "*", "import ", "from ", "#!"))
            ]
            for i in range(len(clean_lines) - 1):
                block = f"{clean_lines[i]} {clean_lines[i + 1]}"
                if len(block) >= 20 and block not in [s[1] for s in spans]:
                    spans.append((rel, block))

    return spans


def _extract_oracle_output_spans(task_dir: Path) -> list[tuple[str, str]]:
    """Run oracle solution in a temporary sandbox and extract generated answer output spans."""
    output_spans: list[tuple[str, str]] = []
    sol_sh = task_dir / "solution/solve.sh"
    sol_py = task_dir / "solution/solve.py"
    if not sol_sh.is_file() and not sol_py.is_file():
        return output_spans

    workspace = Path(tempfile.mkdtemp(prefix="leak_check_oracle_"))
    try:
        _prepare_workspace(task_dir, workspace)
        cmd = (
            ["bash", str(sol_sh.resolve())]
            if sol_sh.is_file()
            else [sys.executable, str(sol_py.resolve())]
        )
        env = dict(os.environ)
        env["APP_DIR"] = str(workspace)
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if proc.returncode == 0:
            out_dir = workspace / "output"
            if out_dir.is_dir():
                for p in out_dir.rglob("*"):
                    if not p.is_file():
                        continue
                    rel = f"oracle output ({p.name})"
                    text = p.read_text(encoding="utf-8", errors="replace").strip()
                    if not text:
                        continue
                    norm_text = " ".join(text.split())
                    if len(norm_text) >= 10:
                        output_spans.append((rel, norm_text))
                    # If JSON, extract distinct key-value spans and data structures
                    try:
                        data = json.loads(text)
                        if isinstance(data, dict):
                            for k, v in data.items():
                                kv_repr = f'"{k}": {json.dumps(v)}'
                                if len(kv_repr) >= 10:
                                    output_spans.append((rel, kv_repr))
                                kv_plain = f"{k}: {v}"
                                if len(kv_plain) >= 8:
                                    output_spans.append((rel, kv_plain))
                    except Exception:
                        for line in text.splitlines():
                            norm_l = " ".join(line.strip().split())
                            if len(norm_l) >= 12:
                                output_spans.append((rel, norm_l))
    except Exception:
        pass
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    return output_spans


def check_no_answer_leakage(task_dir: Path) -> dict[str, Any]:
    """Check that no solution code, golden data, or answer keys leak into agent-visible files."""
    task_dir = Path(task_dir)
    leaks: list[str] = []

    # 1. Structural directory checks: no hidden solution/tests/verifier in environment/
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            rel = p.relative_to(env_dir).as_posix()
            parts = p.relative_to(env_dir).parts
            if any(part in {"solution", "tests", "verifier"} for part in parts):
                leaks.append(f"environment/ contains hidden directory: {rel}")

            # Fast filename heuristic (case-insensitive)
            answer_keys = (
                "golden",
                "solution",
                "expected_summary",
                "answer_key",
                "answer",
                "ground_truth",
                "expected_output",
            )
            if p.is_file() and any(k in p.name.lower() for k in answer_keys):
                leaks.append(f"environment/ contains answer file: {rel}")

        # Check Dockerfile COPY / ADD instructions
        df = env_dir / "Dockerfile"
        if df.is_file():
            df_text = df.read_text(encoding="utf-8", errors="replace")
            for line in df_text.splitlines():
                line_clean = line.strip()
                if line_clean.startswith(("COPY", "ADD")) and any(
                    bad in line_clean for bad in ("solution", "tests", "verifier")
                ):
                    leaks.append(f"environment/Dockerfile copies hidden files: {line_clean}")

    # 2. Explicit answer marker patterns in agent-visible environment files
    answer_marker_regex = re.compile(
        r"(?i)\b(expected_output|golden_output|answer_key|ground_truth|expected_summary|correct_answer|target_output)\s*[:=]\s*(\S+.*)"
    )

    env_files: list[Path] = []
    if env_dir.is_dir():
        for p in sorted(env_dir.rglob("*")):
            if p.is_file() and p.name != "Dockerfile":
                env_files.append(p)

    for ef in env_files:
        rel = ef.relative_to(task_dir).as_posix()
        try:
            content = ef.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line in content.splitlines():
            m = answer_marker_regex.search(line)
            if m:
                matched = m.group(0).strip()
                leaks.append(f"{rel} contains explicit answer marker: '{matched[:60]}'")

    # 3. Content correspondence against sensitive solution and test spans
    sensitive_spans = _extract_sensitive_spans(task_dir)
    for ef in env_files:
        rel = ef.relative_to(task_dir).as_posix()
        try:
            raw_content = ef.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        norm_content = " ".join(raw_content.split())

        for src, span in sensitive_spans:
            if span in norm_content or span in raw_content:
                preview = span[:60] + ("..." if len(span) > 60 else "")
                leaks.append(
                    f"{rel}: sensitive span from {src} leaked in visible surface: '{preview}'"
                )

    # 4. Content correspondence against oracle execution output
    oracle_spans = _extract_oracle_output_spans(task_dir)
    for ef in env_files:
        rel = ef.relative_to(task_dir).as_posix()
        try:
            raw_content = ef.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        norm_content = " ".join(raw_content.split())

        for src, span in oracle_spans:
            if span in norm_content or span in raw_content:
                preview = span[:60] + ("..." if len(span) > 60 else "")
                leaks.append(f"{rel}: oracle output from {src} leaked: '{preview}'")

    # 5. Check instruction.md for verbatim solution code blocks
    instr = task_dir / "instruction.md"
    if instr.is_file():
        try:
            instr_text = " ".join(instr.read_text(encoding="utf-8", errors="replace").split())
            for src, span in sensitive_spans:
                if len(span) >= 36 and span in instr_text and src.startswith("solution/"):
                    preview = span[:60] + ("..." if len(span) > 60 else "")
                    leaks.append(
                        f"instruction.md: solution code span from {src} leaked: '{preview}'"
                    )
        except Exception:
            pass

    # Deduplicate leaks while preserving order
    unique_leaks: list[str] = []
    seen = set()
    for leak in leaks:
        if leak not in seen:
            seen.add(leak)
            unique_leaks.append(leak)

    passed = len(unique_leaks) == 0
    msg = "no answer leakage detected" if passed else f"leakage detected: {'; '.join(unique_leaks)}"
    return {
        "check": "no_answer_leakage",
        "passed": passed,
        "leaks": unique_leaks,
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
