"""The last admission fixes: manifest rows, label wrappers, ancestor sidecars, task status.

Every fixture is synthetic and lives under ``tmp_path``. A refusal of the line happens before any
batch directory, out_root or queue exists, and it names its reason.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab import rollout_admission
from evallab.line import core
from evallab.line.io import read_json, read_rows, write_json, write_rows
from evallab.line.spec import load_spec
from evallab.rollout_admission import (
    AdmissionRefused,
    _refuse_document,
    admit_rollout,
    label_key,
    sealed_inodes,
)

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
RESULT = {"task_name": "plain-task", "trial_name": "a1"}
CORRUPT = {"steps": "not-a-list"}
NO_PATH = "rollout manifest row has no path: the assembly line admits only rollouts on disk"
NOT_AN_OBJECT = "rollout manifest row is not an object"
NOT_DEV = "a split declaration is not dev"


class _Registry:
    def assigned_test_anywhere(
        self, *, item_id: str | None = None, task_id: str | None = None
    ) -> bool:
        return False


# ---- helpers -----------------------------------------------------------------------------------


def _write(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")


def _trial(tmp_path: Path, files: dict[str, Any] | None = None, *, name: str = "a1") -> Path:
    """``<tmp>/job/<name>``: ATIF trajectory and result.json, plus/replacing ``files``."""
    trial = tmp_path / "job" / name
    _write(trial / "agent" / "trajectory.json", ATIF)
    _write(trial / "result.json", RESULT)
    for relative, content in (files or {}).items():
        _write(trial / relative, content)
    return Path(os.path.realpath(trial))


def _admit(rollout: Path, **kwargs: Any) -> Any:
    kwargs.setdefault("registry", _Registry())
    return admit_rollout(str(rollout), **kwargs)


def _spec_source(fixture_spec: Path) -> dict[str, Any]:
    return read_json(fixture_spec)["sources"][0]


def _manifest_rows(fixture_spec: Path, rows: list[Any], *, suffix: str = ".jsonl") -> Path:
    """Declare a manifest of ``rows`` (``.jsonl``: one row per line; ``.json``: ``{"rows": ...}``)
    as the fixture's rollouts; labels cover every dict row that has an id."""
    spec = read_json(fixture_spec)
    manifest = fixture_spec.parent / f"manifest{suffix}"
    if suffix == ".jsonl":
        write_rows(manifest, rows)
    else:
        write_json(manifest, {"rows": rows})
    spec["sources"][0]["rollouts"] = str(manifest)
    labels = [
        {"id": row["id"], "is_cheat": False}
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]
    write_rows(Path(spec["sources"][0]["labels"]), labels or [{"id": "none", "is_cheat": False}])
    write_json(fixture_spec, spec)
    return manifest


def _point(fixture_spec: Path, rollout: Path, **row: Any) -> dict[str, Any]:
    """Make the fixture's only run ``a1`` read from ``rollout``; return the loaded spec."""
    source = _spec_source(fixture_spec)
    write_rows(
        Path(source["rollouts"]),
        [{"id": "a1", "task_id": "plain-task", "path": str(rollout), **row}],
    )
    write_rows(Path(source["labels"]), [{"id": "a1", "is_cheat": False}])
    return load_spec(fixture_spec)


def _assert_nothing_written(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    assert not Path(spec["out_root"]).exists()
    assert not (fixture_spec.parent / "batches" / "synthetic").exists()
    assert not Path(spec["judge"]["queue"]).exists()


def _assert_line_refuses(fixture_spec: Path, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        core.run_line(fixture_spec, fixture_spec.parent)
    _assert_nothing_written(fixture_spec)


def _sealed(root: Path, body: Any = None) -> tuple[Path, frozenset[tuple[int, int]]]:
    """A sealed file under ``<root>/research/answer-keys`` and the sealed inodes."""
    secret = root / "research" / "answer-keys" / "secret.json"
    _write(secret, {"job_name": "j"} if body is None else body)
    return secret, sealed_inodes(root / "research" / "answer-keys")


# ---- (a) manifest rows: the line admits only rollouts on disk ------------------------------------

INLINE = {"id": "inline", "task_id": "plain-task", "steps": ATIF["steps"]}
BY_ID = {"id": "by-id", "task_id": "plain-task"}


@pytest.mark.parametrize("row", [INLINE, BY_ID], ids=["inline-steps", "run-by-id"])
def test_a_pathless_jsonl_manifest_row_is_refused_by_the_line_and_loads_outside_it(
    fixture_spec: Path, row: dict[str, Any]
) -> None:
    clean = _spec_source(fixture_spec)["rollouts"]
    manifest = _manifest_rows(fixture_spec, [read_rows(Path(clean))[0], row])
    _assert_line_refuses(fixture_spec, NO_PATH)
    with pytest.raises(ValueError, match=NO_PATH):
        hd.load_rollouts([manifest], repo_root=fixture_spec.parent, admitted_only=True)
    # hack-drop's own use keeps loading an inline rollout or a run named by id
    legacy = fixture_spec.parent / "legacy.jsonl"
    write_rows(legacy, [row])
    items = hd.load_rollouts([legacy], repo_root=fixture_spec.parent)
    assert [item.item_id for item in items] == [row["id"]]


@pytest.mark.parametrize("bad", [123, "text", None, [1]], ids=["int", "str", "null", "list"])
def test_a_json_manifest_row_that_is_not_an_object_is_refused_everywhere(
    fixture_spec: Path, bad: Any
) -> None:
    manifest = _manifest_rows(fixture_spec, [bad], suffix=".json")
    for admitted_only in (False, True):
        with pytest.raises(ValueError, match=NOT_AN_OBJECT):
            hd.load_rollouts([manifest], repo_root=fixture_spec.parent, admitted_only=admitted_only)
    _assert_line_refuses(fixture_spec, NOT_AN_OBJECT)


def test_a_pathless_object_row_in_a_json_manifest_is_refused_by_the_line_and_skipped_outside(
    fixture_spec: Path,
) -> None:
    clean = fixture_spec.parent / "clean.py"
    on_disk = {"id": "clean-run", "task_id": "plain-task", "path": str(clean)}
    manifest = _manifest_rows(fixture_spec, [on_disk, INLINE], suffix=".json")
    _assert_line_refuses(fixture_spec, NO_PATH)
    with pytest.raises(ValueError, match=NO_PATH):
        hd.load_rollouts([manifest], repo_root=fixture_spec.parent, admitted_only=True)
    # legacy: a pathless object row of a .json manifest is skipped, the row on disk loads
    items = hd.load_rollouts([manifest], repo_root=fixture_spec.parent)
    assert [item.item_id for item in items] == ["clean-run"]


def test_a_rollouts_input_that_does_not_exist_is_not_looked_up_by_id(
    fixture_spec: Path,
) -> None:
    missing = fixture_spec.parent / "no-such-run.jsonl"
    with pytest.raises(FileNotFoundError, match="input path not found"):
        hd.load_rollouts([missing], repo_root=fixture_spec.parent, admitted_only=True)


def _assigned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The registry hack-drop builds from a repo root's ``assignments.csv``, installed for the
    line (the fixture has none)."""
    csv_path = tmp_path / "assignments.csv"
    csv_path.write_text(
        "item_id,task,split\nsealed-run,,test\n,sealed-task,test\nfine-run,fine-task,dev\n",
        encoding="utf-8",
    )
    registry = hd._SyntheticAssignmentsRegistry(csv_path)
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: registry)
    return registry


@pytest.mark.parametrize(
    ("row", "named"),
    [
        ({"id": "sealed-run", "task_id": "plain-task"}, "sealed-run"),
        (
            {"id": "fine-run", "task_id": "plain-task", "metadata": {"task_id": "sealed-task"}},
            "sealed-task",
        ),
        (
            {
                "id": "fine-run",
                "task_id": "plain-task",
                "extra": {"deep": [{"task": "sealed-task"}]},
            },
            "sealed-task",
        ),
    ],
    ids=["row-id", "nested-task-id", "deeply-nested-task"],
)
def test_a_manifest_row_identity_assigned_test_is_refused_before_any_output(
    fixture_spec: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, Any],
    named: str,
) -> None:
    registry = _assigned(tmp_path, monkeypatch)
    trial = _trial(tmp_path)
    row = {**row, "path": str(trial)}
    _manifest_rows(fixture_spec, [row])
    _assert_line_refuses(fixture_spec, f"rollout not admitted: assigned TEST in registry: {named}")
    # the row and its id are what reach admission: the trial alone is admitted
    with pytest.raises(AdmissionRefused, match=f"assigned TEST in registry: {named}"):
        _admit(trial, registry=registry, row=row, run_id=row["id"])
    assert _admit(trial, registry=registry).kind == "trial"


def test_the_same_layout_with_dev_identities_runs(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assigned(tmp_path, monkeypatch)
    row = {"id": "fine-run", "task_id": "plain-task", "path": str(_trial(tmp_path))}
    _manifest_rows(fixture_spec, [row])
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert [r["run_id"] for r in read_rows(batch / "runs.jsonl")] == ["fine-run"]


def test_a_corrupt_trajectory_keeps_its_parse_error_and_goes_to_review_not_keep(
    fixture_spec: Path, tmp_path: Path
) -> None:
    trial = _trial(tmp_path, {"agent/trajectory.json": CORRUPT})
    spec = _point(fixture_spec, trial)
    (item,) = core._intake(spec, fixture_spec.parent)[0]
    assert item.run is None
    assert item.metadata["parse_error"]
    assert item.metadata["run_meta"]["trial_name"] == "a1"  # what loading found travels too
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    (decision,) = read_rows(batch / "decisions.jsonl")
    assert decision["decision"] == "review"
    assert decision["stage_decisions"]["l0"]["decision"] == "review"
    assert decision["stage_decisions"]["l0"]["reasons"] == ["L0_PARSE_ERROR"]
    assert read_rows(batch / "export" / "kept.jsonl") == []
    assert [r["run_id"] for r in read_rows(batch / "export" / "review.jsonl")] == ["a1"]


def test_an_honest_trajectory_has_no_parse_error(fixture_spec: Path, tmp_path: Path) -> None:
    spec = _point(fixture_spec, _trial(tmp_path))
    (item,) = core._intake(spec, fixture_spec.parent)[0]
    assert item.run is not None
    assert "parse_error" not in item.metadata


@pytest.mark.parametrize(
    ("first", "second"),
    [("agent/trajectory.json", "trajectory.json"), ("trajectory.json", "agent/trajectory.json")],
    ids=["corrupt-agent-first", "corrupt-root-second"],
)
def test_a_later_trajectory_that_parses_clears_the_earlier_parse_error(
    tmp_path: Path, first: str, second: str
) -> None:
    # ``agent/trajectory.json`` is the first candidate, ``trajectory.json`` the second
    trial = _trial(tmp_path, {first: CORRUPT, second: ATIF})
    admitted = _admit(trial)
    assert [name for name, _ in admitted.trajectories] == [
        "agent/trajectory.json",
        "trajectory.json",
    ]
    for item in (hd.rollout_from_admitted(trial, admitted), hd._load_single_path_rollout(trial)):
        assert item.run is not None
        assert "parse_error" not in item.metadata


def test_when_every_trajectory_candidate_is_corrupt_the_parse_error_stays(tmp_path: Path) -> None:
    trial = _trial(tmp_path, {"agent/trajectory.json": CORRUPT, "trajectory.json": CORRUPT})
    admitted = _admit(trial, require_trajectory=False)
    first = hd.rollout_from_admitted(trial, admitted)
    second = hd._load_single_path_rollout(trial)
    for item in (first, second):
        assert item.run is None
        assert item.metadata["parse_error"]
    assert first.metadata["parse_error"] == second.metadata["parse_error"]


def test_a_single_json_rollout_that_does_not_parse_keeps_its_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "files" / "rollout_atif.json"
    _write(path, CORRUPT)
    path = Path(os.path.realpath(path))
    admitted = _admit(path, require_trajectory=False)
    first = hd.rollout_from_admitted(path, admitted)
    second = hd._load_single_path_rollout(path)
    assert first.metadata["parse_error"] == second.metadata["parse_error"]
    assert first.run is None and second.run is None


# ---- (b) labels ----------------------------------------------------------------------------------

LABEL_ROWS = [{"id": "r1", "is_cheat": True}, {"id": "r2", "is_cheat": False}]
LABEL_WRAPPERS = {
    "Split-capitalised": {"Split": "test", "rows": LABEL_ROWS},
    "METADATA-split": {"METADATA": {"split": "test"}, "rows": LABEL_ROWS},
    "metadata-list": {"metadata": [{"split": "test"}], "rows": LABEL_ROWS},
    "result-split": {"result": {"split": "test"}, "rows": LABEL_ROWS},
    "nested-info": {"info": {"a": {"b": [{"SPLIT": "held"}]}}, "rows": LABEL_ROWS},
    "split-as-list": {"split": ["dev"], "rows": LABEL_ROWS},
    "split-as-number": {"split": 1, "rows": LABEL_ROWS},
    "items-wrapper": {"Split": "test", "items": LABEL_ROWS},
    "flat-map": {"Split": "clean", "r1": False},
}


def _labels_root(tmp_path: Path, assignments: str = "item_id,task,split\n") -> Path:
    """A repo root whose only registry source is a synthetic ``assignments.csv``."""
    (tmp_path / "assignments.csv").write_text(assignments, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("name", LABEL_WRAPPERS)
def test_a_label_wrapper_declaring_a_non_dev_split_anywhere_is_refused(
    tmp_path: Path, name: str
) -> None:
    root = _labels_root(tmp_path)
    labels = tmp_path / "labels.json"
    write_json(labels, LABEL_WRAPPERS[name])
    with pytest.raises(hd.RefusedRun, match="label file declares a non-DEV split"):
        hd.load_labels(labels, repo_root=root)


@pytest.mark.parametrize(
    "wrapper",
    [
        {"split": "dev", "rows": LABEL_ROWS},
        {"Split": "DEV", "metadata": {"split": "dev"}, "rows": LABEL_ROWS},
        {"split": None, "rows": LABEL_ROWS},
        {"info": {"note": "nothing here"}, "labels": LABEL_ROWS},
    ],
    ids=["dev", "case-folded-dev", "null", "no-split"],
)
def test_a_clean_label_wrapper_is_accepted(tmp_path: Path, wrapper: dict[str, Any]) -> None:
    root = _labels_root(tmp_path)
    labels = tmp_path / "labels.json"
    write_json(labels, wrapper)
    assert hd.load_labels(labels, repo_root=root) == {"r1": True, "r2": False}


ASSIGNED = "item_id,task,split\nsealed-item,,test\n,sealed-task,test\nfine-item,fine-task,dev\n"


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            {"item_id": "harmless", "id": "sealed-item", "is_cheat": True},
            "label row item is assigned TEST in corpus registry: sealed-item",
        ),
        (
            {
                "id": "harmless",
                "item_id": "harmless-too",
                "run_id": "sealed-item",
                "is_cheat": True,
            },
            "label row item is assigned TEST in corpus registry: sealed-item",
        ),
        (
            {"run_id": "harmless", "item_id": "sealed-item", "is_cheat": True},
            "label row item is assigned TEST in corpus registry: sealed-item",
        ),
        (
            {"id": "harmless", "task_id": "harmless-task", "task": "sealed-task", "is_cheat": True},
            "label row task is assigned TEST in corpus registry: sealed-task",
        ),
        (
            {"id": "harmless", "task": "harmless-task", "task_id": "sealed-task", "is_cheat": True},
            "label row task is assigned TEST in corpus registry: sealed-task",
        ),
        (
            {"item_id": "harmless", "id": "sealed-item__trial-1", "is_cheat": True},
            "label row item stem is assigned TEST in corpus registry: sealed-item",
        ),
    ],
    ids=["id", "run_id", "item_id", "task", "task_id", "stem"],
)
@pytest.mark.parametrize("form", ["jsonl", "list", "wrapper"])
def test_a_label_row_with_any_identity_assigned_test_is_refused(
    tmp_path: Path, row: dict[str, Any], message: str, form: str
) -> None:
    root = _labels_root(tmp_path, ASSIGNED)
    labels = tmp_path / "labels.json"
    if form == "jsonl":
        labels.write_text(json.dumps(row) + "\n", encoding="utf-8")
    elif form == "list":
        write_json(labels, [{"id": "ok", "is_cheat": False}, row])
    else:
        write_json(labels, {"split": "dev", "rows": [row]})
    with pytest.raises(hd.RefusedRun, match=message):
        hd.load_labels(labels, repo_root=root)


def test_label_rows_with_only_dev_identities_are_accepted_under_the_same_registry(
    tmp_path: Path,
) -> None:
    root = _labels_root(tmp_path, ASSIGNED)
    labels = tmp_path / "labels.json"
    write_json(
        labels,
        [
            {"item_id": "harmless", "id": "fine-item", "task": "fine-task", "is_cheat": True},
            {"run_id": "other", "item_id": "other-item", "task_id": "plain", "is_cheat": False},
        ],
    )
    assert hd.load_labels(labels, repo_root=root) == {"fine-item": True, "other": False}


def test_a_flat_label_map_naming_a_test_run_is_refused(tmp_path: Path) -> None:
    root = _labels_root(tmp_path, ASSIGNED)
    labels = tmp_path / "labels.json"
    write_json(labels, {"fine-item": False, "sealed-item": True})
    with pytest.raises(hd.RefusedRun, match="label row item is assigned TEST"):
        hd.load_labels(labels, repo_root=root)


# ---- (c) ancestor sidecars -----------------------------------------------------------------------

JOB = {"job_name": "j", "n_total_trials": 1}


def _job_sidecar(tmp_path: Path, name: str, body: Any) -> Path:
    _trial(tmp_path)
    path = tmp_path / "job" / name
    _write(path, body)
    return path


@pytest.mark.parametrize("key", ["is_cheat", "ground_truth", "label", "IS_CHEAT"])
@pytest.mark.parametrize("sidecar", rollout_admission.SIDECARS)
def test_an_ancestor_sidecar_with_an_evaluator_field_is_refused_by_admission_and_the_line(
    fixture_spec: Path, tmp_path: Path, sidecar: str, key: str
) -> None:
    _job_sidecar(tmp_path, sidecar, {**JOB, key: True})
    trial = _trial(tmp_path)
    with pytest.raises(AdmissionRefused, match=f"{sidecar}: evaluator/label field '{key}'"):
        _admit(trial)
    _point(fixture_spec, trial)
    _assert_line_refuses(
        fixture_spec, f"rollout not admitted: .*{sidecar}: evaluator/label field '{key}'"
    )


@pytest.mark.parametrize("sidecar", rollout_admission.SIDECARS)
def test_an_ancestor_sidecar_declaring_a_test_split_is_refused(
    fixture_spec: Path, tmp_path: Path, sidecar: str
) -> None:
    _job_sidecar(tmp_path, sidecar, {**JOB, "Split": "test"})
    trial = _trial(tmp_path)
    with pytest.raises(AdmissionRefused, match=f"{sidecar}: {NOT_DEV}"):
        _admit(trial)
    _point(fixture_spec, trial)
    _assert_line_refuses(fixture_spec, f"rollout not admitted: .*{sidecar}: {NOT_DEV}")


def test_a_deeply_nested_ancestor_sidecar_label_is_refused(tmp_path: Path) -> None:
    _job_sidecar(tmp_path, "config.json", {**JOB, "stats": {"per_task": [{"ground_truth": 1}]}})
    with pytest.raises(AdmissionRefused, match="config.json: evaluator/label field 'ground_truth'"):
        _admit(_trial(tmp_path))


@pytest.mark.parametrize("sidecar", rollout_admission.SIDECARS)
def test_an_ancestor_sidecar_that_is_a_hard_link_to_sealed_material_is_refused(
    fixture_spec: Path, tmp_path: Path, sidecar: str
) -> None:
    secret, sealed = _sealed(tmp_path)  # honest job content: only the link can be the reason
    trial = _trial(tmp_path)
    os.link(secret, trial.parent / sidecar)
    with pytest.raises(AdmissionRefused, match=f"hard link to sealed material: .*{sidecar}"):
        _admit(trial, sealed=sealed)
    _admit(trial)  # control: without the sealed set the same layout is admitted
    _point(fixture_spec, trial)
    _assert_line_refuses(
        fixture_spec, f"rollout not admitted: hard link to sealed material: .*{sidecar}"
    )
    assert secret.read_text() == json.dumps({"job_name": "j"})


@pytest.mark.parametrize("sidecar", rollout_admission.SIDECARS)
def test_an_ancestor_sidecar_that_is_a_symlink_is_refused(
    fixture_spec: Path, tmp_path: Path, sidecar: str
) -> None:
    target = tmp_path / "elsewhere" / "job-file.json"
    _write(target, JOB)  # a harmless target: only the link can be the reason
    trial = _trial(tmp_path)
    (trial.parent / sidecar).symlink_to(target)
    with pytest.raises(
        AdmissionRefused, match=f"ancestor sidecar is not a regular file: .*{sidecar}"
    ):
        _admit(trial)
    _point(fixture_spec, trial)
    _assert_line_refuses(
        fixture_spec, f"rollout not admitted: ancestor sidecar is not a regular file: .*{sidecar}"
    )


def test_an_ancestor_sidecar_that_is_not_json_is_refused_as_unreadable(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    _write(trial.parent / "provenance.json", "{not json")
    with pytest.raises(AdmissionRefused, match="not JSON") as raised:
        _admit(trial)
    assert raised.value.unreadable


def test_the_inodes_of_honest_ancestor_sidecars_are_recorded(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    before = _admit(trial).inodes
    result, config = trial.parent / "result.json", trial.parent / "config.json"
    _write(result, {"n_total_trials": 1, "stats": {}})
    _write(config, {"job_name": "j", "tasks": [{"name": "plain-task"}]})
    inodes = _admit(trial).inodes
    expected = {(os.lstat(p).st_dev, os.lstat(p).st_ino) for p in (result, config)}
    assert expected <= inodes
    assert not expected & before
    assert inodes - expected == before  # nothing else was added


# -- label_key / _refuse_document -----------------------------------------------------------------


def test_the_top_level_steps_are_exempt_only_for_a_trajectory_file() -> None:
    document = {"steps": [{"arguments": {"is_cheat": True}}]}
    assert label_key(document) is None
    assert label_key(document, trajectory=True) is None
    assert label_key(document, trajectory=False) == "is_cheat"
    _refuse_document("agent/trajectory.json", document, trajectory=True)
    with pytest.raises(AdmissionRefused, match="metadata.json: evaluator/label field 'is_cheat'"):
        _refuse_document("metadata.json", document, trajectory=False)


def test_steps_that_are_not_a_top_level_list_are_always_searched() -> None:
    assert label_key({"steps": {"is_cheat": True}}) == "is_cheat"
    assert label_key({"trajs": {"steps": [{"label": 1}]}}) == "label"
    assert label_key({"steps": [], "nodes": [{"ground_truth": 1}]}) == "ground_truth"


def test_a_tools_mapping_is_searched_whole() -> None:
    assert label_key({"tools": {"properties": {"is_cheat": True}}}) == "is_cheat"
    assert label_key({"tools": {"properties": {"label": {"type": "string"}}}}) == "label"


def test_a_real_tool_schema_may_name_a_parameter_label() -> None:
    schema = {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "tag",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "is_cheat": {"type": "boolean"},
                            "nested": {"type": "object", "properties": {"ground_truth": {}}},
                        },
                    },
                },
            }
        ]
    }
    assert label_key(schema) is None
    assert label_key(schema, trajectory=False) is None


@pytest.mark.parametrize(
    ("document", "key"),
    [
        ({"tools": [{"properties": {"is_cheat": True}}]}, "is_cheat"),
        ({"tools": [{"properties": {"label": "x"}}]}, "label"),
        ({"tools": [{"properties": {"ok": {"type": "string"}}, "label": {"x": 1}}]}, "label"),
        ({"tools": [{"function": {"parameters": {"properties": {"p": {"label": 1}}}}}]}, "label"),
        ({"tools": [{"is_cheat": {"type": "boolean"}}]}, "is_cheat"),
    ],
    ids=[
        "scalar-value",
        "string-value",
        "tool-entry-key",
        "label-inside-parameter-schema",
        "entry",
    ],
)
def test_a_tool_entry_naming_a_label_other_than_a_parameter_name_is_refused(
    document: dict[str, Any], key: str
) -> None:
    assert label_key(document) == key
    with pytest.raises(AdmissionRefused, match=f"evaluator/label field '{key}'"):
        _refuse_document("agent/trajectory.json", document, trajectory=True)


def test_an_honest_trajectory_with_label_words_in_its_steps_is_admitted(tmp_path: Path) -> None:
    trajectory = copy.deepcopy(ATIF)
    trajectory["steps"][1]["tool_calls"][0]["arguments"] = {"label": "x", "is_cheat": "no"}
    trial = _trial(tmp_path, {"agent/trajectory.json": trajectory})
    assert _admit(trial).kind == "trial"
    single = tmp_path / "files" / "rollout_atif.json"
    _write(single, trajectory)
    assert _admit(Path(os.path.realpath(single))).kind == "file"


@pytest.mark.parametrize(
    ("relative", "content", "text"),
    [
        (
            "verifier/result.json",
            {"is_cheat": True},
            "verifier/result.json: evaluator/label field 'is_cheat'",
        ),
        (
            "metadata.json",
            {"ground_truth": "cheat"},
            "metadata.json: evaluator/label field 'ground_truth'",
        ),
        (
            "metadata.json",
            {"steps": [{"arguments": {"is_cheat": True}}]},
            "metadata.json: evaluator/label field 'is_cheat'",
        ),
        (
            "agent/notes.json",
            {"steps": [{"label": "x"}]},
            "agent/notes.json: evaluator/label field 'label'",
        ),
        (
            "result.json",
            {**RESULT, "steps": [{"is_cheat": True}]},
            "result.json: evaluator/label field 'is_cheat'",
        ),
        (
            "verifier/grade.json",
            {"nested": {"Label": 1}},
            "verifier/grade.json: evaluator/label field 'Label'",
        ),
    ],
    ids=[
        "verifier-result",
        "metadata",
        "metadata-steps",
        "agent-notes-steps",
        "result-steps",
        "grade",
    ],
)
def test_a_trial_json_file_other_than_the_trajectory_with_a_label_key_is_refused_end_to_end(
    fixture_spec: Path, tmp_path: Path, relative: str, content: Any, text: str
) -> None:
    trial = _trial(tmp_path, {relative: content})
    with pytest.raises(AdmissionRefused, match=text):
        _admit(trial)
    _point(fixture_spec, trial)
    _assert_line_refuses(fixture_spec, f"rollout not admitted: {text}")


# -- the read itself ------------------------------------------------------------------------------


def test_a_file_swapped_for_a_symlink_after_the_walk_is_refused_not_an_os_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "walked.json"
    other = tmp_path / "other.json"
    _write(path, {"a": 1})
    _write(other, {"a": 2})
    seen = os.lstat(path)
    assert rollout_admission._read_whole(path, seen) == b'{"a": 1}'  # control
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(AdmissionRefused, match="file changed while it was being admitted"):
        rollout_admission._read_whole(path, seen)


def test_a_file_removed_or_replaced_by_another_file_after_the_walk_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "walked.json"
    _write(path, {"a": 1})
    seen = os.lstat(path)
    replacement = tmp_path / "second.json"
    _write(replacement, {"a": 1})  # same bytes, another inode, created while the first exists
    os.replace(replacement, path)
    assert os.lstat(path).st_ino != seen.st_ino
    with pytest.raises(AdmissionRefused, match="file changed while it was being admitted"):
        rollout_admission._read_whole(path, seen)
    path.unlink()
    with pytest.raises(AdmissionRefused, match="file changed while it was being admitted"):
        rollout_admission._read_whole(path, seen)  # vanished


def test_a_folder_swapped_for_a_symlink_during_a_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk saw a regular file; by the time it is opened it is a link."""
    trial = _trial(tmp_path)
    outside = tmp_path / "outside.json"
    _write(outside, {"task_name": "plain-task", "trial_name": "a1"})
    real_open = rollout_admission._open
    swapped: list[Path] = []

    def swap_then_open(path: Path, expected: os.stat_result) -> Any:
        if path.name == "result.json" and not swapped:
            swapped.append(path)
            path.unlink()
            path.symlink_to(outside)
        return real_open(path, expected)

    monkeypatch.setattr(rollout_admission, "_open", swap_then_open)
    with pytest.raises(AdmissionRefused, match="file changed while it was being admitted"):
        _admit(trial)
    assert swapped


def test_a_file_that_grows_past_the_bound_after_the_walk_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout_admission, "MAX_READ_BYTES", 100)
    path = tmp_path / "growing.json"
    path.write_text('{"a": "' + "x" * 20 + '"}')
    seen = os.lstat(path)
    assert seen.st_size <= 100
    assert rollout_admission._read_whole(path, seen) == path.read_bytes()  # control
    with path.open("a") as handle:  # same inode, grown after the stat
        handle.write(" " * 300)
    assert os.lstat(path).st_ino == seen.st_ino
    with pytest.raises(AdmissionRefused, match="file is larger than 100 bytes"):
        rollout_admission._read_whole(path, seen)


def test_a_file_already_larger_than_the_bound_is_refused_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout_admission, "MAX_READ_BYTES", 100)
    trial = _trial(tmp_path)
    _write(trial / "agent" / "big.json", {"pad": "x" * 200})
    with pytest.raises(AdmissionRefused, match="file is larger than 100 bytes"):
        _admit(trial)


def test_an_ancestor_sidecar_larger_than_the_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = _trial(tmp_path)
    _write(trial.parent / "result.json", {**JOB, "pad": "x" * 400})
    limit = 300  # every file of the trial fits, the job sidecar does not
    assert max(p.stat().st_size for p in trial.rglob("*") if p.is_file()) < limit
    monkeypatch.setattr(rollout_admission, "MAX_READ_BYTES", limit)
    with pytest.raises(AdmissionRefused, match=f"file is larger than {limit} bytes"):
        _admit(trial)


# ---- (d) task status -----------------------------------------------------------------------------

ROW_OK = {"task_id": "plain-task", "status": "certified", "reason": "ALL_GATES_PASS"}
ROW_OTHER = {"task_id": "weak-task", "status": "weak", "reason": "ATTACK_ACCEPTED"}
DIFFERENT = {"task_id": "plain-task", "status": "weak", "reason": "ATTACK_ACCEPTED"}
REASON_ONLY = {**ROW_OK, "reason": "OTHER_REASON"}


def _status_file(path: Path, form: str, rows: list[Any]) -> Path:
    if form == "object":
        write_json(path, {"tasks": rows})
    elif form == "list":
        write_json(path, rows)
    else:
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


FORMS = ["object", "list", "jsonl"]


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize(
    "bad", [123, "plain-task", None, ["plain-task"]], ids=["int", "str", "null", "list"]
)
def test_a_task_status_row_that_is_not_an_object_is_refused_everywhere(
    fixture_spec: Path, form: str, bad: Any
) -> None:
    path = _status_file(fixture_spec.parent / "status.json", form, [ROW_OK, bad])
    with pytest.raises(ValueError, match="task status row is not an object"):
        hd.load_certified_tasks(path)
    _assert_line_refuses(fixture_spec, "task status row is not an object")


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("other", [DIFFERENT, REASON_ONLY], ids=["status", "reason"])
def test_a_task_repeated_with_a_different_status_is_refused_in_either_order(
    fixture_spec: Path, form: str, other: dict[str, Any]
) -> None:
    for order in ([ROW_OK, ROW_OTHER, other], [other, ROW_OTHER, ROW_OK]):
        path = _status_file(fixture_spec.parent / "status.json", form, order)
        with pytest.raises(
            ValueError, match="task status repeats 'plain-task' with a different status"
        ):
            hd.load_certified_tasks(path)
        _assert_line_refuses(
            fixture_spec, "task status repeats 'plain-task' with a different status"
        )


@pytest.mark.parametrize("form", FORMS)
def test_an_identical_task_status_repeat_is_fine_everywhere(fixture_spec: Path, form: str) -> None:
    path = _status_file(
        fixture_spec.parent / "status.json",
        form,
        [ROW_OK, ROW_OTHER, dict(ROW_OK), dict(ROW_OTHER)],
    )
    assert hd.load_certified_tasks(path) == {
        "plain-task": {"status": "certified", "reason": "ALL_GATES_PASS"},
        "weak-task": {"status": "weak", "reason": "ATTACK_ACCEPTED"},
    }
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert {r["task_id"]: r["status"] for r in read_rows(batch / "tasks.jsonl")} == {
        "plain-task": "certified",
        "weak-task": "weak",
    }
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
