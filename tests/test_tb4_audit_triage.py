"""Blind static triage on tb4-audit-v1 views: sealed reads, planted defects, scoring."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab.qa.ask import WorkspaceTools
from evallab.registry import task_directory_digest

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "research/answer-keys/tb4-audit-v1"
sys.path.insert(0, str(KEY))
try:
    _spec = importlib.util.spec_from_file_location("tb4_triage_signals", KEY / "triage_signals.py")
    assert _spec is not None and _spec.loader is not None
    triage = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = triage  # dataclasses resolve annotations via sys.modules
    _spec.loader.exec_module(triage)
finally:
    sys.path.remove(str(KEY))
views = triage.views
scorer = triage.score_audit

SEPARATE = '[verifier]\nenvironment_mode = "separate"\ntimeout_sec = 600.0\n'
TEST_SH = (
    "#!/bin/bash\npytest /tests/test_outputs.py -rA\n"
    "if [ $? -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; "
    "else echo 0 > /logs/verifier/reward.txt; fi\n"
)
CLEAN_TEST = """import json
from pathlib import Path


def test_total():
    data = json.loads(Path("/app/out.json").read_text())
    assert data["total"] == 42


def test_total_is_int():
    data = json.loads(Path("/app/out.json").read_text())
    assert isinstance(data["total"], int)
"""

TASKS: dict[str, dict[str, str]] = {
    # TS05: the grader requires keys the instruction and agent files never name.
    "literal-task": {
        "instruction.md": "Write `/app/out.json` containing the total of the input numbers.\n",
        "tests/test_outputs.py": """import json
from pathlib import Path


def test_hidden_schema():
    data = json.loads(Path("/app/out.json").read_text())
    assert data["checksum_sha"] == "x"
    assert data["region_code"] == "EU"
    assert data["batch_label"] == "nightly"
    assert data["total"] == 42
""",
    },
    # TS01: the instruction requires output fields that no test ever reads.
    "unread-task": {
        "instruction.md": (
            "Write `/app/report.json` with the fields `total_cost`, `max_delay_min` "
            "and `route_name` for the dispatch plan.\n"
        ),
        "tests/test_outputs.py": """import json
from pathlib import Path


def test_total_cost():
    data = json.loads(Path("/app/report.json").read_text())
    assert data["total_cost"] == 42
""",
    },
    # TS04: the grader imports agent-controlled code from /app into its own process.
    "tamper-task": {
        "instruction.md": "Implement `solve()` in `/app/solver.py` returning the total.\n",
        "tests/test_outputs.py": """import sys

sys.path.insert(0, "/app")


def test_solver_total():
    import solver

    assert solver.solve() == 42
""",
    },
    # TS09: the agent image and the separate verifier image run different Pythons.
    "env-task": {
        "environment/Dockerfile": "FROM python:3.13-slim-bookworm\nWORKDIR /app\n",
        "tests/Dockerfile": "FROM python:3.11-slim-bookworm\nRUN pip install pytest==9.1.1\n",
        "instruction.md": "Write `/app/out.json` with the `total` of the input numbers.\n",
        "tests/test_outputs.py": CLEAN_TEST,
    },
    # Control: none of the four planted detectors may fire.
    "clean-task": {
        "instruction.md": "Write `/app/out.json` with the `total` of the input numbers.\n",
        "tests/test_outputs.py": CLEAN_TEST,
    },
}
PLANTED = {
    "literal-task": ("test_only_string_literals", "unstated_requirement", "prompt_misaligned"),
    "unread-task": ("unread_output_mentions", "weak_check", "weak"),
    "tamper-task": ("agent_code_imported_into_grader", "grader_tamperable", "tamperable"),
    "env-task": ("env_python_mismatch", "env_mismatch_or_flaky", "broken"),
}
# The grader file a port regression test for each planted defect would cite.
LOCUS = {"env-task": "tests/Dockerfile"}


def _write_task(upstream: Path, name: str, files: dict[str, str]) -> dict[str, Any]:
    package = upstream / name
    defaults = {
        "task.toml": f'[task]\nname = "terminal-bench/{name}"\n\n{SEPARATE}',
        "environment/Dockerfile": "FROM python:3.12-slim-bookworm\nWORKDIR /app\n",
        "tests/Dockerfile": "FROM python:3.12-slim-bookworm\nRUN pip install pytest==9.1.1\n",
        "tests/test.sh": TEST_SH,
        "solution/solve.sh": "#!/bin/bash\necho '{\"total\": 42}' > /app/out.json\n",
    }
    for relative, text in (defaults | files).items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return {
        "split": "test",
        "scope": "task",
        "subject": {
            "kind": "task",
            "task_id": f"terminal-bench/{name}",
            "task_digest": task_directory_digest(package),
        },
        "package": {"name": name, "pin": "a" * 40},
    }


def _view(root: Path, tasks: dict[str, dict[str, str]] = TASKS, name: str = "view") -> Path:
    upstream = root / f"{name}-upstream"
    rows = [_write_task(upstream, task, files) for task, files in tasks.items()]
    destination = root / name
    views.materialize_agent_view(rows, destination, split="test", task_root=upstream)
    return destination


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """Scan with every detector enabled (the dev calibration may switch some off)."""
    view = _view(tmp_path_factory.mktemp("triage").resolve())
    return view, triage.scan_view(view, cfg=triage.UNCALIBRATED)


def _by_task(scan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for prediction in scan["predictions"]:
        out.setdefault(scorer.task_name(prediction["subject"]["task_id"]), []).append(prediction)
    return out


def test_scan_reads_only_through_the_sealed_capability(
    scanned: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    view, scan = scanned
    audit = scan["read_audit"]
    assert audit["all_reads_through_sealed_tools"] is True
    assert audit["direct_fs_reads_of_view_or_key_files"] == 0
    assert audit["fs_events_under_view_or_key_mediated_by_capability"] > 0
    assert audit["files_read_outside_inventory"] == []
    assert set(audit["tool_calls"]) == {"read"} and audit["tool_calls"]["read"] > 0

    calls: list[dict[str, Any]] = []
    original = views.SealedWorkspaceTools.run

    def spy(self: Any, tool: str, args: dict[str, Any]) -> str:
        calls.append(args)
        return original(self, tool, args)

    monkeypatch.setattr(views.SealedWorkspaceTools, "run", spy)
    again = triage.scan_view(view, cfg=triage.UNCALIBRATED)
    assert len(calls) == again["read_audit"]["tool_calls"]["read"]
    assert again["predictions"] == scan["predictions"]  # deterministic


def test_the_read_audit_catches_a_direct_filesystem_read(tmp_path: Path) -> None:
    view = _view(tmp_path.resolve(), {"clean-task": TASKS["clean-task"]})
    with triage.fs_audit(view) as audit:
        tools = views.agent_tools(view)
        triage.SealedReader(tools).text("tasks/clean-task/instruction.md", 50)
        assert audit.mediated > 0 and audit.direct == []
        (view / "tasks/clean-task/tests/test_outputs.py").read_text()
    assert len(audit.direct) == 1 and audit.direct[0].endswith("tests/test_outputs.py")


def test_reader_refuses_unsealed_tools_and_rechecks_the_seal(tmp_path: Path) -> None:
    view = _view(tmp_path.resolve(), {"clean-task": TASKS["clean-task"]})
    with pytest.raises(TypeError, match="SealedWorkspaceTools"):
        triage.SealedReader(WorkspaceTools(view, {"inputs.jsonl"}))
    reader = triage.SealedReader(views.agent_tools(view))
    changed = view / "tasks/clean-task/instruction.md"
    changed.chmod(0o600)
    changed.write_text("Mutated after export.\n")
    with pytest.raises(ValueError, match="changed after export"):
        reader.text("tasks/clean-task/instruction.md", 10)


def test_chunked_reads_and_clipped_listings_recover_every_byte(tmp_path: Path) -> None:
    long_lines = "\n".join(f"line {i:04d} " + "x" * 120 for i in range(450)) + "\n"
    data = {f"tests/data/{'d' * 80}_{i:03d}.json": "{}\n" for i in range(220)}
    view = _view(
        tmp_path.resolve(),
        {"big-task": {**TASKS["clean-task"], "tests/helpers.py": long_lines, **data}},
    )
    tools = views.agent_tools(view)
    reader = triage.SealedReader(tools)
    assert reader.lines("tasks/big-task/tests/helpers.py", 3000) == long_lines.splitlines()
    listed = reader.listing("tasks/big-task/tests")
    assert reader.listing_clips >= 1
    assert set(listed) == {n for n in tools.files if n.startswith("tasks/big-task/tests/")}


@pytest.mark.parametrize("task", sorted(PLANTED))
def test_planted_static_defects_are_detected(
    scanned: tuple[Path, dict[str, Any]], task: str
) -> None:
    _, scan = scanned
    detector, taxonomy_class, category = PLANTED[task]
    assert detector in scan["tasks"][task]["detectors"]
    hits = [p for p in _by_task(scan)[task] if p["id"].split(":")[2] == detector]
    assert hits and all(p["root_cause"] == [taxonomy_class] for p in hits)
    assert all(p["category"] == category for p in hits)
    assert any(ref.startswith(f"tasks/{task}/") for ref in hits[0]["evidence_refs"])


def test_features_record_the_planted_surfaces(scanned: tuple[Path, dict[str, Any]]) -> None:
    _, scan = scanned
    features = {task: row["features"] for task, row in scan["tasks"].items()}
    assert features["literal-task"]["test_only_string_literals"] >= 3
    assert features["unread-task"]["unread_output_mentions"] == 2
    assert features["tamper-task"]["dockerfile_has_sys_path_app"] is True
    assert (features["env-task"]["agent_python"], features["env-task"]["verifier_python"]) == (
        "3.13",
        "3.11",
    )
    assert features["env-task"]["env_python_mismatch"] is True
    assert features["clean-task"]["task_toml_env_mode"] == "separate"
    clean = scan["tasks"]["clean-task"]["detectors"]
    assert not {d for d, _, _ in PLANTED.values()} & set(clean)
    for name in (
        "test_file_count",
        "test_assertion_count",
        "instruction_code_tokens",
        "solution_loc",
        "test_only_numeric_constants",
        "has_random_or_time_in_solution_without_repeat_test",
        "has_surface_only_workbook_or_file_check",
    ):
        assert name in features["clean-task"]


def test_predictions_are_valid_findings_with_real_evidence(
    scanned: tuple[Path, dict[str, Any]],
) -> None:
    view, scan = scanned
    tools = views.agent_tools(view)
    assert triage.validate_predictions(scan["predictions"], tools) == len(scan["predictions"])
    reader = triage.SealedReader(tools)
    for prediction in scan["predictions"]:
        assert prediction["schema"] == "qa.finding/v1" and prediction["verdict"] == "fail"
        assert prediction["reviewer"]["cost_usd"] == 0.0 and prediction["measurement"] == "not_run"
        assert prediction["root_cause"][0] in triage.CLASSES
        for ref in prediction["evidence_refs"]:
            path, _, symbol = ref.partition("::")
            path = path.rsplit(":", 1)[0] if path.rsplit(":", 1)[-1].isdigit() else path
            assert path in tools.files
            if symbol:
                assert symbol in reader.text(path, 3000)
    with pytest.raises(ValueError, match="outside the view inventory"):
        forged = dict(scan["predictions"][0], evidence_refs=["tasks/clean-task/tests/forged.py"])
        triage.validate_predictions([forged], tools)


def _key_items() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    class_ids = {k: v[0] for k, v in triage.CLASSES.items()}
    items = []
    for task, (_, taxonomy_class, category) in sorted(PLANTED.items()):
        items.append(
            {
                "item_id": f"planted:{task}",
                "scope": "task",
                "task": task,
                "split": "test",
                "expected_category": category,
                "taxonomy_class": taxonomy_class,
                "taxonomy_class_id": class_ids[taxonomy_class],
                "label_strength": "pm_reported",
                "auditor_evidence": {"counted_source_count": 1},
                "port": {"tests": [{"path": f"{task}/{LOCUS.get(task, 'tests/test_outputs.py')}"}]},
            }
        )
    tasks = [
        {"task": task, "split": "test", "exposure_stratum": "unexposed", "historical_exposures": []}
        for task in TASKS
    ]
    return items, tasks


def test_candidates_score_cleanly_through_score_audit(scanned: tuple[Path, dict[str, Any]]) -> None:
    _, scan = scanned
    items, tasks = _key_items()
    key = scorer.build_key(items, tasks)
    report = scorer.score(key, scan["predictions"])
    assert report["predictions"]["scored"] == len(scan["predictions"])
    assert report["predictions"].get("off_key_task", 0) == 0
    for mode in ("locus_and_class", "category_1to1", "task_any_1to1"):
        cell = report["modes"][mode]["headline"]["unexposed_test"]
        assert cell["recall"]["k"] == cell["recall"]["n"] == len(PLANTED)
    planted_ids = {f"planted:{t}" for t in PLANTED}
    assert {f for _, f in report["matches"]["locus_and_class"]} == planted_ids
    credited = triage._credited(key, report, scan["predictions"])
    directions = triage.direction_breakdown(key, credited, scan["predictions"])
    by_dir = directions["test"]["category_1to1"]
    assert by_dir["wrong_pass_TS01_TS04"]["n"] == 2 and by_dir["wrong_fail_TS05_TS10"]["n"] == 2
    assert by_dir["wrong_pass_TS01_TS04"]["k"] == 2 and by_dir["wrong_fail_TS05_TS10"]["k"] == 2


def test_python_version_inference() -> None:
    assert triage.python_version("FROM python:3.13-slim-bookworm\n")[0] == "3.13"
    assert triage.python_version("FROM ubuntu:24.04\n")[0] == "3.12"
    assert (
        triage.python_version("FROM mcr.microsoft.com/playwright/python:v1.52.0-noble\n")[0]
        == "3.12"
    )
    assert triage.python_version("FROM ubuntu:24.04\n", "uv run --python 3.11 pytest")[0] == "3.11"
    assert triage.python_version("FROM node:20\n")[0] is None


def test_frozen_calibration_is_dev_only_and_serializable(tmp_path: Path) -> None:
    assert set(triage.CALIBRATION) == set(triage.UNCALIBRATED)
    assert json.loads(json.dumps(triage.CALIBRATION)) == triage.CALIBRATION
    assert set(triage.CALIBRATION["disabled_detectors"]) <= {
        "unread_output_mentions",
        "determinism_unchecked",
        "surface_only_document_check",
        "fixed_visible_inputs",
        "low_assertion_count",
        "timing_gate",
        "agent_source_inspection",
        "unguarded_float_parse",
        "answer_like_environment_files",
        "agent_code_imported_into_grader",
        "agent_code_runs_in_verifier",
        "self_reported_result_marker",
        "agent_controls_verifier_dependencies",
        "in_process_timing",
        "shared_grading_environment",
        "test_only_string_literals",
        "test_only_numeric_constants",
        "scored_instruction_binary_reward",
        "exact_equality_oracle",
        "env_python_mismatch",
        "grade_time_network_or_install",
        "short_wall_clock_limits",
    }
    view = _view(tmp_path.resolve(), {"clean-task": TASKS["clean-task"]})
    scan = triage.scan_view(view, keep_surfaces=True)
    items, tasks = _key_items()
    key = scorer.build_key(items, tasks)
    with pytest.raises(ValueError, match="dev-split"):
        triage.calibrate(scan["surfaces"], key)
