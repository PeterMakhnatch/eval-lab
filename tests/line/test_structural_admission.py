"""Structural admission of a native rollout, end to end through the line.

A Harbor-like trial ``<job>/a1`` is built under ``tmp_path`` and every way a split or an
evaluator/label field can hide in one of its JSON files is tried through ``admit_rollout``, through
``core._intake`` and through ``core.run_line`` (which must refuse before any batch, out_root or
queue exists). The scored run is built from the bytes admission returned, never from a second read
of the files, and a rollout that changes after admission fails the batch check.
"""

from __future__ import annotations

import builtins
import copy
import io
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab import rollout_admission
from evallab.line import core
from evallab.line import io as line_io
from evallab.line.io import digest, read_json, read_rows, write_rows
from evallab.line.spec import load_spec
from evallab.rollout_admission import DOCUMENT_DEPTH, AdmissionRefused, admit_rollout

ATIF: dict[str, Any] = {
    "schema_version": "ATIF-v1.4",
    "steps": [
        {"step_id": 1, "source": "user", "message": "hi"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "write",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "write",
                    "arguments": {"path": "x.py", "content": "y=1"},
                }
            ],
        },
    ],
}
RESULT = {"task_name": "t-cert", "trial_name": "a1"}
NOT_DEV = "a split declaration is not dev"
CODE = "def add(a, b):\n    return a + b\n"
DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-y=0\n+y=1\n"
FORBIDDEN_BY_CONTENT = "def add(a, b):\n    return a - b\n"


class _Registry:
    def assigned_test_anywhere(
        self, *, item_id: str | None = None, task_id: str | None = None
    ) -> bool:
        return False


def _traj(**extra: Any) -> dict[str, Any]:
    return {**copy.deepcopy(ATIF), **extra}


def _with_args(arguments: dict[str, Any], message: str = "write") -> dict[str, Any]:
    doc = copy.deepcopy(ATIF)
    doc["steps"][1]["message"] = message
    doc["steps"][1]["tool_calls"][0]["arguments"] = arguments
    return doc


def _nest(depth: int, leaf: Any) -> Any:
    for _ in range(depth):
        leaf = {"n": leaf}
    return leaf


def _write(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")


def _trial(tmp_path: Path, files: dict[str, Any] | None = None) -> Path:
    """``<tmp>/job/a1``: ATIF trajectory, result.json, plus ``files`` (relative path -> content;
    ``agent/trajectory.json`` and ``result.json`` replace the defaults)."""
    trial = tmp_path / "job" / "a1"
    _write(trial / "agent" / "trajectory.json", ATIF)
    _write(trial / "result.json", RESULT)
    for relative, content in (files or {}).items():
        _write(trial / relative, content)
    return Path(os.path.realpath(trial))


def _file_rollout(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "files" / "rollout_atif.json"
    _write(path, document)
    return Path(os.path.realpath(path))


def _admit(rollout: Path, **kwargs: Any) -> Any:
    return admit_rollout(str(rollout), registry=_Registry(), **kwargs)


def _point(fixture_spec: Path, rollout: Path, **row: Any) -> dict[str, Any]:
    """Make the fixture's only run ``a1`` read from ``rollout``; return the loaded spec."""
    source = read_json(fixture_spec)["sources"][0]
    write_rows(
        Path(source["rollouts"]), [{"id": "a1", "task_id": "t-cert", "path": str(rollout), **row}]
    )
    write_rows(Path(source["labels"]), [{"id": "a1", "is_cheat": False}])
    return load_spec(fixture_spec)


def _assert_nothing_written(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    assert not Path(spec["out_root"]).exists()
    assert not Path(spec["judge"]["queue"]).exists()


def _assert_refused_everywhere(fixture_spec: Path, rollout: Path, text: str) -> None:
    """Admission, the line's intake and ``run_line`` all refuse with ``text``; nothing is written."""
    with pytest.raises(AdmissionRefused) as direct:
        _admit(rollout)
    assert re.search(text, direct.value.detail), direct.value.detail
    spec = _point(fixture_spec, rollout)
    with pytest.raises(ValueError, match=f"rollout not admitted: .*{text}"):
        core._intake(spec, fixture_spec.parent)
    with pytest.raises(ValueError, match=f"rollout not admitted: .*{text}"):
        core.run_line(fixture_spec, fixture_spec.parent)
    _assert_nothing_written(fixture_spec)


def _split(relative: str) -> str:
    return rf"{re.escape(relative)}: {NOT_DEV}"


def _label(relative: str, key: str) -> str:
    return rf"{re.escape(relative)}: evaluator/label field {re.escape(repr(key))}"


# ---- A. the matrix over a Harbor-like trial ---------------------------------------------------

_SPLIT_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("meta", {"metadata.json": {"split": "test"}}, "metadata.json"),
    ("verifier", {"verifier/result.json": {"split": "test"}}, "verifier/result.json"),
    ("agent-extra", {"agent/extra.json": {"split": "test"}}, "agent/extra.json"),
    ("sub-dir", {"sub/submission.json": {"split": "test"}}, "sub/submission.json"),
    (
        "step-arguments",
        {"agent/trajectory.json": _with_args({"path": "x.py", "split": "test"})},
        "agent/trajectory.json",
    ),
    (
        "step-top-level",
        {"agent/trajectory.json": _traj(split="test")},
        "agent/trajectory.json",
    ),
    ("case-variant", {"metadata.json": {"SPLIT": "Held"}}, "metadata.json"),
    ("list-value", {"metadata.json": {"split": ["dev"]}}, "metadata.json"),
    ("nested-deep", {"agent/extra.json": _nest(20, {"split": "test"})}, "agent/extra.json"),
]


@pytest.mark.parametrize(
    ("files", "relative"),
    [pytest.param(f, r, id=i) for i, f, r in _SPLIT_CASES],
)
def test_a_split_declaration_in_any_json_file_of_the_trial_is_refused(
    fixture_spec: Path, tmp_path: Path, files: dict[str, Any], relative: str
) -> None:
    _assert_refused_everywhere(fixture_spec, _trial(tmp_path, files), _split(relative))


_LABEL_CASES: list[tuple[str, dict[str, Any], str, str]] = [
    ("meta", {"metadata.json": {"is_cheat": True}}, "metadata.json", "is_cheat"),
    (
        "verifier",
        {"verifier/result.json": {"ground_truth": "cheat"}},
        "verifier/result.json",
        "ground_truth",
    ),
    ("agent-extra", {"agent/extra.json": {"behavior": "x"}}, "agent/extra.json", "behavior"),
    (
        "trajectory-top-level",
        {"agent/trajectory.json": _traj(is_cheat=True)},
        "agent/trajectory.json",
        "is_cheat",
    ),
    (
        "trajectory-nodes",
        {"agent/trajectory.json": _traj(nodes=[{"is_cheat": True}])},
        "agent/trajectory.json",
        "is_cheat",
    ),
    (
        "trajectory-tools-key",
        {"agent/trajectory.json": _traj(tools=[{"is_cheat": True}])},
        "agent/trajectory.json",
        "is_cheat",
    ),
    (
        "tool-parameter-schema-value",
        {
            "agent/trajectory.json": _traj(
                tools=[
                    {
                        "function": {
                            "name": "f",
                            "parameters": {
                                "properties": {"label": {"type": "string", "is_cheat": True}}
                            },
                        }
                    }
                ]
            )
        },
        "agent/trajectory.json",
        "is_cheat",
    ),
    (
        "properties-outside-tools",
        {"agent/trajectory.json": _traj(extra={"properties": {"label": 1}})},
        "agent/trajectory.json",
        "label",
    ),
    (
        "steps-below-the-top-level",
        {"agent/trajectory.json": _traj(extra={"steps": [{"is_cheat": True}]})},
        "agent/trajectory.json",
        "is_cheat",
    ),
    (
        "nested-20-deep",
        {"agent/extra.json": _nest(20, {"is_cheat": True})},
        "agent/extra.json",
        "is_cheat",
    ),
    ("case-variant", {"metadata.json": {"Is_Cheat": True}}, "metadata.json", "Is_Cheat"),
]


@pytest.mark.parametrize(
    ("files", "relative", "key"),
    [pytest.param(f, r, k, id=i) for i, f, r, k in _LABEL_CASES],
)
def test_an_evaluator_label_field_in_any_json_file_of_the_trial_is_refused(
    fixture_spec: Path, tmp_path: Path, files: dict[str, Any], relative: str, key: str
) -> None:
    _assert_refused_everywhere(fixture_spec, _trial(tmp_path, files), _label(relative, key))


def _deep_tool_schema() -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(12):
        schema = {"type": "object", "properties": {"child": schema}}
    return {"function": {"name": "f", "parameters": schema}}


_ALLOWED: list[tuple[str, dict[str, Any]]] = [
    (
        "label-words-in-steps",
        {
            "agent/trajectory.json": _with_args(
                {"path": "x.py", "content": "y=1", "label": "a", "is_cheat": True},
                message="the label is_cheat ground_truth behavior split test",
            )
        },
    ),
    (
        "tool-parameter-named-label",
        {
            "agent/trajectory.json": _traj(
                tools=[
                    {
                        "function": {
                            "name": "f",
                            "parameters": {"properties": {"label": {"type": "string"}}},
                        }
                    }
                ]
            )
        },
    ),
    ("split-dev", {"metadata.json": {"split": "dev"}, "agent/extra.json": {"Split": "DEV"}}),
    ("split-null", {"metadata.json": {"split": None}}),
    ("deep-honest-tool-schema", {"agent/trajectory.json": _traj(tools=[_deep_tool_schema()])}),
]


@pytest.mark.parametrize("files", [pytest.param(f, id=i) for i, f in _ALLOWED])
def test_label_words_in_steps_and_tool_parameter_names_and_a_dev_split_are_admitted(
    fixture_spec: Path, tmp_path: Path, files: dict[str, Any]
) -> None:
    trial = _trial(tmp_path, files)
    admitted = _admit(trial)
    assert admitted.kind == "trial"
    spec = _point(fixture_spec, trial)
    items, rows = core._intake(spec, fixture_spec.parent)
    assert [r["run_id"] for r in rows] == ["a1"]
    assert rows[0]["sha256"] == admitted.sha256
    assert items[0].run is not None


def test_the_admitted_harbor_trial_runs_through_the_line_and_checks(
    fixture_spec: Path, tmp_path: Path
) -> None:
    # Control for the refusals above: with nothing forbidden the same trial is a passing batch.
    trial = _trial(tmp_path, {"metadata.json": {"split": "dev"}})
    _point(fixture_spec, trial)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"] is True


def test_a_json_file_nested_past_the_document_depth_is_refused(
    fixture_spec: Path, tmp_path: Path
) -> None:
    deep = _trial(tmp_path, {"agent/extra.json": _nest(DOCUMENT_DEPTH + 6, {"x": 1})})
    _assert_refused_everywhere(fixture_spec, deep, _split("agent/extra.json"))


def test_a_json_file_nested_just_inside_the_document_depth_is_admitted(tmp_path: Path) -> None:
    trial = _trial(tmp_path, {"agent/extra.json": _nest(DOCUMENT_DEPTH - 2, {"x": 1})})
    assert _admit(trial).kind == "trial"


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        pytest.param('{"a": 1, "a": 2}', r"duplicate JSON key", id="duplicate-key"),
        pytest.param('{"split": "dev", "split": "test"}', r"duplicate JSON key", id="split-twice"),
        pytest.param('{"a": ', r"not JSON", id="truncated"),
        pytest.param("not json at all", r"not JSON", id="garbage"),
    ],
)
def test_an_extra_json_file_that_is_not_strict_json_is_refused(
    fixture_spec: Path, tmp_path: Path, raw: str, text: str
) -> None:
    trial = _trial(tmp_path, {"agent/extra.json": raw})
    _assert_refused_everywhere(fixture_spec, trial, rf"agent/extra\.json.*{text}")


def test_an_oversized_extra_json_file_is_refused_not_read(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = _trial(tmp_path)
    spec = _point(fixture_spec, trial)  # the spec and manifest are read before the limit drops
    limit = max(p.stat().st_size for p in trial.rglob("*") if p.is_file()) + 1
    monkeypatch.setattr(line_io, "MAX_READ_BYTES", limit)
    monkeypatch.setattr(rollout_admission, "MAX_READ_BYTES", limit)
    assert _admit(trial).kind == "trial"  # control: every honest file fits under the limit
    assert core._intake(spec, fixture_spec.parent)[1][0]["run_id"] == "a1"
    _write(trial / "agent" / "extra.json", {"pad": "x" * (limit + 10)})
    text = rf"file is larger than {limit} bytes: .*agent/extra\.json"
    with pytest.raises(AdmissionRefused, match=text):
        _admit(trial)
    with pytest.raises(ValueError, match=f"rollout not admitted: {text}"):
        core._intake(spec, fixture_spec.parent)


_FILE_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("label-top-level", _traj(is_cheat=True), _label("rollout_atif.json", "is_cheat")),
    (
        "label-deep",
        _traj(extra=_nest(20, {"ground_truth": 1})),
        _label("rollout_atif.json", "ground_truth"),
    ),
    ("split-deep", _traj(extra=_nest(20, {"split": "test"})), _split("rollout_atif.json")),
    ("split-top-level", _traj(Split="test"), _split("rollout_atif.json")),
]


@pytest.mark.parametrize(
    ("document", "text"), [pytest.param(d, t, id=i) for i, d, t in _FILE_CASES]
)
def test_a_json_rollout_file_with_a_split_or_label_field_is_refused(
    fixture_spec: Path, tmp_path: Path, document: dict[str, Any], text: str
) -> None:
    _assert_refused_everywhere(fixture_spec, _file_rollout(tmp_path, document), text)


def test_a_clean_json_rollout_file_is_admitted(fixture_spec: Path, tmp_path: Path) -> None:
    path = _file_rollout(tmp_path, _traj(tools=[_deep_tool_schema()]))
    assert _admit(path).kind == "file"
    items, rows = core._intake(_point(fixture_spec, path), fixture_spec.parent)
    assert rows[0]["sha256"] == _admit(path).sha256 and items[0].run is not None


# ---- B. a symlink inside a bundle is never followed -------------------------------------------


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path any python-level open/read helper is asked for."""
    seen: list[str] = []

    def wrap(owner: Any, name: str) -> None:
        real = getattr(owner, name)

        def spy(*args: Any, **kwargs: Any) -> Any:
            target = args[0] if args else kwargs.get("file", kwargs.get("path"))
            if isinstance(target, (str, bytes, os.PathLike)):
                seen.append(os.fsdecode(target))
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)

    wrap(os, "open")
    wrap(builtins, "open")
    wrap(io, "open")
    for name in ("read_bytes", "read_text", "open"):
        wrap(Path, name)
    return seen


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "solution.py").write_text(CODE)
    return Path(os.path.realpath(bundle))


@pytest.mark.parametrize("name", ["final.diff", "solution.py", "linked-folder"])
def test_a_symlink_in_a_bundle_is_refused_and_its_target_is_never_opened(
    fixture_spec: Path, tmp_path: Path, opened: list[str], name: str
) -> None:
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    secret = secret_dir / "secret.txt"
    secret.write_text("the sealed answer")
    bundle = _bundle(tmp_path)
    if name == "linked-folder":
        link = bundle / "sub"
        link.symlink_to(secret_dir, target_is_directory=True)
    else:
        link = bundle / name
        link.unlink(missing_ok=True)
        link.symlink_to(secret)
    opened.clear()  # only what admission itself opens counts, not this test's own setup
    with pytest.raises(AdmissionRefused, match="symlink inside the trial"):
        _admit(bundle, require_trajectory=False)
    spec = _point(fixture_spec, bundle)
    with pytest.raises(ValueError, match="rollout not admitted: symlink inside the trial"):
        core._intake(spec, fixture_spec.parent)
    with pytest.raises(ValueError, match="rollout not admitted: symlink inside the trial"):
        core.run_line(fixture_spec, fixture_spec.parent)
    _assert_nothing_written(fixture_spec)
    resolved = {os.path.realpath(p) for p in opened}
    assert str(secret) not in opened and str(secret) not in resolved
    assert str(link) not in opened  # the link itself was not opened either (open follows it)
    assert not any(p.startswith(str(secret_dir) + os.sep) for p in opened)


def test_the_open_spy_does_see_the_files_an_admission_reads(
    tmp_path: Path, opened: list[str]
) -> None:
    # Control for the test above: a regular bundle file is recorded, so a miss there is real.
    bundle = _bundle(tmp_path)
    opened.clear()
    _admit(bundle, require_trajectory=False)
    assert str(bundle / "solution.py") in opened


# ---- C. the scored run is built from admitted bytes only --------------------------------------


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a rollout file was opened again after admission")


def _no_second_read(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_read_bundle_files", "load_atif_file", "_load_single_path_rollout"):
        monkeypatch.setattr(hd, name, _boom)


def test_a_bundle_is_built_from_the_bytes_admission_returned(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "final.diff").write_text(DIFF)
    legacy = hd._load_single_path_rollout(bundle)
    spec = _point(fixture_spec, bundle)
    _no_second_read(monkeypatch)
    items, rows = core._intake(spec, fixture_spec.parent)
    assert rows[0]["sha256"] == digest(bundle)
    assert rows[0]["rollout_path"] == str(bundle)
    item = items[0]
    assert ("solution.py", CODE) in item.files
    assert item.diff_text == DIFF
    assert item.run is None
    assert item.files == legacy.files and item.diff_text == legacy.diff_text


def test_a_trial_is_built_from_the_documents_admission_returned(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = _trial(tmp_path)
    legacy = hd._load_single_path_rollout(trial)
    spec = _point(fixture_spec, trial)
    _no_second_read(monkeypatch)
    items, rows = core._intake(spec, fixture_spec.parent)
    assert rows[0]["sha256"] == digest(trial)
    item = items[0]
    assert item.run is not None and legacy.run is not None
    assert item.run.doc == legacy.run.doc
    assert [s["step_id"] for s in item.run.doc["steps"]] == [1, 2]
    assert item.files == legacy.files and ("x.py", "y=1") in item.files


def _mutate_after_admission(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> Callable[..., Any]:
    """After the first admission returns, overwrite the rollout file on disk; return the real one."""
    real = rollout_admission.admit_rollout
    calls: list[str] = []

    def mutating(listed: str, **kwargs: Any) -> Any:
        result = real(listed, **kwargs)
        if not calls:
            calls.append(listed)
            (bundle / "solution.py").write_text(FORBIDDEN_BY_CONTENT)
        return result

    monkeypatch.setattr(rollout_admission, "admit_rollout", mutating)
    return real


def test_a_file_rewritten_right_after_admission_does_not_change_what_is_scored(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    original = digest(bundle)
    spec = _point(fixture_spec, bundle)
    _mutate_after_admission(monkeypatch, bundle)
    items, rows = core._intake(spec, fixture_spec.parent)
    assert (bundle / "solution.py").read_text() == FORBIDDEN_BY_CONTENT  # the swap happened
    assert items[0].files == (("solution.py", CODE),)
    assert rows[0]["sha256"] == original != digest(bundle)


def test_a_batch_run_while_the_rollout_changes_records_the_admitted_hash_and_fails_its_check(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    original = digest(bundle)
    _point(fixture_spec, bundle)
    real = _mutate_after_admission(monkeypatch, bundle)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_rows(batch / "runs.jsonl")[0]["sha256"] == original
    assert read_json(batch / "check.json")["passed"] is False
    monkeypatch.setattr(rollout_admission, "admit_rollout", real)
    failed = {
        c["name"]: c["details"]
        for c in core.check_batch(batch, fixture_spec.parent)["checks"]
        if c["status"] == "FAIL"
    }
    assert "intake source identity changed" in failed["dev_only"]
    assert "hash mismatch" in failed["provenance"]


def test_a_rollout_rewritten_after_a_clean_run_fails_the_batch_check(
    fixture_spec: Path, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    _point(fixture_spec, bundle)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    (bundle / "solution.py").write_text(FORBIDDEN_BY_CONTENT)
    result = core.check_batch(batch, fixture_spec.parent)
    failed = {c["name"]: c["details"] for c in result["checks"] if c["status"] == "FAIL"}
    assert result["passed"] is False
    assert "intake source identity changed" in failed["dev_only"]
    assert "hash mismatch" in failed["provenance"]


# ---- D. a label file cannot reach a decision stage through a bundle ---------------------------


def _labels(fixture_spec: Path) -> Path:
    return Path(read_json(fixture_spec)["sources"][0]["labels"])


def test_a_bundle_file_that_is_a_hard_link_of_the_labels_file_is_refused_before_any_batch(
    fixture_spec: Path, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    _point(fixture_spec, bundle)
    (bundle / "solution.py").unlink()
    os.link(_labels(fixture_spec), bundle / "solution.py")
    with pytest.raises(ValueError, match="a label file is also an input to a decision stage"):
        core.run_line(fixture_spec, fixture_spec.parent)
    _assert_nothing_written(fixture_spec)


def test_a_bundle_file_swapped_for_the_labels_file_after_a_clean_run_fails_the_check(
    fixture_spec: Path, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    _point(fixture_spec, bundle)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    (bundle / "solution.py").unlink()
    os.link(_labels(fixture_spec), bundle / "solution.py")
    result = core.check_batch(batch, fixture_spec.parent)
    failed = {c["name"]: c["details"] for c in result["checks"] if c["status"] == "FAIL"}
    assert result["passed"] is False
    assert "labels entered a decision stage" in failed["label_isolation"]
    assert "hash mismatch" in failed["provenance"]


# ---- E. a split nested under result / run_meta ------------------------------------------------

_NESTED = [
    pytest.param({"result": {"split": "test"}}, id="result"),
    pytest.param({"run_meta": {"Split": "test"}}, id="run-meta"),
]


@pytest.mark.parametrize("nested", _NESTED)
@pytest.mark.parametrize(
    ("where", "make"),
    [
        pytest.param(
            "agent/trajectory.json",
            lambda n: {"agent/trajectory.json": _traj(**n)},
            id="trajectory",
        ),
        pytest.param(
            "result.json", lambda n: {"result.json": {**RESULT, **n}}, id="result-sidecar"
        ),
        pytest.param(
            "config.json", lambda n: {"config.json": {"trial_name": "a1", **n}}, id="config-sidecar"
        ),
    ],
)
def test_a_split_under_result_or_run_meta_in_a_trial_document_is_refused(
    fixture_spec: Path,
    tmp_path: Path,
    nested: dict[str, Any],
    where: str,
    make: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    _assert_refused_everywhere(fixture_spec, _trial(tmp_path, make(nested)), _split(where))


@pytest.mark.parametrize("nested", _NESTED)
def test_a_split_under_result_or_run_meta_in_a_manifest_row_is_refused(
    fixture_spec: Path, tmp_path: Path, nested: dict[str, Any]
) -> None:
    trial = _trial(tmp_path)
    with pytest.raises(AdmissionRefused, match=NOT_DEV):
        _admit(trial, row={"id": "a1", "path": str(trial), **nested})
    _point(fixture_spec, trial, **nested)
    with pytest.raises(ValueError, match="non-DEV split"):
        core.run_line(fixture_spec, fixture_spec.parent)
    _assert_nothing_written(fixture_spec)
