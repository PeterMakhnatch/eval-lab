"""Offline PM-port materialization for the native Harbor grader.

The original task still supplies the agent image, instructions and artifact
contract. A separate verifier uses the complete port Containerfile, locked
Python environments, original setup-data bytes and the port's own Step/judge.
Nothing here executes setup code, candidates, providers or container tools on
the host. Static admission is not a claim that an image has built or run.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import shutil
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import tomlkit

from evallab.upstream_grader import GraderRefusal, _resources, _tree_digest

PM_FIXED_PIN = "a3ec9e22725b490a30b35d127bc9e7ac8a28de82"
UPSTREAM_EXPORT_PIN = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
_DATA_DIRS = ("student_data", "shared_data", "root_data", "intermediate_data")
_COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
_PROVIDER_IMPORTS = {"openai", "anthropic", "litellm", "google.genai", "google.generativeai"}

# Runs only inside the dedicated native verifier image. It calls the actual
# Step pre-scoring custody hook and exact ExecutableJudge argv, never a selected
# assertion or fixture label. The port's outer CLI reports infrastructure
# crashes as score=0; these known wrapper failures deliberately produce no
# Harbor reward, so infrastructure does not become a measured candidate miss.
_DRIVER = r'''from __future__ import annotations
import json
import math
import os
import shutil
import subprocess
import traceback
from pathlib import Path, PurePosixPath

LOG = Path("/logs/verifier")


def transfer(source: Path, destination: Path) -> None:
    if not source.exists() and not source.is_symlink():
        return  # Missing submissions must reach the real scorer as missing.
    for entry in (source, *source.rglob("*")) if source.is_dir() else (source,):
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise RuntimeError(f"unsupported submission file type: {entry}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def source_for(destination: Path, artifacts: list[str]) -> Path:
    relative = destination.relative_to(Path("/workdir"))
    matches = []
    for source in artifacts:
        original = PurePosixPath(source)
        if original.parts[1:] == ("app", *relative.parts):
            matches.append(Path(source))
        elif original.parts[1:] == ("workdir", *relative.parts):
            matches.append(Path(source))
        elif original == PurePosixPath("/app"):
            matches.append(Path(source) / relative)
    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        raise RuntimeError(f"no unambiguous native artifact binding for {destination}")
    return matches[0]


def records(metadata: dict, prefix: str = "") -> list[dict]:
    result = []
    checks = metadata.get("checks")
    if isinstance(checks, dict):
        for name, value in checks.items():
            if value == "passed" or (isinstance(value, str) and value.startswith("failed:")):
                result.append({"name": prefix + str(name), "status": "passed" if value == "passed" else "failed", "message": value})
    failed = metadata.get("failed_tests")
    if isinstance(failed, dict):
        result.extend({"name": prefix + str(name), "status": "failed", "message": str(value)} for name, value in failed.items())
    for key in ("ok", "deterministic"):
        if isinstance(metadata.get(key), bool):
            result.append({"name": prefix + key, "status": "passed" if metadata[key] else "failed", "message": str(metadata[key])})
    for key, value in metadata.items():
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    label = item.get("variant", item.get("kind", index))
                    result.extend(records(item, prefix + f"{key}/{label}/"))
    return result


def main() -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    settings = json.loads(Path("/tests/candidate-port-config.json").read_text())
    from karotte import EvaluationRunConfig
    from karotte.judges import ExecutableJudge
    from environment import get_tasks
    config = EvaluationRunConfig(run_id="native-candidate-port", task_id=settings["task_id"], model="candidate-no-model", use_fake_model=True, save_artifacts=False, extra_config={})
    classes = [task for task in get_tasks() if task.id == settings["task_id"]]
    if len(classes) != 1:
        raise RuntimeError("the pinned port must expose exactly one matching Task")
    task = classes[0](config=config)
    if task.required_hardware != settings["required_hardware"]:
        raise RuntimeError("runtime port hardware differs from admitted metadata")
    steps = list(task.steps)
    if len(steps) != 1:
        raise RuntimeError("multi-step port scoring is not admitted")
    step = steps[0]
    paths = step.submission_paths
    if not paths:
        raise RuntimeError("the fixed scorer has no file submission contract")
    bindings = []
    for destination in paths:
        source = source_for(destination, settings["artifacts"])
        bindings.append({"source": str(source), "destination": str(destination), "present": source.exists()})
        if source != destination:
            transfer(source, destination)
    (LOG / "port-submission-bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")
    # Do not run task.pre_hook(): native Harbor already ran the candidate in
    # the original environment, and a port pre-hook would replace its work.
    step.pre_scoring_hook()
    judge = step.judge
    if type(judge) is not ExecutableJudge:
        raise RuntimeError("only the pinned file-based ExecutableJudge is admitted")
    command = list(judge.subprocess_run_args)
    if not command or not all(isinstance(part, str) for part in command):
        raise RuntimeError("the fixed scorer command is not a string argv")
    command[-1] = str(LOG / "port-score.json")
    (LOG / "port-invocation.json").write_text(json.dumps({"mode": "full", "argv": command, "cwd": judge.cwd, "grader_digest": settings["grader_digest"]}, indent=2) + "\n")
    with (LOG / "port-stdout.txt").open("w") as stdout, (LOG / "port-stderr.txt").open("w") as stderr:
        completed = subprocess.run(command, cwd=judge.cwd, stdout=stdout, stderr=stderr, check=False)
    if completed.returncode:
        raise RuntimeError(f"the fixed scorer exited {completed.returncode}")
    payload = json.loads((LOG / "port-score.json").read_text())
    score, metadata = payload["score"], payload["metadata"]
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1 or not isinstance(metadata, dict):
        raise RuntimeError("the fixed scorer emitted an invalid score/metadata payload")
    error = metadata.get("error", "")
    if isinstance(error, str) and (error.startswith("Grading timed out") or error.startswith("Grading crashed") or error.startswith("Grader timed out") or error.startswith("Grader crashed")):
        raise RuntimeError(f"fixed scorer infrastructure failure: {error}")
    # Only explicitly named results are normalized. Aggregate pass counts do
    # not create invented passing cases; the lossless full metadata survives.
    tests = records(metadata)
    (LOG / "ctrf.json").write_text(json.dumps({"results": {"tool": {"name": "pm-fixed-port-full"}, "tests": tests}, "port": {"mode": "full", "metadata": metadata, "per_test_scope": "explicitly named scorer results; aggregate counts retained in metadata"}}, indent=2) + "\n")
    (LOG / "reward.json").write_text(json.dumps({"reward": score}) + "\n")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        LOG.mkdir(parents=True, exist_ok=True)
        (LOG / "port-infrastructure-error.txt").write_text(traceback.format_exc())
        raise
'''
_TEST_SH = "#!/bin/bash\nset -euo pipefail\nexec /root/.venv/bin/python /tests/candidate-port-driver.py\n"
_IMAGE_SUFFIX = "\n# Native fixed-port replay; no candidate bytes are baked into this image.\nCOPY candidate-port-driver.py candidate-port-config.json test.sh /tests/\nUSER root\nWORKDIR /root\n"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _read_config(task: Path) -> dict[str, Any]:
    try:
        return tomllib.loads((task / "task.toml").read_text())
    except (OSError, ValueError) as exc:
        raise GraderRefusal(f"invalid original native task: {exc}") from exc


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise GraderRefusal(f"unsafe setup-data path: {value!r}")
    return path


class _SetupRecipe:
    """Interpret only local data declarations, never arbitrary setup Python.

    This is the existing port's DataContext.fetch_hub convention: constants,
    dictionaries/comprehensions and loops that construct hash-pinned copies.
    Transforming/generating/unpacking/external-download recipes are refused,
    not skipped or approximated. Their actual setup must be qualified first.
    """

    def __init__(self, tree: ast.Module, task_id: str) -> None:
        self.names: dict[str, Any] = {"ctx": {name: PurePosixPath(name) for name in _DATA_DIRS}}
        self.task_id = task_id
        self.assets: list[dict[str, str]] = []
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
                if isinstance(target, ast.Name) and statement.value is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        self.names[target.id] = ast.literal_eval(statement.value)
        setup = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "setup"), None)
        if setup is None or len(setup.args.args) != 1:
            raise GraderRefusal("setup_data.py has no single-argument offline setup recipe")
        self.names[setup.args.args[0].arg] = self.names["ctx"]
        self.statements(setup.body)
        if not self.assets:
            raise GraderRefusal("setup_data.py declared no locally resolvable hash-pinned assets")

    def expression(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.names:
            return self.names[node.id]
        if isinstance(node, ast.Attribute):
            base = self.expression(node.value)
            if isinstance(base, dict) and node.attr in _DATA_DIRS:
                return base[node.attr]
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(self.expression(item) for item in node.elts)
        if isinstance(node, ast.Dict):
            result: dict[Any, Any] = {}
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    result.update(self.expression(value))
                else:
                    result[self.expression(key)] = self.expression(value)
            return result
        if isinstance(node, ast.Subscript):
            return self.expression(node.value)[self.expression(node.slice)]
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for item in node.values:
                if isinstance(item, ast.FormattedValue):
                    parts.append(str(self.expression(item.value)))
                elif isinstance(item, ast.Constant):
                    parts.append(str(item.value))
                else:
                    raise GraderRefusal("unsupported f-string segment in setup-data recipe")
            return "".join(parts)
        if isinstance(node, ast.BinOp):
            left, right = self.expression(node.left), self.expression(node.right)
            if isinstance(node.op, ast.Div) and isinstance(left, PurePosixPath) and isinstance(right, str):
                return left / _relative(right)
            if isinstance(node.op, ast.BitOr) and isinstance(left, dict) and isinstance(right, dict):
                return left | right
            if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
                return left + right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "items" and not node.args and not node.keywords:
            value = self.expression(node.func.value)
            if isinstance(value, dict):
                return tuple(value.items())
        if isinstance(node, ast.DictComp) and len(node.generators) == 1:
            generator = node.generators[0]
            if generator.ifs or generator.is_async:
                raise GraderRefusal("conditional/async setup-data comprehension is unsupported")
            result = {}
            for value in self.expression(generator.iter):
                self.assign(generator.target, value)
                result[self.expression(node.key)] = self.expression(node.value)
            return result
        raise GraderRefusal(f"setup-data recipe needs unsupported expression {type(node).__name__}")

    def assign(self, target: ast.AST, value: Any) -> None:
        if isinstance(target, ast.Name):
            self.names[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)) and len(target.elts) == len(value):
            for item, part in zip(target.elts, value, strict=True):
                self.assign(item, part)
        elif isinstance(target, ast.Subscript):
            self.expression(target.value)[self.expression(target.slice)] = value
        else:
            raise GraderRefusal("unsupported setup-data assignment")

    def statements(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                if statement.value is None:
                    continue
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                value = self.expression(statement.value)
                for target in targets:
                    self.assign(target, value)
            elif isinstance(statement, ast.AugAssign) and isinstance(statement.op, ast.BitOr):
                self.expression(statement.target).update(self.expression(statement.value))
            elif isinstance(statement, ast.For) and not statement.orelse:
                for value in self.expression(statement.iter):
                    self.assign(statement.target, value)
                    self.statements(statement.body)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "update" and len(call.args) == 1 and not call.keywords:
                    self.expression(call.func.value).update(self.expression(call.args[0]))
                elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id in {"ctx", "context"} and call.func.attr == "fetch_hub" and len(call.args) == 3 and not call.keywords:
                    task, revision, assets = map(self.expression, call.args)
                    if task != self.task_id or revision != "4.0.0" or not isinstance(assets, dict):
                        raise GraderRefusal("setup-data Hub task/revision differs from the matched original v4 task")
                    for source, (destination, digest) in assets.items():
                        relative = _relative(str(destination))
                        _relative(source)
                        if relative.parts[0] not in _DATA_DIRS or not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                            raise GraderRefusal("setup-data asset has no safe destination and exact sha256")
                        self.assets.append({"source": source, "destination": str(relative), "sha256": digest})
                else:
                    raise GraderRefusal(f"setup-data recipe requires nonlocal/transforming call {ast.unparse(call.func)}")
            else:
                raise GraderRefusal(f"setup-data recipe requires unsupported statement {type(statement).__name__}")


def _source_pin(root: Path) -> str | None:
    """Read a nearby checkout pin without invoking git or assuming an export is git."""
    for base in (root, root.parent):
        marker = base / ".git"
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            text = marker.read_text().strip()
            if not text.startswith("gitdir: "):
                continue
            git_dir = (base / text[8:]).resolve()
        else:
            continue
        head = (git_dir / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head if len(head) == 40 else None
        ref = head[5:]
        candidate = git_dir / ref
        if candidate.is_file():
            return candidate.read_text().strip()
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for row in packed.read_text().splitlines():
                if not row.startswith(("#", "^")) and row.endswith(" " + ref):
                    return row.split()[0]
    return None


def _interface(port: Path, task_id: str) -> dict[str, Any]:
    task_modules = sorted((port / "src/environment/tasks").glob("*/__init__.py"))
    matches = []
    for module in task_modules:
        tree = ast.parse(module.read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(isinstance(base, ast.Name) and base.id == "Task" for base in node.bases):
                ids = [value.value for assignment in node.body if isinstance(assignment, ast.Assign) for target in assignment.targets if isinstance(target, ast.Name) and target.id == "id" for value in [assignment.value] if isinstance(value, ast.Constant)]
                if task_id in ids:
                    matches.append((module, tree, node))
    if len(matches) != 1:
        raise GraderRefusal("port must contain exactly one Task with the original task identity")
    module, tree, task_class = matches[0]
    hardware = [statement.value.value for method in task_class.body if isinstance(method, ast.FunctionDef) and method.name == "required_hardware" for statement in method.body if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)]
    if len(hardware) != 1:
        raise GraderRefusal("port required_hardware must be an explicit literal resource declaration")
    declarations = {}
    for entry in hardware[0].split(","):
        key, separator, value = entry.partition("=")
        if not separator or key in declarations or key not in {"cpus", "memory_mb", "storage_mb", "gpus", "gpu_types"}:
            raise GraderRefusal("unsupported port hardware declaration")
        declarations[key] = value if key == "gpu_types" else int(value)
    if not all(key in declarations for key in ("cpus", "memory_mb", "storage_mb")):
        raise GraderRefusal("port must explicitly declare CPU, memory and storage")
    judges = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("Judge")}
    if judges != {"ExecutableJudge"}:
        raise GraderRefusal("custom/composite/model-backed port judge is not a qualified native replay shape")
    # Only dependency interfaces are inspected, never assertion bodies/findings.
    for source in sorted((port / "src/environment").rglob("*.py")):
        source_tree = ast.parse(source.read_text())
        imports = {alias.name for node in ast.walk(source_tree) if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module or "" for node in ast.walk(source_tree) if isinstance(node, ast.ImportFrom))
        if any(name == provider or name.startswith(provider + ".") for name in imports for provider in _PROVIDER_IMPORTS):
            raise GraderRefusal("fixed scorer imports an external model/provider dependency; separate authorization and an explicit adapter are required")
    constants: dict[str, int | float] = {}

    def number(node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        if isinstance(node, ast.BinOp):
            left, right = number(node.left), number(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Mult):
                return left * right
        raise ValueError("not a numeric resource declaration")

    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = number(node.value)
            except ValueError:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    timeout = constants.get("GRADING_TIMEOUT_S", constants.get("GRADER_TIMEOUT_S"))
    return {
        "module": ".".join(module.relative_to(port / "src").parts[:-1]),
        "required_hardware": hardware[0],
        "hardware": declarations,
        "declared_grading_timeout_sec": timeout,
    }


def _describe(upstream_task: Path | str, port_task: Path | str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    upstream, port = Path(upstream_task).resolve(), Path(port_task).resolve()
    if not upstream.is_dir() or not port.is_dir() or upstream == port:
        raise GraderRefusal("original and port must be distinct existing task directories")
    config = _read_config(upstream)
    task_id = (config.get("task") or {}).get("name", upstream.name).rsplit("/", 1)[-1]
    if task_id != upstream.name or port.name != task_id:
        raise GraderRefusal("original task, port directory and declared task identity must match")
    if config.get("steps"):
        raise GraderRefusal("multi-step original tasks need an explicit artifact/lifecycle adapter")
    for directory in (upstream / "environment", upstream / "tests", port):
        if any((directory / name).exists() for name in _COMPOSE_NAMES):
            raise GraderRefusal("Compose/service task shapes are not supported by the strict native candidate runner")
    for required in ("Containerfile", "setup_data.py", "pyproject.toml", "uv.lock", ".manifest.json"):
        if not (port / required).is_file():
            raise GraderRefusal(f"port runtime dependency is missing: {required}")
    if not (upstream / "instruction.md").is_file() or not (upstream / "environment/Dockerfile").is_file() and not (config.get("environment") or {}).get("docker_image"):
        raise GraderRefusal("original native instructions/environment definition is missing")
    upstream_digest, port_digest = _tree_digest(upstream), _tree_digest(port)
    port_pin = _source_pin(port)
    if port_pin is not None and port_pin != PM_FIXED_PIN:
        raise GraderRefusal(f"port checkout is not the approved fixed-scorer pin {PM_FIXED_PIN}: {port_pin}")
    interface = _interface(port, task_id)
    agent_raw = config.get("environment") or {}
    agent_resource = _resources(agent_raw, role="agent")
    verifier_raw = dict(interface["hardware"])
    verifier_raw["build_timeout_sec"] = agent_resource["build_timeout_sec"]
    verifier_resource = _resources(verifier_raw, role="verifier")
    artifacts = config.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or not all(isinstance(value, str) and PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts for value in artifacts):
        raise GraderRefusal("original task must declare absolute file/directory artifact sources")
    if not all(value == "/app" or value.startswith("/app/") or value.startswith("/workdir/") for value in artifacts):
        raise GraderRefusal("native artifact paths outside /app and /workdir need an explicit port binding")
    recipe = _SetupRecipe(ast.parse((port / "setup_data.py").read_text()), task_id)
    seen: dict[str, str] = {}
    for asset in recipe.assets:
        if asset["destination"] in seen and seen[asset["destination"]] != asset["sha256"]:
            raise GraderRefusal("setup-data recipe has conflicting destination identities")
        seen[asset["destination"]] = asset["sha256"]
        source = upstream / asset["source"]
        if not source.is_file() or source.is_symlink():
            raise GraderRefusal(f"pinned setup asset is unavailable locally: {asset['source']}")
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != asset["sha256"]:
            raise GraderRefusal(f"local upstream asset differs from pinned port setup-data sha256: {asset['source']}")
    source_pin = _source_pin(upstream)
    grader_digest = _sha(_json({"port_tree": port_digest, "assets": sorted(recipe.assets, key=lambda asset: (asset["destination"], asset["source"]))}).encode())
    runtime = {"task_id": task_id, "required_hardware": interface["required_hardware"], "artifacts": artifacts, "grader_digest": grader_digest}
    generated_dockerfile = (port / "Containerfile").read_text() + _IMAGE_SUFFIX
    logical_image = _sha(_json({"grader_digest": grader_digest, "driver": _sha(_DRIVER.encode()), "config": runtime, "test_sh": _TEST_SH, "dockerfile": generated_dockerfile}).encode())
    result = {
        "schema": "evallab.candidate_port/v1", "task_dir": str(upstream), "upstream_task": str(upstream), "port_task": str(port), "task_id": task_id,
        "mode": "full", "grader_digest": grader_digest,
        "upstream_identity": {"path": str(upstream), "id": task_id, "exact_tree_digest": upstream_digest, "task_toml_sha256": _sha((upstream / "task.toml").read_bytes()), "source_pin": source_pin, "export_declared_pin": UPSTREAM_EXPORT_PIN if source_pin is None else None, "pin_verification": "git metadata" if source_pin else "export pin declaration; exact local bytes separately bound"},
        "port_identity": {"path": str(port), "id": task_id, "exact_tree_digest": port_digest, "source_pin": port_pin, "required_fixed_pin": PM_FIXED_PIN, "pin_verification": "git metadata" if port_pin else "no checkout metadata; exact local bytes bound, pin unverified", "containerfile_sha256": _sha((port / "Containerfile").read_bytes()), "lock_sha256": _sha((port / "uv.lock").read_bytes()), "manifest_sha256": _sha((port / ".manifest.json").read_bytes())},
        "resources": [agent_resource, verifier_resource], "port_interface": interface,
        "setup_data": {"mode": "offline exact hash-pinned copies", "asset_count": len(recipe.assets), "assets": recipe.assets},
        "native_image_identity": {
            "agent": {"source_context": str(upstream / "environment"), "exact_context_digest": _tree_digest(upstream / "environment"), "docker_image": agent_resource["docker_image"]},
            "verifier": {"source_context": str(port), "logical_digest": logical_image, "exact_context_digest": None, "materialization": "pending", "candidate_independent": True, "evidence_path_independent": True},
        },
        "support": {"static_admission": "candidate", "runtime_qualified": False, "local": "requires strict live Docker admission and a successful full-port image build", "modal": "requires upstream native platform/resource admission, one shared approval/cap/ledger and successful image hydration", "refusals": [], "runtime_network": "fixed verifier no-network; no provider credentials or model invocation", "limitations": ["Only one file-submission ExecutableJudge step; runtime checks exact original artifact bindings before scoring.", "Only local sha256-pinned setup-data copy recipes; transformations/external downloads are refused.", "CPU/memory exact; declared storage is minimum free capacity, not a disk quota.", "Port metadata and explicitly named per-test results are retained; aggregate counts do not invent passing test identities."]},
        "_runtime": runtime,
    }
    return result, recipe.assets


def describe_port_task(upstream_task: Path | str, port_task: Path | str) -> dict[str, Any]:
    """Describe an admitted full scorer using local immutable source bytes only."""
    try:
        result, _ = _describe(upstream_task, port_task)
    except (OSError, SyntaxError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GraderRefusal):
            raise
        raise GraderRefusal(f"invalid fixed-port package: {exc}") from exc
    result.pop("_runtime")
    return result


def prepare_port_task(upstream_task: Path | str, port_task: Path | str, output_dir: Path | str) -> dict[str, Any]:
    """Materialize a fresh native task; never mutate original/port/candidate inputs."""
    try:
        result, assets = _describe(upstream_task, port_task)
        upstream, port, destination = Path(result["upstream_task"]), Path(result["port_task"]), Path(output_dir).resolve()
        if destination.exists() or any(destination == source or destination.is_relative_to(source) or source.is_relative_to(destination) for source in (upstream, port)):
            raise GraderRefusal("port output_dir must be fresh and outside both immutable source trees")
        runtime = result.pop("_runtime")
        # Resolve all refusal boundaries before creating any output bytes.
        config = tomlkit.parse((upstream / "task.toml").read_text())
        verifier = config.get("verifier", tomlkit.table())
        verifier["environment_mode"] = "separate"
        verifier["user"] = "root"
        grading_timeout = result["port_interface"]["declared_grading_timeout_sec"]
        if grading_timeout is not None:
            verifier["timeout_sec"] = grading_timeout
        verifier["network_mode"] = "no-network"
        verifier.pop("allowed_hosts", None)
        verifier_environment = tomlkit.table()
        for key, value in result["port_interface"]["hardware"].items():
            verifier_environment[key] = value
        verifier_environment["build_timeout_sec"] = result["resources"][1]["build_timeout_sec"]
        verifier_environment["network_mode"] = "no-network"
        verifier["environment"] = verifier_environment
        config["verifier"] = verifier
        destination.mkdir(parents=True)
        shutil.copytree(upstream / "environment", destination / "environment")
        shutil.copy2(upstream / "instruction.md", destination / "instruction.md")
        # Keep original solution assets for native --oracle, if present. They
        # never enter either image or the port's hidden scorer directory.
        if (upstream / "solution").is_dir():
            shutil.copytree(upstream / "solution", destination / "solution")
        (destination / "task.toml").write_text(tomlkit.dumps(config))
        tests = destination / "tests"
        shutil.copytree(port, tests)
        for asset in assets:
            target = tests / asset["destination"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(upstream / asset["source"], target)
        (tests / "Dockerfile").write_text((port / "Containerfile").read_text() + _IMAGE_SUFFIX)
        (tests / "candidate-port-driver.py").write_text(_DRIVER)
        (tests / "candidate-port-config.json").write_text(_json(runtime) + "\n")
        (tests / "test.sh").write_text(_TEST_SH)
        for name in ("Dockerfile", "candidate-port-driver.py", "candidate-port-config.json"):
            (tests / name).chmod(0o644)
        (tests / "test.sh").chmod(0o755)
        result["task_dir"] = str(destination)
        result["native_image_identity"]["verifier"].update({"materialization": "prepared", "context": str(tests), "exact_context_digest": _tree_digest(tests)})
        result["native_image_identity"]["agent"]["context"] = str(destination / "environment")
        result["prepared_task_digest"] = _tree_digest(destination)
        return result
    except (OSError, SyntaxError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GraderRefusal):
            raise
        raise GraderRefusal(f"cannot prepare fixed-port package: {exc}") from exc
