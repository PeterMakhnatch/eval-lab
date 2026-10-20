"""Source-bound JSON tools for an adaptive environment QA reviewer.

Reviewer inspection is distinct from solver access. Runtime calls use the shared
upstream grader; task packages are never edited and model claims never certify
an execution or an independently verified finding.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from evallab.qa.ask import WorkspaceTools
from evallab.qa.models import Finding
from evallab.registry import task_directory_digest

TOOL_NAMES = (
    "task-info", "run-upstream-grader", "run-reference", "run-blank", "probe", "diff",
    "record-finding",
)
_RUNTIME_TOOLS = frozenset({"run-upstream-grader", "run-reference", "run-blank", "probe"})
_CITATION = re.compile(r"([^:\n]+):([1-9][0-9]*)(?:-([1-9][0-9]*))?")


def _write_new(path: Path, value: Any) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("QA artifact paths must not follow symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text() != data:
            raise ValueError(f"immutable QA artifact already exists: {path}")
        return
    with path.open("x", encoding="utf-8") as stream:
        stream.write(data)


def _owned_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("QA evidence paths must not follow symlinks; use /private/tmp")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=True)


class EnvTools:
    """One pinned package and owned evidence root, shared by CLI and agent loop."""

    def __init__(
        self, task_dir: Path, run_dir: Path, *, repo_root: Path, max_seconds: float = 600,
        view_dir: Path | None = None,
    ) -> None:
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, int | float) or not 0 < max_seconds <= 28_800:
            raise ValueError("max_seconds must be in (0, 28800]")
        self.task_dir = task_dir.expanduser().resolve(strict=True)
        if not (self.task_dir / "instruction.md").is_file():
            raise ValueError("task must be a Harbor package with instruction.md")
        if any(path.is_symlink() for path in self.task_dir.rglob("*")):
            raise ValueError("task package symlinks cannot be safely dereferenced by QA tools")
        requested_run = run_dir.expanduser().absolute().resolve()
        if requested_run.is_relative_to(self.task_dir) or self.task_dir.is_relative_to(requested_run):
            raise ValueError("task source and QA output directories must be disjoint")
        if view_dir is not None:
            view_root = view_dir.expanduser().absolute().resolve()
            if requested_run.is_relative_to(view_root) or view_root.is_relative_to(requested_run):
                raise ValueError("exported view and QA output directories must be disjoint")
        self.run_dir = _owned_directory(run_dir)
        self.repo_root = repo_root.resolve(strict=True)
        self.max_seconds = max_seconds
        self.deadline = time.monotonic() + max_seconds
        self.task_id = self.task_dir.name
        self.task_digest = task_directory_digest(self.task_dir)
        self.files = {
            path.relative_to(self.task_dir).as_posix()
            for path in self.task_dir.rglob("*")
            if path.is_file() and not path.is_symlink() and ".git" not in path.parts
            and "__pycache__" not in path.parts and (
                path.relative_to(self.task_dir).as_posix() in {"instruction.md", "task.toml"}
                or path.relative_to(self.task_dir).parts[0] in {"tests", "solution", "environment"}
            )
        }
        self.reader = WorkspaceTools(self.task_dir, self.files)
        self.view_dir: Path | None = None
        self._view_module: Any = None
        self._view_manifest_digest: str | None = None
        if view_dir is not None:
            self._bind_view(view_dir)
        _write_new(self.run_dir / "task.json", {
            "schema": "qa.env.task/v1", "task_id": self.task_id,
            "task_digest": self.task_digest, "task_dir": str(self.task_dir),
            **({"view_dir": str(self.view_dir)} if self.view_dir is not None else {}),
        })

    def _bind_view(self, directory: Path) -> None:
        """Consume the scorer's exported capability, never its private answer key."""
        helper = self.repo_root / "research" / "answer-keys" / "tb4-audit-v1" / "views.py"
        spec = importlib.util.spec_from_file_location("_qa_env_tb4_views", helper)
        if spec is None or spec.loader is None:
            raise ValueError(f"the released TB4 view provider is unavailable: {helper}")
        provider = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provider)
        capability = provider.agent_tools(directory)
        records = [
            json.loads(line)
            for line in capability._path("inputs.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matches = [
            record for record in records
            if record.get("package", {}).get("name") == self.task_dir.name
        ]
        if len(matches) != 1:
            raise ValueError("the exported view must identify this task exactly once")
        public = matches[0]
        subject = public.get("subject", {})
        if subject.get("kind") != "task" or subject.get("task_digest") != self.task_digest:
            raise ValueError("exported view does not match the pinned upstream package")
        if not isinstance(subject.get("task_id"), str) or not subject["task_id"]:
            raise ValueError("exported view has no canonical task identity")
        prefix = f"tasks/{self.task_dir.name}/"
        if public.get("files_root") != prefix.removesuffix("/"):
            raise ValueError("exported task files are not under their canonical view root")
        selected = {name.removeprefix(prefix) for name in capability.files if name.startswith(prefix)}
        if "instruction.md" not in selected or not selected <= self.files:
            raise ValueError("exported view contains missing or unsupported task input surfaces")
        root = capability.root / prefix
        for name in selected:
            if (root / name).read_bytes() != (self.task_dir / name).read_bytes():
                raise ValueError(f"exported task bytes differ from the pinned upstream file: {name}")
        self.task_id = subject["task_id"]
        self.view_dir = capability.root
        self._view_module = provider
        self._view_manifest_digest = hashlib.sha256((self.view_dir / "bundle.json").read_bytes()).hexdigest()
        self.files = selected
        self.reader = WorkspaceTools(root, selected)

    def remaining_seconds(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("QA task time budget exhausted")
        return remaining

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown QA environment tool: {name}")
        if not isinstance(args, dict):
            raise ValueError("tool args must be a JSON object")
        self.remaining_seconds()
        if task_directory_digest(self.task_dir) != self.task_digest:
            raise ValueError("task package changed after its QA identity was pinned")
        if self.view_dir is not None:
            actual = hashlib.sha256((self.view_dir / "bundle.json").read_bytes()).hexdigest()
            if actual != self._view_manifest_digest:
                raise ValueError("exported view manifest changed after its QA identity was pinned")
            self._view_module.agent_tools(self.view_dir)
        if name == "task-info":
            result = self._task_info(args)
        elif name == "diff":
            result = self._diff(args)
        elif name == "record-finding":
            result = self._record_finding(args)
        else:
            result = self._execute(name, args)
        return {
            "tool": name, "task_id": self.task_id, "task_digest": self.task_digest,
            "result": result,
        }

    def _task_info(self, args: dict[str, Any]) -> dict[str, Any]:
        from evallab.qa.env_playbooks import defect_classes

        if set(args) - {"path", "start_line", "end_line", "pattern"}:
            raise ValueError("unknown task-info argument")
        if "pattern" in args:
            read_args = {key: value for key, value in args.items() if key in {"path", "pattern"}}
            return {"evidence_basis": "source_read", "text": self.reader.run("grep", read_args)}
        if "path" in args:
            return {"evidence_basis": "source_read", "text": self.reader.run("read", args)}
        inventory = []
        for name in sorted(self.files):
            path = self.reader._path(name)
            head = name.split("/", 1)[0]
            role = {
                "instruction.md": "instruction", "tests": "grader", "solution": "reference",
                "environment": "environment", "task.toml": "configuration",
            }.get(head, "task_file")
            inventory.append({"path": name, "role": role, "bytes": path.stat().st_size})
        return {
            "evidence_basis": "source_read", "files": inventory,
            "instruction": self.reader.run("read", {"path": "instruction.md"}),
            "defect_classes": list(defect_classes()), "tools": list(TOOL_NAMES),
            "finding_schema": Finding.model_json_schema(),
            "tool_arguments": {
                "task-info": {"path": "optional package-relative path", "start_line": 1,
                              "end_line": 100, "pattern": "optional literal search instead of read"},
                "run-upstream-grader": {
                    "solution_dir": "relative directory under out/candidates/",
                    "submission": {"kind": "command | file | json | patch",
                                   "content": "ordinary candidate program or output",
                                   "path": "absolute in-container output path for file/json"},
                    "plan": False, "timeout_seconds": 600,
                },
                "run-reference": {"plan": False, "timeout_seconds": 600},
                "run-blank": {"plan": False, "timeout_seconds": 600},
                "probe": {"solution_dir": "relative directory under out/candidates/",
                          "test": "Python assertions, run in fresh reference and candidate containers",
                          "submission": "alternative to solution_dir; same structured format as above",
                          "plan": False, "timeout_seconds": 300},
                "diff": {"left": "package path or candidate/<relative-file>",
                         "right": "package path or candidate/<relative-file>"},
                "record-finding": {"defect_class": "one canonical task-side class",
                                   "finding": "qa.finding/v1 matching finding_schema; root_cause=[defect_class]"},
            },
            "note": "Reviewer access does not prove that a solver can reach reference/tests. "
                    "Source review is not executed grading or confirmation of an exploit. "
                    "Root README/cheat material is excluded from reviewer inputs. Supply either "
                    "solution_dir or submission for candidate runs; programs execute only in "
                    "the first-party sandbox, never on the host.",
        }

    def _diff(self, args: dict[str, Any]) -> dict[str, Any]:
        from difflib import unified_diff

        if set(args) - {"left", "right"} or not all(
            isinstance(args.get(key), str) and args[key] for key in ("left", "right")
        ):
            raise ValueError("diff needs left and right package- or evidence-relative files")
        texts = []
        for raw in (args["left"], args["right"]):
            if raw.startswith("candidate/"):
                path = self._candidate_path(raw.removeprefix("candidate/"), file=True)
            else:
                path = self.reader._path(self.reader._relative(raw))
            if path.stat().st_size > 256_000:
                raise ValueError("diff input exceeds 256000 bytes; use a bounded task-info read")
            texts.append(path.read_text(encoding="utf-8").splitlines(keepends=True))
        lines = list(unified_diff(*texts, fromfile=args["left"], tofile=args["right"]))
        text = "".join(lines)
        if len(text) > 32_000:
            raise ValueError("diff exceeds 32000 characters; compare smaller files")
        return {"evidence_basis": "source_read", "diff": text}

    def _candidate_path(self, raw: str, *, file: bool = False) -> Path:
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
            raise ValueError("candidate paths must be relative to the run's candidates/ directory")
        base = self.run_dir / "candidates"
        path = base / raw
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("candidate symlinks are forbidden")
        path = path.resolve(strict=True)
        if not path.is_relative_to(base.resolve()) or (file and not path.is_file()):
            raise ValueError("candidate path leaves its declared directory")
        if not file and not path.is_dir():
            raise ValueError("candidate must be a directory")
        return path

    def _candidate(self, args: dict[str, Any]) -> Path:
        if ("solution_dir" in args) == ("submission" in args):
            raise ValueError("select exactly one solution_dir or structured submission")
        if "solution_dir" in args:
            return self._candidate_path(args["solution_dir"])
        from evallab.qa.task import submission_to_script

        submission = args["submission"]
        if not isinstance(submission, dict):
            raise ValueError("submission must use the existing QA Task structured format")
        directory = _owned_directory(self.run_dir / "candidates") / uuid.uuid4().hex
        compiled = submission_to_script(submission, out_dir=directory, label="solve")
        if compiled is None:
            raise ValueError("invalid structured submission")
        script, _ = compiled
        executable = directory / "solve.sh"
        if not executable.exists():
            with executable.open("x", encoding="utf-8") as stream:
                stream.write(script)
        return directory

    def _citation(self, value: str) -> bool:
        matched = _CITATION.fullmatch(value)
        if matched is None:
            return False
        path = self.reader._path(self.reader._relative(matched[1]))
        lines = path.read_text(encoding="utf-8").splitlines()
        start, end = int(matched[2]), int(matched[3] or matched[2])
        if not 1 <= start <= end <= len(lines):
            raise ValueError(f"citation outside retained task evidence: {value}")
        return True

    def _witness(self, value: str) -> bool:
        if not value.startswith("witness:"):
            return False
        identity = value.removeprefix("witness:")
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("invalid execution witness id")
        receipt = self.run_dir / "observations" / f"{identity}.json"
        if any(part.is_symlink() for part in (receipt, *receipt.parents)):
            raise ValueError("execution witness symlinks are forbidden")
        observed = json.loads(receipt.read_text())
        if observed.get("id") != identity or observed.get("task_digest") != self.task_digest or observed.get("tool") not in _RUNTIME_TOOLS:
            raise ValueError("execution witness is not bound to this task and tool")
        if observed.get("execution_status") != "measured":
            raise ValueError("an unmeasured or failed execution is not an executed witness")
        return True

    def _record_finding(self, args: dict[str, Any]) -> dict[str, Any]:
        from evallab.qa.env_playbooks import defect_classes

        if set(args) != {"defect_class", "finding"}:
            raise ValueError("record-finding needs defect_class and a qa.finding/v1 finding")
        defect_class = args["defect_class"]
        if defect_class not in defect_classes():
            raise ValueError("defect_class is not in the pinned task-side taxonomy")
        finding = Finding.model_validate(args["finding"])
        if finding.subject.kind != "task" or finding.subject.task_id != self.task_id:
            raise ValueError("finding subject does not match the bound task")
        if finding.subject.task_digest != self.task_digest:
            raise ValueError("finding must name the exact inspected task digest")
        if finding.refutation.status == "verified":
            raise ValueError("the reviewer cannot self-certify independent refutation; "
                             "record as unverified and retain the actual evidence")
        if finding.verdict != "fail":
            raise ValueError("record-finding records suspected defects, not blanket pass certificates")
        if finding.root_cause != [defect_class]:
            raise ValueError("finding root_cause must contain exactly its canonical task-side class")
        evidence = []
        for ref in finding.evidence_refs:
            if self._witness(ref):
                evidence.append("executed_witness")
            elif self._citation(ref):
                evidence.append("source_citation")
            else:
                raise ValueError(f"evidence must be package file:line or witness:<id>: {ref}")
        if not evidence:
            raise ValueError("a finding needs a resolvable file:line citation or executed witness")
        if "executed_witness" not in evidence and finding.measurement != "not_run":
            raise ValueError("source-only evidence requires measurement='not_run'; "
                             "it cannot claim native execution")
        payload = finding.model_dump(mode="json", by_alias=True)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        path = self.run_dir / "findings" / f"{digest}.json"
        _write_new(path, payload)
        return {
            "path": str(path), "defect_class": defect_class, "status": "suspected",
            "evidence_basis": sorted(set(evidence)), "finding": payload,
            "note": "Evidence identity and citation bounds validated; semantic confirmation "
                    "and independent refutation are separate from recording a reviewer claim.",
        }

    def _observe(self, tool: str, result: dict[str, Any]) -> dict[str, Any]:
        identity = uuid.uuid4().hex
        measured = result.get("status") == "measured" and result.get("executed") is True
        if tool != "probe" and measured and result.get("task", {}).get("digest") != self.task_digest:
            raise ValueError("native grader result belongs to a different task package")
        receipt = {
            "schema": "qa.env.observation/v1", "id": identity, "tool": tool,
            "task_id": self.task_id, "task_digest": self.task_digest,
            "execution_status": "measured" if measured else "unmeasured", "result": result,
        }
        _write_new(self.run_dir / "observations" / f"{identity}.json", receipt)
        return {
            **result, "execution_status": receipt["execution_status"],
            "witness": f"witness:{identity}" if measured else None,
            "observation_path": str(self.run_dir / "observations" / f"{identity}.json"),
        }

    def _execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        from evallab.upstream_grader import run_upstream_grader

        if name == "probe":
            return self._probe(args)
        if set(args) - {"solution_dir", "submission", "plan", "timeout_seconds", "platform"}:
            raise ValueError("unknown grader argument; backend/paid approval are owner policy")
        if "plan" in args and type(args["plan"]) is not bool:
            raise ValueError("plan must be boolean")
        if name != "run-upstream-grader" and {"solution_dir", "submission"} & set(args):
            raise ValueError("reference/blank controls do not take candidate files")
        candidate = None
        if name == "run-upstream-grader":
            candidate = self._candidate(args)
        requested = args.get("timeout_seconds", 600)
        if type(requested) is not int or requested <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        timeout = min(requested, int(self.remaining_seconds()))
        if timeout < 1:
            raise TimeoutError("less than one second remains for native grading")
        native_root = _owned_directory(self.run_dir / "native")
        result = run_upstream_grader(
            self.task_dir, candidate, oracle=name == "run-reference", blank=name == "run-blank",
            backend="docker", platform=args.get("platform"), timeout_seconds=timeout,
            output_dir=native_root / uuid.uuid4().hex, plan=args.get("plan", False),
        )
        if args.get("plan", False):
            return {**result, "evidence_basis": "plan_only", "execution_status": "not_run",
                    "witness": None}
        return self._observe(name, result)

    def _probe(self, args: dict[str, Any]) -> dict[str, Any]:
        import ast
        import shutil

        from evallab.upstream_grader import run_upstream_grader

        if set(args) - {"solution_dir", "submission", "test", "plan", "timeout_seconds", "platform"}:
            raise ValueError("unknown probe argument")
        source = args.get("test")
        if not isinstance(source, str) or not source.strip() or len(source.encode()) > 64_000:
            raise ValueError("probe needs a nonempty Python test of at most 64000 bytes")
        ast.parse(source, filename="qa-added-probe.py")
        candidate = self._candidate(args)
        if "plan" in args and type(args["plan"]) is not bool:
            raise ValueError("plan must be boolean")
        requested = args.get("timeout_seconds", 300)
        if type(requested) is not int or requested <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        directory = _owned_directory(self.run_dir / "probes") / uuid.uuid4().hex
        package = directory / self.task_dir.name
        shutil.copytree(self.task_dir, package, symlinks=False)
        tests = package / "tests"
        (tests / "qa-added-probe.py").write_text(source, encoding="utf-8")
        # This is an added diagnostic grader, never an upstream verifier result.
        (tests / "test.sh").write_text(
            "#!/bin/bash\nset -u\nmkdir -p /logs/verifier\n"
            "python3 /tests/qa-added-probe.py > /logs/verifier/probe-stdout.txt "
            "2> /logs/verifier/probe-stderr.txt\nqa_status=$?\n"
            "cat /logs/verifier/probe-stdout.txt /logs/verifier/probe-stderr.txt\n"
            "if [ \"$qa_status\" -eq 0 ]; then\n"
            "  echo 'qa-added-probe.py::added_check PASSED'\n"
            "  printf '1.0' > /logs/verifier/reward.txt\n"
            "else\n  echo 'qa-added-probe.py::added_check FAILED'\n"
            "  printf '0.0' > /logs/verifier/reward.txt\nfi\nexit 0\n",
            encoding="utf-8",
        )
        changed_digest = task_directory_digest(package)
        observations = {}
        for label in ("reference", "candidate"):
            timeout = min(requested, int(self.remaining_seconds()))
            if timeout < 1:
                raise TimeoutError("QA task time budget exhausted between fresh probe controls")
            observations[label] = run_upstream_grader(
                package, candidate if label == "candidate" else None,
                oracle=label == "reference", backend="docker", timeout_seconds=timeout,
                platform=args.get("platform"), plan=args.get("plan", False),
                output_dir=directory / f"{label}-native",
            )
        measured = all(
            value.get("status") == "measured" and value.get("executed") is True
            for value in observations.values()
        )
        reference_passed = observations["reference"].get("reward") == 1 if measured else None
        result = {
            "kind": "added_diagnostic_probe", "upstream_grader_unchanged": False,
            "source_task_digest": self.task_digest, "probe_task_digest": changed_digest,
            "probe_source": str(tests / "qa-added-probe.py"),
            "status": "measured" if measured else "unmeasured",
            "executed": any(value.get("executed") is True for value in observations.values()),
            "reference": observations["reference"], "candidate": observations["candidate"],
            "reference_passed": reference_passed,
            "distinguishes": observations["candidate"].get("reward") == 0
            if reference_passed is True else None,
            "note": "The Python assertion runs only in fresh first-party task environments. "
                    "A failing reference invalidates the proposed check; a distinguishing check "
                    "does not by itself prove that its assertion follows from the instruction.",
        }
        if args.get("plan", False):
            return {**result, "execution_status": "not_run", "witness": None}
        return self._observe("probe", result)


def _tool_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        if args.request == "-":
            request = json.load(sys.stdin)
        else:
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        if not isinstance(request, dict) or set(request) - {"task", "out", "args", "max_seconds", "view"}:
            raise ValueError("request needs task, out and args, with optional max_seconds and view")
        if not all(isinstance(request.get(key), str) and request[key] for key in ("task", "out")):
            raise ValueError("task and out must be nonempty paths")
        tools = EnvTools(
            Path(request["task"]), Path(request["out"]), repo_root=root,
            max_seconds=request.get("max_seconds", 600),
            view_dir=Path(request["view"]) if request.get("view") is not None else None,
        )
        with redirect_stdout(sys.stderr):
            result = tools.call(args.env_tool, request.get("args", {}))
    except (OSError, ValueError, TypeError, TimeoutError, ImportError, SyntaxError) as exc:
        print(json.dumps({"ok": False, "tool": args.env_tool, "error": str(exc),
                          "error_type": type(exc).__name__, "reward": None}))
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True, allow_nan=False))
    return 0


def add_env_commands(sub: Any) -> None:
    """Register the same source-bound JSON capabilities used by the reviewer."""
    env = sub.add_parser("env", help="Adaptive environment QA tools and subscription reviewer")
    commands = env.add_subparsers(dest="env_tool", required=True)
    for name in TOOL_NAMES:
        command = commands.add_parser(name, help=f"JSON-in/JSON-out {name}")
        command.add_argument("--request", default="-", help="JSON request file, or - for stdin")
        command.set_defaults(func=_tool_command)
    from evallab.qa.env_agent import add_env_agent_command

    add_env_agent_command(commands)
