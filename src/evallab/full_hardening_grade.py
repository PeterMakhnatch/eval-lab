"""Grade-time payload of ``full-hardening@1`` (``evallab harden``), shipped verbatim as ``tests/_evallab/grade.py``.

Standard library only; the hardened grader runs it with ``python3 -I -S`` so no ``.pth``, ``sitecustomize`` or
user site directory planted by the agent is processed while the reward is decided. ``unittest-run`` is the one
subcommand that runs under the task's own interpreter (with site), because it executes the task's tests.

Subcommands (``--root`` remaps every absolute container path, for tests only):

* ``prepare``: L1 auto-load purge (agent-created ``conftest.py``/``sitecustomize.py``/``*.pth``/runner configs are
  deleted, baseline files present in the original package are restored to their recorded bytes) followed by L2
  pristine restoration of every graded test and grader-helper file recorded in ``plan.json``.
* ``inject``: rewrite a test command file in place so the runner writes a structured report to
  ``$EVALLAB_REPORT`` (pytest ``--junitxml``, vitest/jest JSON, mocha JSON, unittest through ``unittest-run``).
* ``finish``: L3 gate. Reward 1 only when the original grader wrote 1 *and* the report exists, parses, holds at least
  the expected test count with no failure/error and no extra skip. Writes ``reward.txt`` and ``evallab-gate.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

#: File names whose mere presence changes what a test runner loads or how it reports.
AUTOLOAD_NAMES = (
    "conftest.py",
    "sitecustomize.py",
    "usercustomize.py",
    "pytest.ini",
    "tox.ini",
)
AUTOLOAD_SUFFIXES = (".pth",)
#: JavaScript runner configuration (a new or edited config can redirect, filter or replace the run).
AUTOLOAD_PREFIXES = ("vitest.config.", "vite.config.", "jest.config.", ".mocharc.")
#: Dependency trees are left alone in the workspace walk (a separate verifier is what protects them).
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".eggs",
    }
)

RUNNERS = ("pytest", "unittest", "vitest", "jest", "mocha")
#: Shell tests and lookups that name the runner without running it (``[ -x ./node_modules/.bin/vitest ]``).
_PROBE = re.compile(r"\[\[?\s+!?\s*-[a-zA-Z]\s|\bcommand\s+-v\b|\bwhich\s|\btype\s")
_COMMENT = re.compile(r"^\s*#")
_PIP = re.compile(r"\b(pip|pip3|uv|poetry|npm|yarn|pnpm)\s+(install|add|i)\b")
_PYTEST = re.compile(
    r"(\bpython[\d.]*(?:\s+-[A-Za-z]+)*\s+-m\s+pytest\b|(?<![\w.$/-])(?:[\w./-]*/)?pytest(?=\s|$|;|\)))"
)
_UNITTEST = re.compile(r"(\bpython[\d.]*(?:\s+-[A-Za-z]+)*)\s+-m\s+unittest\b")
_VITEST = re.compile(r"((?<![\w.-])(?:[\w./-]*/)?vitest(?:\s+run)?)(?=\s|$|;|\))")
_JEST = re.compile(r"((?<![\w.-])(?:[\w./-]*/)?jest)(?=\s|$|;|\))")
_MOCHA = re.compile(r"((?<![\w.-])(?:[\w./-]*/)?mocha)(?=\s|$|;|\))")
_REPORT = '"$EVALLAB_REPORT"'


def is_autoload(name: str) -> bool:
    return (
        name in AUTOLOAD_NAMES
        or name.endswith(AUTOLOAD_SUFFIXES)
        or name.startswith(AUTOLOAD_PREFIXES)
    )


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _at(root: str, container_path: str) -> str:
    return os.path.join(root, container_path.lstrip("/")) if root != "/" else container_path


def inject_report(
    text: str, runner: str, *, unittest_runner: str = "/tests/_evallab/grade.py"
) -> tuple[str, int]:
    """``(new_text, invocations_rewritten)``: ask every runner invocation for a report at ``$EVALLAB_REPORT``.

    Direct injection on the command line survives wrappers that ``unset PYTEST_ADDOPTS`` or start the runner under
    ``env -i`` (the shell expands ``$EVALLAB_REPORT`` before the runner starts).
    """
    out: list[str] = []
    count = 0
    for line in text.splitlines(keepends=True):
        if (
            _COMMENT.match(line)
            or _PIP.search(line)
            or _PROBE.search(line)
            or "EVALLAB_REPORT" in line
        ):
            out.append(line)
            continue
        if runner == "pytest":
            new, n = _PYTEST.subn(lambda m: f"{m.group(1)} --junitxml={_REPORT}", line)
        elif runner == "unittest":
            new, n = _UNITTEST.subn(
                lambda m: f"{m.group(1)} {unittest_runner} unittest-run --report {_REPORT} --", line
            )
        elif runner == "vitest":
            new, n = _VITEST.subn(
                lambda m: f"{m.group(1)} --reporter=json --outputFile={_REPORT}", line
            )
        elif runner == "jest":
            new, n = _JEST.subn(lambda m: f"{m.group(1)} --json --outputFile={_REPORT}", line)
        elif runner == "mocha":
            new, n = _MOCHA.subn(
                lambda m: f"{m.group(1)} --reporter json --reporter-option output={_REPORT}", line
            )
        else:
            raise ValueError(f"unknown runner {runner!r}")
        out.append(new)
        count += n
    return "".join(out), count


def report_counts(runner: str, path: str) -> dict:
    """``{tests, failures, errors, skipped}`` from the runner's structured report; raises on absence or garbage."""
    if runner == "pytest":
        import xml.etree.ElementTree as ET

        root = ET.parse(path).getroot()
        tests = failures = errors = skipped = 0
        for case in root.iter("testcase"):
            tests += 1
            failures += case.find("failure") is not None
            errors += case.find("error") is not None
            skipped += case.find("skipped") is not None
        return {"tests": tests, "failures": failures, "errors": errors, "skipped": skipped}
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if runner == "unittest":
        return {key: int(data[key]) for key in ("tests", "failures", "errors", "skipped")}
    if runner in ("vitest", "jest"):
        failed_suites = int(data.get("numFailedTestSuites", 0)) + int(
            data.get("numRuntimeErrorTestSuites", 0)
        )
        return {
            "tests": int(data["numTotalTests"]),
            "failures": int(data.get("numFailedTests", 0)),
            "errors": failed_suites + (0 if data.get("success", True) else 1),
            "skipped": int(data.get("numPendingTests", 0)) + int(data.get("numTodoTests", 0)),
        }
    if runner == "mocha":
        stats = data["stats"]
        return {
            "tests": int(stats["tests"]),
            "failures": int(stats.get("failures", 0)),
            "errors": 0,
            "skipped": int(stats.get("pending", 0)),
        }
    raise ValueError(f"unknown runner {runner!r}")


def gate(runner: str, report: str, expected: dict | None) -> dict:
    """The L3 decision; ``expected`` None means calibration (counts recorded, no gate)."""
    result: dict = {"runner": runner, "report": report, "expected": expected}
    try:
        counts = report_counts(runner, report)
    except Exception as exc:  # absent, truncated or forged-garbage report: fail closed
        result.update(ok=False, counts=None, why=f"no readable report: {type(exc).__name__}")
        return result
    result["counts"] = counts
    if expected is None:
        result.update(ok=True, why="calibration: counts recorded, no gate")
        return result
    want_tests = max(int(expected.get("tests", 0)), 1)
    want_skipped = int(expected.get("skipped", 0))
    problems = []
    if counts["tests"] < want_tests:
        problems.append(f"tests {counts['tests']} < expected {want_tests}")
    if counts["failures"] or counts["errors"]:
        problems.append(f"failures={counts['failures']} errors={counts['errors']}")
    if counts["skipped"] > want_skipped:
        problems.append(f"skipped {counts['skipped']} > expected {want_skipped}")
    result.update(ok=not problems, why="; ".join(problems) or "report matches the reference")
    return result


def _load_plan(path: str) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def cmd_prepare(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    root = args.root
    bundle = os.path.dirname(os.path.abspath(args.plan))
    log: dict = {"removed": [], "restored_baseline": [], "restored_pristine": []}
    baseline = plan.get("baseline", {})
    workdir = _at(root, plan["workdir"])
    if plan.get("l1", True) and os.path.isdir(workdir):
        for current, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if not is_autoload(name):
                    continue
                path = os.path.join(current, name)
                container = (
                    "/" + os.path.relpath(path, root).replace(os.sep, "/") if root != "/" else path
                )
                want = baseline.get(container)
                if want is None:
                    os.remove(path)
                    log["removed"].append(container)
                elif os.path.islink(path) or sha256_file(path) != want:
                    os.remove(path)
                    shutil.copyfile(os.path.join(bundle, "baseline", container.lstrip("/")), path)
                    log["restored_baseline"].append(container)
        for container in sorted(baseline):
            path = _at(root, container)
            if not os.path.lexists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copyfile(os.path.join(bundle, "baseline", container.lstrip("/")), path)
                log["restored_baseline"].append(container)
    for container, want in sorted(plan.get("pristine", {}).items()):
        source = os.path.join(bundle, "pristine", container.lstrip("/"))
        if sha256_file(source) != want:
            raise SystemExit(f"bundled pristine copy of {container} does not match the plan")
        path = _at(root, container)
        if os.path.isfile(path) and not os.path.islink(path) and sha256_file(path) == want:
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(source, path)
        log["restored_pristine"].append(container)
    with open(args.log, "w", encoding="utf-8") as stream:
        json.dump(log, stream, indent=2, sort_keys=True)
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    runner = plan.get("runner")
    if runner not in RUNNERS:
        print(f"evallab harden: no structured report for runner {runner!r}", file=sys.stderr)
        return 0
    path = _at(args.root, args.file)
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    new, count = inject_report(text, runner)
    if count == 0:
        print(
            f"evallab harden: no {runner} invocation found in {args.file}; the report gate fails closed",
            file=sys.stderr,
        )
        return 0
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(new)
    print(
        f"evallab harden: {runner} report requested in {count} invocation(s) of {args.file}",
        file=sys.stderr,
    )
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    verifier = _at(args.root, args.verifier_dir)
    reward_path = os.path.join(verifier, "reward.txt")
    try:
        with open(reward_path, encoding="utf-8") as stream:
            original = stream.read().strip()
    except OSError:
        original = ""
    record: dict = {"original_reward": original}
    runner = plan.get("runner")
    if plan.get("l3") and runner in RUNNERS:
        decision = gate(runner, _at(args.root, args.report), plan.get("expected"))
        record["gate"] = decision
        passed = original in ("1", "1.0") and decision["ok"]
        final = "1" if passed else "0"
    else:
        record["gate"] = None
        final = original
    record["final_reward"] = final
    if final:
        with open(reward_path, "w", encoding="utf-8") as stream:
            stream.write(final + "\n")
    with open(os.path.join(verifier, "evallab-gate.json"), "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
    gate_text = (
        "none" if record["gate"] is None else ("GATE_OK" if record["gate"]["ok"] else "GATE_NO")
    )
    print(
        f"evallab harden: original reward {original or 'missing'} -> {final or 'missing'} (gate {gate_text})"
    )
    return 0


def cmd_unittest_run(args: argparse.Namespace) -> int:
    """Run ``python -m unittest <args>`` semantics and write a JSON count report before exiting."""
    import unittest

    sys.path[0] = (
        os.getcwd()
    )  # ``python -m unittest`` puts the working directory first, not this file's directory
    report = args.report
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}

    class Runner(unittest.TextTestRunner):
        def run(self, test):  # type: ignore[override]
            result = super().run(test)
            counts.update(
                tests=result.testsRun,
                failures=len(result.failures) + len(result.unexpectedSuccesses),
                errors=len(result.errors),
                skipped=len(result.skipped),
            )
            with open(report, "w", encoding="utf-8") as stream:
                json.dump(counts, stream)
            return result

    program = unittest.main(
        module=None, argv=["python -m unittest", *args.rest], testRunner=Runner, exit=False
    )
    return 0 if program.result.wasSuccessful() and counts["tests"] > 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grade.py")
    parser.add_argument("--root", default="/")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--plan", required=True)
    prepare.add_argument("--log", required=True)
    inject = sub.add_parser("inject")
    inject.add_argument("--plan", required=True)
    inject.add_argument("--file", required=True)
    finish = sub.add_parser("finish")
    finish.add_argument("--plan", required=True)
    finish.add_argument("--report", required=True)
    finish.add_argument("--verifier-dir", default="/logs/verifier")
    run = sub.add_parser("unittest-run")
    run.add_argument("--report", required=True)
    run.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "unittest-run" and args.rest[:1] == ["--"]:
        args.rest = args.rest[1:]
    return {
        "prepare": cmd_prepare,
        "inject": cmd_inject,
        "finish": cmd_finish,
        "unittest-run": cmd_unittest_run,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
