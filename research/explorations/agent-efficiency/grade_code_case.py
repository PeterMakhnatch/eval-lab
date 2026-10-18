"""Bounded external acceptance for prepared native coding workspaces.

Fixtures are created outside the solver snapshot. No model, installation, build,
pytest invocation, source-text assertions, or private readout helper is used.
The devloop subprocess boundary uses a controlled executable instead of running
real pytest; trajectory readouts exercise the real public CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from prepare_code_cases import BASELINE, TASKS

CLI_PROGRAM = """import sys
from pathlib import Path
from evallab.cli import run_cli
raise SystemExit(run_cli(sys.argv[2:], workspace=Path(sys.argv[1])))
"""
CACHE_PARTS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []

    def check(self, name: str, condition: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "passed": bool(condition), "detail": detail})

    def json_result(self, label: str, result: subprocess.CompletedProcess) -> dict | None:
        self.observations.append(
            {
                "label": label,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        try:
            value = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            self.check(f"{label}: single JSON object", False, str(exc))
            return None
        self.check(f"{label}: single JSON object", isinstance(value, dict))
        return value if isinstance(value, dict) else None


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    rows = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in CACHE_PARTS)
        parent = Path(directory)
        for name in sorted(files + [name for name in directories if (parent / name).is_symlink()]):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                rows[relative] = {"path": relative, "kind": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                rows[relative] = {
                    "path": relative,
                    "kind": "file",
                    "sha256": digest(path.read_bytes()),
                    "executable": bool(path.stat().st_mode & 0o111),
                }
    return rows


def check_scope(workspace: Path, manifest: dict, task: str, report: Report) -> bool:
    manifest_file = Path(manifest["baseline_files"])
    data = manifest_file.read_bytes()
    if digest(data) != manifest["baseline_files_sha256"]:
        raise ValueError("External baseline file manifest digest does not match")
    expected = {
        row["path"]: row
        for row in json.loads(data)
        if not any(part in CACHE_PARTS for part in Path(row["path"]).parts)
    }
    actual = inventory(workspace)
    changed = sorted(
        path for path in expected.keys() | actual.keys() if expected.get(path) != actual.get(path)
    )
    allowed = set(TASKS[task]["paths"])
    rejected = [path for path in changed if path not in allowed]
    rejected.extend(
        path
        for path in changed
        if path in allowed
        and (
            actual.get(path, {}).get("kind") != "file"
            or actual.get(path, {}).get("executable") != expected[path].get("executable")
        )
    )
    report.check("Source-only edit scope", not rejected, {"changed": changed, "rejected": rejected})
    return not rejected


def environment(
    fixture: Path, workspace: Path, extra: dict[str, str] | None = None
) -> dict[str, str]:
    home = fixture / "isolated-home"
    home.mkdir(exist_ok=True)
    result = {
        "PATH": str(fixture / "bin") + os.pathsep + os.defpath,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "LANG": "en_US.UTF-8",
        "TZ": "UTC",
        "PYTHONPATH": str(workspace / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "OTEL_SDK_DISABLED": "true",
        "HF_HUB_OFFLINE": "1",
        "EVALLAB_RUNS_ROOT": str(fixture),
        "EVALLAB_DATA_ROOT": str(fixture / "data"),
        "EVALLAB_DERIVED_ROOT": str(fixture / "derived"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    result.update(extra or {})
    return result


def run_cli(workspace: Path, fixture: Path, args: list[str], extra: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-B", "-c", CLI_PROGRAM, str(fixture), *args],
        cwd=fixture,
        env=environment(fixture, workspace, extra),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=90,
        check=False,
    )


def grade_devloop(workspace: Path, fixture: Path, report: Report) -> None:
    files = {
        ".gitignore": "bin/\nisolated-home/\ninvocation-*.json\ndry-run-invocation.json\nderived/\ndata/\n",
        "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests", "checks"]\n',
        "src/evallab/alpha.py": "VALUE = 1\n",
        "src/evallab/beta.py": "from evallab.alpha import VALUE\n",
        "tests/test_alpha.py": "from evallab import alpha\n",
        "tests/test_text.py": 'REFERENCE = "evallab.alpha"\n',
        "tests/test_beta.py": "from evallab.beta import VALUE\n",
        "tests/test_other.py": "def test_other():\n    assert True\n",
        "checks/test_extra.py": "import evallab.alpha\n",
    }
    for relative, content in files.items():
        path = fixture / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git_env = environment(fixture, workspace)
    for command in (
        ["git", "init", "--quiet"],
        ["git", "add", "--", *files],
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=" + os.devnull,
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(
            command, cwd=fixture, env=git_env, check=True, capture_output=True, text=True
        )
    (fixture / "src/evallab/alpha.py").write_text("VALUE = 2\n", encoding="utf-8")
    binary = fixture / "bin" / "uv"
    binary.parent.mkdir()
    binary.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['PROBE_INVOCATION']).write_text(json.dumps(sys.argv[1:]))\n"
        "status = int(os.environ['PROBE_CHILD_STATUS'])\n"
        "if os.environ['PROBE_CHILD_KIND'] == 'json':\n"
        "    print(json.dumps({'child_stdout_marker': True, 'status': status}), flush=True)\n"
        "else:\n"
        "    print('CHILD_STDOUT_MARKER', flush=True)\n"
        "print('CHILD_STDERR_MARKER', file=sys.stderr, flush=True)\n"
        "raise SystemExit(status)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    selected = {"tests/test_alpha.py", "tests/test_text.py", "checks/test_extra.py"}
    for as_json, status, kind in ((True, 0, "plain"), (True, 7, "json"), (False, 7, "plain")):
        label = f"devloop json={as_json} child={status} output={kind}"
        invocation = fixture / f"invocation-{as_json}-{status}.json"
        extra = {
            "PROBE_INVOCATION": str(invocation),
            "PROBE_CHILD_STATUS": str(status),
            "PROBE_CHILD_KIND": kind,
        }
        result = run_cli(
            workspace,
            fixture,
            ["registry", "devloop", "--run", *(["--json"] if as_json else [])],
            extra,
        )
        report.check(
            label + ": exact child exit status", result.returncode == status, result.returncode
        )
        report.check(label + ": child executed", invocation.is_file())
        marker = "child_stdout_marker" if kind == "json" else "CHILD_STDOUT_MARKER"
        report.check(label + ": child stderr visible", "CHILD_STDERR_MARKER" in result.stderr)
        if as_json:
            payload = report.json_result(label, result)
            report.check(
                label + ": child stdout routed to stderr",
                marker in result.stderr and marker not in result.stdout,
            )
            if payload is not None:
                report.check(
                    label + ": direct selection unchanged",
                    set(payload.get("modules", [])) == selected,
                    payload.get("modules"),
                )
                report.check(
                    label + ": inventory unchanged",
                    payload.get("changed") == ["src/evallab/alpha.py"],
                    payload.get("changed"),
                )
                if invocation.is_file():
                    forwarded = json.loads(invocation.read_text())
                    command = payload.get("command", "")
                    report.check(
                        label + ": plan command forwarded",
                        bool(command) and shlex.split(command)[1:] == forwarded,
                    )
        else:
            report.observations.append(
                {
                    "label": label,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            report.check(
                label + ": native interactive stdout",
                marker in result.stdout and marker not in result.stderr,
            )
    invocation = fixture / "dry-run-invocation.json"
    result = run_cli(
        workspace,
        fixture,
        ["registry", "devloop", "--json"],
        {
            "PROBE_INVOCATION": str(invocation),
            "PROBE_CHILD_STATUS": "9",
            "PROBE_CHILD_KIND": "plain",
        },
    )
    payload = report.json_result("devloop plan only", result)
    report.check("devloop plan only: no child", result.returncode == 0 and not invocation.exists())
    if payload is not None:
        report.check(
            "devloop plan only: direct selection unchanged",
            set(payload.get("modules", [])) == selected,
        )


CONTEXT_PROGRAM = """import json, sys
from pathlib import Path
from evallab.contextpack import build_context_pack, render_mission_brief_template
root = Path(sys.argv[1])
paths = ['src/evallab/probe_module.py']
def pack(budget, scoped=True):
    result = build_context_pack('builder', root=root, paths=paths if scoped else None, token_budget=budget)
    return {'metadata': result.to_dict(), 'markdown': result.markdown}
full = pack(None)
upper = full['metadata']['estimated_tokens']
budgets = sorted({1, 500, max(1, upper - 1), *range(750, upper, 250)})
print(json.dumps({'full': full, 'unlimited_zero': pack(0), 'repeat_full': pack(None),
                  'unscoped': pack(None, False), 'brief': render_mission_brief_template('builder'),
                  'bounded': [{'budget': budget, **pack(budget)} for budget in budgets]}))
"""


def grade_contextpack(workspace: Path, fixture: Path, report: Report) -> None:
    needle = "src/evallab/probe_module.py"
    one = (
        "## Contract\nReference `"
        + needle
        + "`.\nSEMANTIC_ONE_SENTINEL\n\n```python\nvalue = 'keep exact whitespace'\n```\n"
        + ("Verbatim ordinary semantic detail.\n" * 90)
    )
    two = (
        "## Boundary\nUse `"
        + needle
        + "` without rewording.\nSEMANTIC_TWO_SENTINEL\n"
        + ("A separate matching semantic section stays intact.\n" * 40)
    )
    now = "# Current state\nNOW_SAFETY_SENTINEL\n" + (
        "Current approval and safety routing remain authoritative.\n" * 30
    )
    bodies = {
        "NOW.md": now,
        "ordinary.md": "# Ordinary guide\nUnmatched introduction.\n\n"
        + one
        + "\n"
        + two
        + "\n## Appendix\nUNMATCHED_APPENDIX_SENTINEL\n",
        "INDEX.md": "# Generated index\nINDEX_INVENTORY_SENTINEL\n"
        + ("Inventory reference `" + needle + "` with generated locator details.\n") * 180,
        "repo-map.md": "# Generated map\nMAP_INVENTORY_SENTINEL\n"
        + ("Generated symbol for `" + needle + "` and lookup coordinates.\n") * 150,
        "unrelated.md": "# Other guide\nUNRELATED_SCOPE_SENTINEL\n",
        "historical.md": "# Historical\nHISTORICAL_SENTINEL `" + needle + "`\n",
    }
    docs = fixture / "docs"
    docs.mkdir()
    for filename, body in bodies.items():
        status = "historical" if filename == "historical.md" else "living"
        (docs / filename).write_text(
            f"---\nstatus: {status}\naudience: [builder]\n---\n\n" + body, encoding="utf-8"
        )
    result = subprocess.run(
        [sys.executable, "-B", "-c", CONTEXT_PROGRAM, str(fixture)],
        cwd=fixture,
        env=environment(fixture, workspace),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=90,
        check=False,
    )
    observations = report.json_result("contextpack public function", result)
    report.check("contextpack function completed", result.returncode == 0, result.returncode)
    if observations is None:
        return
    full = observations["full"]
    expected = {"docs/NOW.md", "docs/ordinary.md", "docs/INDEX.md", "docs/repo-map.md"}

    def included(item: dict) -> set[str]:
        return {doc["path"] for doc in item["metadata"]["docs"]}

    # Inspect public result data, not the compiler's internal drop algorithm.
    report.check(
        "Unbudgeted scoped corpus unchanged", included(full) == expected, sorted(included(full))
    )
    report.check(
        "Unbudgeted semantic sections verbatim",
        one.strip() in full["markdown"] and two.strip() in full["markdown"],
    )
    report.check(
        "Unmatched semantic sections excluded",
        "UNMATCHED_APPENDIX_SENTINEL" not in full["markdown"],
    )
    report.check(
        "Unrelated/historical scoped docs excluded",
        all(
            marker not in full["markdown"]
            for marker in ("UNRELATED_SCOPE_SENTINEL", "HISTORICAL_SENTINEL")
        ),
    )
    report.check("Deterministic public output", full == observations["repeat_full"])
    report.check(
        "Nonpositive budget remains unlimited",
        full["markdown"] == observations["unlimited_zero"]["markdown"]
        and included(observations["unlimited_zero"]) == expected,
    )
    report.check(
        "Unscoped audience corpus unchanged",
        included(observations["unscoped"]) == expected | {"docs/unrelated.md"},
    )
    scores = full["metadata"].get("doc_scores", {})
    report.check(
        "High-reference inventories still scored above ordinary docs",
        scores.get("docs/INDEX.md", 0)
        > scores.get("docs/ordinary.md", 0)
        > scores.get("docs/NOW.md", -1)
        and scores.get("docs/repo-map.md", 0) > scores.get("docs/ordinary.md", 0),
    )
    inventories = {"docs/INDEX.md", "docs/repo-map.md"}
    inventory_first = now_last = truthful = verbatim = True
    saw_inventory_shedding = saw_ordinary_shedding = False
    violations = []
    markers = {
        "docs/NOW.md": "NOW_SAFETY_SENTINEL",
        "docs/ordinary.md": "SEMANTIC_ONE_SENTINEL",
        "docs/INDEX.md": "INDEX_INVENTORY_SENTINEL",
        "docs/repo-map.md": "MAP_INVENTORY_SENTINEL",
    }
    for item in observations["bounded"]:
        retained = included(item)
        dropped = expected - retained
        metadata = item["metadata"]
        text = item["markdown"]
        bad_inventory = "docs/ordinary.md" not in retained and bool(retained & inventories)
        bad_now = "docs/NOW.md" not in retained and bool(retained)
        inventory_first &= not bad_inventory
        now_last &= not bad_now
        if bad_inventory or bad_now:
            violations.append({"budget": item["budget"], "retained": sorted(retained)})
        saw_inventory_shedding |= (
            bool(dropped & inventories) and {"docs/NOW.md", "docs/ordinary.md"} <= retained
        )
        saw_ordinary_shedding |= retained == {"docs/NOW.md"}
        truthful &= (
            set(metadata.get("dropped_items", [])) == dropped
            and bool(metadata.get("truncated")) == bool(dropped)
            and metadata.get("estimated_tokens") == (len(text) + 3) // 4
            and all((marker in text) == (path in retained) for path, marker in markers.items())
            and observations["brief"].strip() in text
        )
        if "docs/ordinary.md" in retained:
            verbatim &= one.strip() in text and two.strip() in text
    report.check("Generated inventories shed before ordinary docs", inventory_first, violations)
    report.check("NOW sheds last", now_last, violations)
    report.check(
        "Boundaries include inventory-only and ordinary shedding",
        saw_inventory_shedding and saw_ordinary_shedding,
    )
    report.check("Retained matching sections remain verbatim", verbatim)
    report.check("Omissions, size and protected brief are truthful", truthful)
    extreme = next(item for item in observations["bounded"] if item["budget"] == 1)
    report.check(
        "Extreme positive budget honestly omits all docs",
        not included(extreme) and set(extreme["metadata"].get("dropped_items", [])) == expected,
    )
    report.check(
        "Extreme positive budget reports protected oversize",
        extreme["metadata"].get("estimated_tokens", 0) > 1
        and extreme["metadata"].get("truncated") is True
        and extreme["metadata"].get("tokens_shed", 0) > 0,
    )


def make_trajectory(fixture: Path) -> tuple[Path, list[tuple[Path, int]], list[str]]:
    trial = fixture / "trial"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    markers = ["HEAD_USER_BODY", "HEAD_AGENT_BODY", "CONT_FIRST_BODY", "CONT_LAST_BODY"]

    def step(native_id: int, message: str, source: str = "agent") -> dict:
        return {
            "step_id": native_id,
            "source": source,
            "message": message,
            "metrics": {"prompt_tokens": 7, "completion_tokens": 2, "cached_tokens": 1},
        }

    head = agent / "trajectory.json"
    tail = agent / "trajectory.cont-1.json"
    common = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "fixture", "model_name": "fixture-model"},
    }
    write_json(
        head,
        {
            **common,
            "session_id": "head",
            "continued_trajectory_ref": tail.name,
            "steps": [step(10, markers[0], "user"), step(40, markers[1])],
        },
    )
    write_json(
        tail,
        {
            **common,
            "session_id": "tail",
            "steps": [step(3, markers[2]), step(90, markers[3])],
            "final_metrics": {
                "total_prompt_tokens": 97531,
                "total_completion_tokens": 2468,
                "total_cached_tokens": 1234,
            },
        },
    )
    write_json(
        trial / "result.json",
        {"id": "fixture-trial", "trial_name": "trial", "task_name": "fixture-task"},
    )
    return trial, [(head, 10), (head, 40), (tail, 3), (tail, 90)], markers


def grade_traj(workspace: Path, baseline: Path, fixture: Path, report: Report) -> None:
    trial, coordinates, markers = make_trajectory(fixture)
    base_args = ["traj", "outline", str(trial)]
    reference_result = run_cli(baseline, fixture, [*base_args, "--json"])
    reference = report.json_result("Pinned native full CLI reference", reference_result)
    report.check("Pinned full CLI reference available", reference_result.returncode == 0)
    full_result = run_cli(workspace, fixture, [*base_args, "--json"])
    full = report.json_result("Unselected full JSON", full_result)
    report.check("Unselected full JSON succeeds", full_result.returncode == 0)
    if full is None or reference is None:
        return
    report.check("Unselected full JSON unchanged", full == reference)
    steps = full.get("steps", [])
    report.check(
        "Logical IDs independent of noncontiguous native IDs",
        [step.get("step_id") for step in steps] == [1, 2, 3, 4],
    )
    expected_totals = {
        "total_prompt_tokens": 97531,
        "total_completion_tokens": 2468,
        "total_cached_tokens": 1234,
        "total_cost_usd": None,
        "final_metrics_present": True,
    }
    report.check(
        "Full native totals authoritative and unknown cost stays unknown",
        all(full.get(key) == value for key, value in expected_totals.items()),
    )
    report.check(
        "Full native original source coordinates",
        len(steps) == 4
        and all(
            step.get("source_path") == str(path)
            and step.get("source_step_id") == native_id
            and step.get("source_sha256") == digest(path.read_bytes())
            for step, (path, native_id) in zip(steps, coordinates, strict=False)
        ),
    )
    for selector, expected_steps in (
        (["--summary"], []),
        (["--steps", "2:3"], steps[1:3]),
        (["--steps", "1:1"], steps[:1]),
        (["--steps", "4:4"], steps[3:4]),
    ):
        label = "JSON " + " ".join(selector)
        result = run_cli(workspace, fixture, [*base_args, *selector, "--json"])
        payload = report.json_result(label, result)
        report.check(label + ": succeeds", result.returncode == 0)
        if payload is None:
            continue
        report.check(
            label + ": exact selected step records", payload.get("steps", []) == expected_steps
        )
        report.check(
            label + ": all nonstep full-run fields unchanged",
            all(payload.get(key) == value for key, value in full.items() if key != "steps"),
        )
        if selector == ["--summary"]:
            report.check(
                label + ": no step bodies leaked",
                not any(marker in result.stdout for marker in markers),
            )
    for selector, selected_indexes in ((["--summary"], []), (["--steps", "2:3"], [1, 2])):
        result = run_cli(workspace, fixture, [*base_args, *selector])
        label = "Text " + " ".join(selector)
        report.observations.append(
            {
                "label": label,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        report.check(label + ": succeeds", result.returncode == 0)
        report.check(
            label + ": full native token totals visible",
            all(str(value) in result.stdout.replace(",", "") for value in (97531, 2468, 1234)),
        )
        report.check(
            label + ": bounded step bodies",
            all(
                (marker in result.stdout) == (index in selected_indexes)
                for index, marker in enumerate(markers)
            ),
        )
        for index in selected_indexes:
            path, native_id = coordinates[index]
            report.check(
                label + f": native citation for logical {index + 1}",
                str(path) in result.stdout
                and str(native_id) in result.stdout
                and digest(path.read_bytes()) in result.stdout,
            )
    for selector in (
        ["--steps"],
        ["--steps", "1"],
        ["--steps", "0:1"],
        ["--steps", "3:2"],
        ["--steps", "x:y"],
        ["--steps", "1:2:3"],
        ["--steps", "999:1000"],
    ):
        result = run_cli(workspace, fixture, [*base_args, *selector, "--json"])
        label = "Reject " + " ".join(selector)
        report.check(
            label,
            result.returncode != 0 and bool(result.stderr.strip()),
            {"returncode": result.returncode, "stderr": result.stderr},
        )
    # Known spend must survive selection just as unknown spend does.
    tail = coordinates[-1][0]
    tail_data = json.loads(tail.read_text())
    tail_data["final_metrics"]["total_cost_usd"] = 1.2345
    write_json(tail, tail_data)
    known_result = run_cli(workspace, fixture, [*base_args, "--summary", "--json"])
    known = report.json_result("Known native cost summary", known_result)
    report.check(
        "Known native cost retained",
        known_result.returncode == 0
        and known is not None
        and known.get("total_cost_usd") == 1.2345
        and known.get("total_prompt_tokens") == 97531,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task", required=True, choices=tuple(TASKS))
    parser.add_argument(
        "--output", required=True, type=Path, help="Fresh private JSON receipt outside workspace"
    )
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = args.output.resolve()
    if output.exists():
        parser.error("--output already exists; preserve prior evidence and choose a fresh path")
    if output.is_relative_to(workspace):
        parser.error("--output and fixtures must remain outside the solver workspace")
    output.parent.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix=output.stem + "-fixtures-", dir=output.parent))
    report = Report()
    error = None
    manifest = json.loads(args.manifest.read_text())
    try:
        if manifest["baseline_revision"] != BASELINE:
            raise ValueError("Unexpected pinned baseline revision")
        if manifest["tasks"][args.task]["allowed_paths"] != TASKS[args.task]["paths"]:
            raise ValueError("Manifest edit scope differs from public task scope")
        if not workspace.is_dir():
            raise FileNotFoundError(workspace)
        if check_scope(workspace, manifest, args.task, report):
            if args.task == "devloop_json":
                grade_devloop(workspace, fixture, report)
            elif args.task == "contextpack_priority":
                grade_contextpack(workspace, fixture, report)
            else:
                grade_traj(workspace, Path(manifest["baseline_workspace"]), fixture, report)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        report.check("Grader completed without infrastructure exception", False, error)
    passed = bool(report.checks) and all(item["passed"] for item in report.checks) and error is None
    payload = {
        "schema": "agent-efficiency-native-code-grade/v1",
        "task": args.task,
        "workspace": str(workspace),
        "fixture_root": str(fixture),
        "baseline_revision": BASELINE,
        "manifest_sha256": digest(args.manifest.read_bytes()),
        "status": "ERROR" if error else "PASS" if passed else "FAIL",
        "passed": passed,
        "checks": report.checks,
        "observations": report.observations,
        "infrastructure_error": error,
        "limitations": [
            "Bounded public behavioral acceptance, not proof of all correctness.",
            "Native local source snapshots are not hermetic Harbor trials or an adversarial sandbox.",
            "Devloop child is a controlled executable; it does not invoke actual pytest or install dependencies.",
            "Scope ignores only native Git metadata and conventional Python/tool cache directories.",
            "No LLM judge or outcome-based exclusion; retain ERROR/FAIL receipts as attempted cases.",
        ],
    }
    write_json(output, payload)
    print(
        json.dumps(
            {
                "task": args.task,
                "status": payload["status"],
                "receipt": str(output),
                "checks": len(report.checks),
                "failed": [item["name"] for item in report.checks if not item["passed"]],
            },
            indent=2,
        )
    )
    return 0 if passed else 2 if error else 1


if __name__ == "__main__":
    raise SystemExit(main())
