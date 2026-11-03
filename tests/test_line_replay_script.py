"""Synthetic standard replay entrypoint contracts; no retained data or Station I/O."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path, value):
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _group(recall=(2, 4), alarms=(1, 5)):
    return {
        "cheats": recall[1],
        "clean": alarms[1],
        "metrics": {
            "drop_or_review_recall": {"k": recall[0], "n": recall[1], "rate": -99},
            "clean_false_alarm_rate": {"k": alarms[0], "n": alarms[1], "rate": -99},
        },
    }


@pytest.fixture
def replay_case(tmp_path, monkeypatch):
    module_spec = importlib.util.spec_from_file_location(
        "line_replay_script", ROOT / "scripts/line-replay.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    root = tmp_path / "set"
    root.mkdir()
    _write_json(root / "spec.json", {"schema": "evallab.line_spec/v1", "split": "dev"})
    (root / "rollouts.jsonl").write_bytes(b"alpha\n")
    scoreboard = {"by_corpus": {"synthetic": _group()}}
    _write_json(root / "baseline.json", {"scoreboard": scoreboard})
    files = []
    for name in ("spec.json", "rollouts.jsonl"):
        data = (root / name).read_bytes()
        files.append(
            {
                "path": name,
                "sha256": sha256(data).hexdigest(),
                "size": len(data),
                "kind": "synthetic",
            }
        )
    manifest = {
        "schema": "evallab.line_replay_manifest/v1",
        "files": files,
        "input_files": len(files),
        "run_count": 9,
        "corpora": {"synthetic": 9},
    }
    _write_json(root / "MANIFEST.json", manifest)
    batch = root / "batches" / "synthetic"
    batch.mkdir(parents=True)
    _write_json(batch / "scoreboard.json", scoreboard)
    result = {"batch": str(batch), "passed": True, "changed_runs": []}
    case = SimpleNamespace(
        module=module,
        root=root,
        batch=batch,
        manifest=manifest,
        result=result,
        scoreboard=scoreboard,
        calls=[],
        status=0,
    )

    def fake_replay(spec_path, baseline, repo_root):
        case.calls.append((spec_path, baseline, repo_root))
        return case.result, case.status

    monkeypatch.setattr(module, "replay", fake_replay)
    monkeypatch.setenv("EVALLAB_LINE_REPLAY_SET", str(root))
    return case


@pytest.mark.parametrize("status", [0, 1, 2])
def test_replay_exit_status_is_preserved(replay_case, status, capsys):
    case = replay_case
    case.status = status
    case.result["passed"] = status == 0
    if status == 2:
        case.result["error"] = "synthetic replay invalid"
    assert case.module.main([]) == status
    assert len(case.calls) == 1
    output = capsys.readouterr()
    assert str(case.batch) in output.out
    assert {0: "PASS", 1: "REGRESSION", 2: "INVALID"}[status] in output.out
    if status == 2:
        assert "synthetic replay invalid" in output.err


@pytest.mark.parametrize("component", [None, "spec.json", "MANIFEST.json", "baseline.json"])
def test_missing_required_data_is_explicit_skip(replay_case, component, tmp_path, capsys):
    case = replay_case
    if component is None:
        args = ["--set", str(tmp_path / "absent")]
    else:
        (case.root / component).unlink()
        args = []
    assert case.module.main(args) == 77
    assert "SKIP" in capsys.readouterr().err
    assert not case.calls


def test_set_override_takes_precedence_over_environment(replay_case, monkeypatch, tmp_path):
    case = replay_case
    monkeypatch.setenv("EVALLAB_LINE_REPLAY_SET", str(tmp_path / "absent"))
    assert case.module.main(["--set", str(case.root)]) == 0


def test_same_size_tampering_is_invalid_before_replay(replay_case, capsys):
    case = replay_case
    (case.root / "rollouts.jsonl").write_bytes(b"omega\n")
    assert case.module.main([]) == 2
    assert "rollouts.jsonl" in capsys.readouterr().err
    assert not case.calls


def test_hash_covers_all_stream_chunks(replay_case):
    case = replay_case
    path = case.root / "rollouts.jsonl"
    data = b"x" * (1024 * 1024 + 123)
    path.write_bytes(data)
    case.manifest["files"][1].update(size=len(data), sha256=sha256(data).hexdigest())
    _write_json(case.root / "MANIFEST.json", case.manifest)
    with path.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"y")
    assert case.module.main([]) == 2
    assert not case.calls


def test_missing_listed_input_is_invalid_not_skip(replay_case, capsys):
    case = replay_case
    (case.root / "rollouts.jsonl").unlink()
    assert case.module.main([]) == 2
    output = capsys.readouterr().err
    assert "rollouts.jsonl" in output
    assert "SKIP" not in output
    assert not case.calls


@pytest.mark.parametrize(
    "path", ["../outside.jsonl", "/tmp/outside.jsonl", "nested/../../outside.jsonl"]
)
def test_manifest_path_traversal_is_rejected(replay_case, path, capsys):
    case = replay_case
    case.manifest["files"][1]["path"] = path
    _write_json(case.root / "MANIFEST.json", case.manifest)
    assert case.module.main([]) == 2
    assert "escapes set" in capsys.readouterr().err
    assert not case.calls


def test_symlink_cannot_escape_set(replay_case, tmp_path):
    case = replay_case
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"alpha\n")
    path = case.root / "rollouts.jsonl"
    path.unlink()
    path.symlink_to(outside)
    assert case.module.main([]) == 2
    assert not case.calls


@pytest.mark.parametrize(
    ("field", "value"),
    [("sha256", "not-a-hash"), ("size", True), ("size", -1), ("kind", None), ("source", 3)],
)
def test_malformed_manifest_file_metadata_is_invalid(replay_case, field, value):
    case = replay_case
    case.manifest["files"][1][field] = value
    _write_json(case.root / "MANIFEST.json", case.manifest)
    assert case.module.main([]) == 2
    assert not case.calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "unknown"),
        ("files", {}),
        ("input_files", 999),
        ("run_count", True),
        ("corpora", {"synthetic": 8}),
    ],
)
def test_malformed_manifest_structure_is_invalid(replay_case, field, value):
    case = replay_case
    case.manifest[field] = value
    _write_json(case.root / "MANIFEST.json", case.manifest)
    assert case.module.main([]) == 2
    assert not case.calls


def test_duplicate_manifest_input_is_invalid(replay_case):
    case = replay_case
    case.manifest["files"].append(deepcopy(case.manifest["files"][0]))
    case.manifest["input_files"] += 1
    _write_json(case.root / "MANIFEST.json", case.manifest)
    assert case.module.main([]) == 2
    assert not case.calls


def test_spec_must_be_in_frozen_manifest(replay_case):
    case = replay_case
    case.manifest["files"].pop(0)
    case.manifest["input_files"] -= 1
    _write_json(case.root / "MANIFEST.json", case.manifest)
    assert case.module.main([]) == 2
    assert not case.calls


@pytest.mark.parametrize("name", ["MANIFEST.json", "baseline.json"])
@pytest.mark.parametrize("content", ["{broken", "[]"])
def test_invalid_required_json_is_status_two(replay_case, name, content):
    case = replay_case
    (case.root / name).write_text(content, encoding="utf-8")
    assert case.module.main([]) == 2
    assert not case.calls


def test_io_failure_is_status_two(replay_case, monkeypatch):
    case = replay_case

    def unreadable(path):
        raise PermissionError("synthetic unreadable input")

    monkeypatch.setattr(case.module, "_digest", unreadable)
    assert case.module.main([]) == 2
    assert not case.calls


@pytest.mark.parametrize("error", [OSError("synthetic I/O"), ValueError("synthetic value")])
def test_api_io_and_value_failures_are_invalid(replay_case, monkeypatch, error):
    case = replay_case

    def fail(*args):
        raise error

    monkeypatch.setattr(case.module, "replay", fail)
    assert case.module.main([]) == 2


def test_optional_provenance_is_checked_when_present(replay_case):
    case = replay_case
    provenance = {
        "baseline_sha256": sha256((case.root / "baseline.json").read_bytes()).hexdigest(),
        "manifest_sha256": sha256((case.root / "MANIFEST.json").read_bytes()).hexdigest(),
        "code_rev": "synthetic-revision",
        "reason": "synthetic fixture",
    }
    _write_json(case.root / "baseline-provenance.json", provenance)
    assert case.module.main([]) == 0


@pytest.mark.parametrize("field", ["baseline_sha256", "manifest_sha256"])
def test_provenance_hash_mismatch_prevents_replay(replay_case, field):
    case = replay_case
    provenance = {
        "baseline_sha256": sha256((case.root / "baseline.json").read_bytes()).hexdigest(),
        "manifest_sha256": sha256((case.root / "MANIFEST.json").read_bytes()).hexdigest(),
    }
    provenance[field] = "0" * 64
    _write_json(case.root / "baseline-provenance.json", provenance)
    assert case.module.main([]) == 2
    assert not case.calls


def test_summary_uses_metric_counts_not_cached_rates(replay_case, capsys):
    case = replay_case
    current = {"by_corpus": {"synthetic": _group(recall=(3, 4), alarms=(0, 5))}}
    _write_json(case.batch / "scoreboard.json", current)
    assert case.module.main([]) == 0
    out = capsys.readouterr().out
    assert "2/4 (50.0%) -> 3/4 (75.0%)" in out
    assert "1/5 (20.0%) -> 0/5 (0.0%)" in out
    assert "-99" not in out


def test_summary_zero_denominators_are_not_rates(replay_case, capsys):
    case = replay_case
    scoreboard = {"by_corpus": {"synthetic": _group(recall=(0, 0), alarms=(0, 0))}}
    _write_json(case.root / "baseline.json", {"scoreboard": scoreboard})
    _write_json(case.batch / "scoreboard.json", scoreboard)
    assert case.module.main([]) == 0
    assert "0/0 (n/a) -> 0/0 (n/a)" in capsys.readouterr().out


@pytest.mark.parametrize("status", [0, 1, 2])
@pytest.mark.parametrize("failure", ["no-batch", "scoreboard", "denominator", "result"])
def test_invalid_summary_never_replaces_replay_exit(replay_case, status, failure, capsys):
    case = replay_case
    case.status = status
    if failure == "no-batch":
        case.result.pop("batch")
        case.result["error"] = "synthetic early replay failure"
    elif failure == "scoreboard":
        (case.batch / "scoreboard.json").write_text("broken", encoding="utf-8")
    elif failure == "denominator":
        case.scoreboard["by_corpus"]["synthetic"]["cheats"] = 99
        _write_json(case.batch / "scoreboard.json", case.scoreboard)
    else:
        case.result = None
    assert case.module.main([]) == status
    assert "summary" in capsys.readouterr().err.lower()


def test_all_changed_decisions_are_saved_without_flooding_summary(replay_case, capsys):
    case = replay_case
    changes = [
        {"corpus": "synthetic", "run_id": f"sample-{i}", "before": "keep", "after": "review"}
        for i in range(50)
    ]
    case.result["changed_runs"] = changes
    assert case.module.main([]) == 0
    report = case.batch / "changed-decisions.json"
    assert json.loads(report.read_text(encoding="utf-8")) == changes
    out = capsys.readouterr().out
    assert "Changed decisions: 50" in out
    assert str(report) in out
    assert "sample-49" not in out


def test_replay_leaves_frozen_files_unchanged(replay_case):
    case = replay_case
    frozen = {path: path.read_bytes() for path in case.root.iterdir() if path.is_file()}
    assert case.module.main([]) == 0
    assert all(path.read_bytes() == original for path, original in frozen.items())


def test_malformed_spec_with_matching_hash_is_invalid(replay_case):
    case = replay_case
    data = b"{not json"
    (case.root / "spec.json").write_bytes(data)
    case.manifest["files"][0].update(size=len(data), sha256=sha256(data).hexdigest())
    _write_json(case.root / "MANIFEST.json", case.manifest)
    assert case.module.main([]) == 2
    assert not case.calls


def test_malformed_optional_provenance_is_invalid(replay_case):
    case = replay_case
    (case.root / "baseline-provenance.json").write_text("[]", encoding="utf-8")
    assert case.module.main([]) == 2
    assert not case.calls


def test_changed_report_io_failure_preserves_regression_exit(replay_case, monkeypatch, capsys):
    case = replay_case
    case.status = 1
    case.result["changed_runs"] = [
        {"corpus": "synthetic", "run_id": "sample", "before": "drop", "after": "keep"}
    ]
    original = Path.write_text

    def write_text(path, *args, **kwargs):
        if path == case.batch / "changed-decisions.json":
            raise PermissionError("synthetic report unwritable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    assert case.module.main([]) == 1
    assert "synthetic report unwritable" in capsys.readouterr().err
