"""Admission gate: every declared input is refused before a batch directory or queue exists.

Covers the DEV gate failing open (JSON-object manifests, rollout bundle trajectories, split
masking), repeated JSON keys, and malformed label files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_json, read_rows, write_json, write_rows

NON_DEV = "non-DEV split"
DUPLICATE = "duplicate JSON key"
MALFORMED = "malformed label row"


def _edit_spec(spec_path: Path, change: Any) -> dict[str, Any]:
    spec = read_json(spec_path)
    change(spec)
    write_json(spec_path, spec)
    return spec


def _assert_refused(fixture_spec: Path, pattern: str) -> None:
    """``run_line`` refuses with ``pattern`` and leaves no batch, out_root or queue behind."""
    root = fixture_spec.parent
    spec = read_json(fixture_spec)
    with pytest.raises(ValueError, match=pattern):
        core.run_line(fixture_spec, root)
    assert not (root / "batches" / "synthetic").exists()
    assert not Path(spec["out_root"]).exists()
    assert not Path(spec["judge"]["queue"]).exists()


def _manifest_rows(fixture_spec: Path) -> list[dict[str, Any]]:
    return read_rows(Path(read_json(fixture_spec)["sources"][0]["rollouts"]))


def _json_manifest(fixture_spec: Path, document: dict[str, Any]) -> Path:
    """Replace the fixture's JSONL manifest with a ``rollouts.json`` object document."""
    path = fixture_spec.parent / "rollouts.json"
    write_json(path, document)

    def point(spec: dict[str, Any]) -> None:
        spec["sources"][0]["rollouts"] = str(path)

    _edit_spec(fixture_spec, point)
    return path


def _raw_manifest(fixture_spec: Path, lines: list[str], name: str = "rollouts.jsonl") -> Path:
    path = fixture_spec.parent / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def point(spec: dict[str, Any]) -> None:
        spec["sources"][0]["rollouts"] = str(path)

    _edit_spec(fixture_spec, point)
    return path


def _labels_path(fixture_spec: Path) -> Path:
    return Path(read_json(fixture_spec)["sources"][0]["labels"])


def _write_labels(fixture_spec: Path, lines: list[str]) -> Path:
    path = _labels_path(fixture_spec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _bundle_spec(fixture_spec: Path, tmp_path: Path) -> Path:
    """Point the fixture at one run whose rollout is a directory bundle (no trajectory yet)."""
    spec = read_json(fixture_spec)
    bundle = tmp_path / "trial_bundle"
    bundle.mkdir()
    write_rows(
        Path(spec["sources"][0]["rollouts"]),
        [{"id": "bundle-run", "task_id": "plain-task", "path": str(bundle), "reward": 1}],
    )
    write_rows(Path(spec["sources"][0]["labels"]), [{"id": "bundle-run", "is_cheat": False}])
    return bundle


_STEPS = [{"step": 1, "action": "bash", "command": "echo hi"}]

# Every way a split can be declared that must never reach a DEV batch.
_BAD_SPLITS: list[dict[str, Any]] = [
    {"split": "test"},
    {"split": "TEST "},
    {"metadata": {"split": "test"}},
    {"split": "dev", "metadata": {"split": "test"}},  # a DEV top level never masks metadata
    {"split": "test", "metadata": {"split": "dev"}},  # nor metadata the top level
    {"split": "   ", "metadata": {"split": "test"}},
    {"split": ["test"]},
    {"split": 1},
    {"metadata": {"split": ["test"]}},
]
_BAD_IDS = [
    "top-test",
    "top-test-padded",
    "meta-test",
    "dev-top-meta-test",
    "test-top-meta-dev",
    "blank-top-meta-test",
    "list-top",
    "number-top",
    "list-meta",
]


# ---- 1. the DEV gate must not fail open ------------------------------------------------------


@pytest.mark.parametrize("declared", _BAD_SPLITS, ids=_BAD_IDS)
def test_a_json_object_manifest_declaring_a_non_dev_split_is_refused(
    fixture_spec: Path, declared: dict[str, Any]
) -> None:
    _json_manifest(fixture_spec, {**declared, "rows": _manifest_rows(fixture_spec)})
    _assert_refused(fixture_spec, NON_DEV)


@pytest.mark.parametrize("declared", _BAD_SPLITS, ids=_BAD_IDS)
def test_a_json_object_manifest_row_declaring_a_non_dev_split_is_refused(
    fixture_spec: Path, declared: dict[str, Any]
) -> None:
    rows = _manifest_rows(fixture_spec)
    rows[1] = {**rows[1], **declared}
    _json_manifest(fixture_spec, {"rows": rows})
    _assert_refused(fixture_spec, NON_DEV)


@pytest.mark.parametrize("declared", _BAD_SPLITS, ids=_BAD_IDS)
def test_a_jsonl_manifest_row_declaring_a_non_dev_split_is_refused(
    fixture_spec: Path, declared: dict[str, Any]
) -> None:
    rows = _manifest_rows(fixture_spec)
    rows[1] = {**rows[1], **declared}
    write_rows(Path(read_json(fixture_spec)["sources"][0]["rollouts"]), rows)
    _assert_refused(fixture_spec, NON_DEV)


@pytest.mark.parametrize(
    "declared",
    [{"split": "dev"}, {"split": " DEV "}, {"split": None}, {"metadata": {"split": "dev"}}, {}],
    ids=["dev", "dev-padded-cased", "null", "metadata-dev", "absent"],
)
def test_a_json_object_manifest_declaring_dev_or_no_split_is_admitted(
    fixture_spec: Path, declared: dict[str, Any]
) -> None:
    _json_manifest(fixture_spec, {**declared, "rows": _manifest_rows(fixture_spec)})
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert {row["run_id"] for row in read_rows(batch / "runs.jsonl")} == {"clean-run", "weak-run"}
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


_BUNDLE_FILES = ["trajectory.json", "agent/trajectory.json"]
_BUNDLE_DECLARATIONS: list[dict[str, Any]] = [
    {"split": "test"},
    {"metadata": {"split": "test"}},
    {"split": "dev", "metadata": {"split": "test"}},
    {"split": ["test"]},
]


def _write_trajectory(bundle: Path, relative: str, declared: dict[str, Any]) -> None:
    target = bundle / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, {**declared, "steps": _STEPS})


@pytest.mark.parametrize("relative", _BUNDLE_FILES)
@pytest.mark.parametrize(
    "declared",
    _BUNDLE_DECLARATIONS,
    ids=["top-test", "metadata-test", "dev-top-meta-test", "list-top"],
)
def test_a_rollout_bundle_whose_trajectory_declares_a_non_dev_split_is_refused(
    fixture_spec: Path, tmp_path: Path, relative: str, declared: dict[str, Any]
) -> None:
    bundle = _bundle_spec(fixture_spec, tmp_path)
    _write_trajectory(bundle, relative, declared)
    _assert_refused(fixture_spec, NON_DEV)


@pytest.mark.parametrize("relative", _BUNDLE_FILES)
@pytest.mark.parametrize(
    "declared",
    [{"split": "dev"}, {"metadata": {"split": "dev"}}, {}],
    ids=["dev", "metadata-dev", "absent"],
)
def test_a_rollout_bundle_whose_trajectory_declares_dev_or_no_split_is_admitted(
    fixture_spec: Path, tmp_path: Path, relative: str, declared: dict[str, Any]
) -> None:
    bundle = _bundle_spec(fixture_spec, tmp_path)
    _write_trajectory(bundle, relative, declared)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert [row["run_id"] for row in read_rows(batch / "runs.jsonl")] == ["bundle-run"]
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


def test_a_non_dev_trajectory_is_refused_even_when_a_clean_sibling_trajectory_loads_first(
    fixture_spec: Path, tmp_path: Path
) -> None:
    # The loader reads agent/trajectory.json first and keeps only its steps; the root
    # trajectory.json is never parsed by it, yet its declared split must still pass.
    bundle = _bundle_spec(fixture_spec, tmp_path)
    _write_trajectory(bundle, "agent/trajectory.json", {})
    _write_trajectory(bundle, "trajectory.json", {"split": "test"})
    _assert_refused(fixture_spec, NON_DEV)


@pytest.mark.parametrize("declared", _BAD_SPLITS, ids=_BAD_IDS)
def test_a_labels_file_row_declaring_a_non_dev_split_is_refused(
    fixture_spec: Path, declared: dict[str, Any]
) -> None:
    lines = [
        json.dumps({"id": "clean-run", "is_cheat": False}),
        json.dumps({"id": "weak-run", "is_cheat": True, **declared}),
    ]
    _write_labels(fixture_spec, lines)
    with pytest.raises(ValueError, match=NON_DEV):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, NON_DEV)


def test_a_single_row_labels_file_declaring_a_non_dev_split_is_refused(
    fixture_spec: Path,
) -> None:
    _write_labels(
        fixture_spec,
        [
            json.dumps(
                {"id": "weak-run", "is_cheat": True, "split": "   ", "metadata": {"split": "test"}}
            )
        ],
    )
    with pytest.raises(ValueError, match=NON_DEV):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, NON_DEV)


def test_a_labels_file_may_declare_dev_or_a_null_split(fixture_spec: Path) -> None:
    _write_labels(
        fixture_spec,
        [
            json.dumps({"id": "clean-run", "is_cheat": False, "split": None}),
            json.dumps({"id": "weak-run", "is_cheat": True, "split": " Dev ", "metadata": {}}),
        ],
    )
    assert hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent) == {
        "clean-run": False,
        "weak-run": True,
    }
    assert core.check_batch(core.run_line(fixture_spec, fixture_spec.parent), fixture_spec.parent)[
        "passed"
    ]


# ---- 2. a repeated JSON key is refused, wherever it appears ----------------------------------


def test_a_spec_with_a_repeated_key_is_refused(fixture_spec: Path) -> None:
    text = fixture_spec.read_text(encoding="utf-8").rstrip()
    assert text.endswith("}")
    fixture_spec.write_text(text[:-1] + ', "batch_id": "other"}', encoding="utf-8")
    with pytest.raises(ValueError, match=DUPLICATE):
        core.run_line(fixture_spec, fixture_spec.parent)
    root = fixture_spec.parent
    assert not (root / "batches").exists()
    assert not (root / "queue.jsonl").exists()


def test_a_spec_cannot_repeat_split_to_hide_a_non_dev_value(fixture_spec: Path) -> None:
    text = fixture_spec.read_text(encoding="utf-8").rstrip()
    fixture_spec.write_text(text[:-1] + ', "split": "test"}', encoding="utf-8")
    with pytest.raises(ValueError, match=DUPLICATE):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not (fixture_spec.parent / "batches").exists()


def test_a_rollouts_jsonl_row_with_a_repeated_split_key_is_refused(fixture_spec: Path) -> None:
    clean = fixture_spec.parent / "clean.py"
    row = json.dumps({"id": "clean-run", "task_id": "plain-task", "path": str(clean)})
    _raw_manifest(fixture_spec, [row[:-1] + ', "split": "test", "split": "dev"}'])
    _assert_refused(fixture_spec, DUPLICATE)


def test_a_json_manifest_with_a_repeated_top_level_key_is_refused(fixture_spec: Path) -> None:
    rows = json.dumps(_manifest_rows(fixture_spec))
    _raw_manifest(
        fixture_spec,
        ['{"split": "test", "rows": ' + rows + ', "split": "dev"}'],
        name="rollouts.json",
    )
    _assert_refused(fixture_spec, DUPLICATE)


def test_a_json_manifest_row_with_a_repeated_key_is_refused(fixture_spec: Path) -> None:
    clean = fixture_spec.parent / "clean.py"
    row = json.dumps({"id": "clean-run", "task_id": "plain-task", "path": str(clean)})
    _raw_manifest(
        fixture_spec,
        ['{"rows": [' + row[:-1] + ', "split": "test", "split": "dev"}]}'],
        name="rollouts.json",
    )
    _assert_refused(fixture_spec, DUPLICATE)


def test_a_single_line_labels_file_with_a_repeated_key_is_refused(fixture_spec: Path) -> None:
    # One line: the whole-file parse is the only parse, so it must itself refuse a repeat.
    _write_labels(fixture_spec, ['{"id": "weak-run", "is_cheat": true, "is_cheat": false}'])
    with pytest.raises(ValueError, match=DUPLICATE):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, DUPLICATE)


def test_a_multi_line_labels_file_with_a_repeated_key_is_refused(fixture_spec: Path) -> None:
    _write_labels(
        fixture_spec,
        [
            '{"id": "clean-run", "is_cheat": false}',
            '{"id": "weak-run", "is_cheat": true, "is_cheat": false}',
        ],
    )
    with pytest.raises(ValueError, match=DUPLICATE):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, DUPLICATE)


def test_a_json_wrapper_labels_file_with_a_repeated_key_is_refused(fixture_spec: Path) -> None:
    _write_labels(
        fixture_spec,
        ['{"rows": [{"id": "weak-run", "is_cheat": true}], "rows": []}'],
    )
    with pytest.raises(ValueError, match=DUPLICATE):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, DUPLICATE)


def test_a_task_status_file_with_a_repeated_key_is_refused(fixture_spec: Path) -> None:
    status = Path(read_json(fixture_spec)["task_status"])
    status.write_text(
        '{"tasks": [{"task_id": "plain-task", "status": "certified", "reason": "ALL_GATES_PASS",'
        ' "status": "weak"}]}',
        encoding="utf-8",
    )
    _assert_refused(fixture_spec, DUPLICATE)


def test_a_task_status_file_cannot_repeat_a_top_level_key(fixture_spec: Path) -> None:
    status = Path(read_json(fixture_spec)["task_status"])
    status.write_text(
        '{"tasks": [{"task_id": "plain-task", "status": "certified", "reason": "ALL_GATES_PASS"}],'
        ' "tasks": []}',
        encoding="utf-8",
    )
    _assert_refused(fixture_spec, DUPLICATE)


@pytest.mark.parametrize("relative", _BUNDLE_FILES)
def test_a_rollout_bundle_trajectory_with_a_repeated_key_is_refused(
    fixture_spec: Path, tmp_path: Path, relative: str
) -> None:
    bundle = _bundle_spec(fixture_spec, tmp_path)
    target = bundle / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    steps = json.dumps(_STEPS)
    target.write_text('{"split": "test", "split": "dev", "steps": ' + steps + "}", encoding="utf-8")
    _assert_refused(fixture_spec, DUPLICATE)


# ---- 3. a malformed label file is refused, never partly read ---------------------------------


_GARBAGE = [
    "this is not json",
    '{"id": "weak-run", "is_cheat": true, "split": "test"',  # truncated
    '{"id": "weak-run", "is_cheat": tru}',
]
_GARBAGE_IDS = ["garbage", "truncated", "bad-literal"]


@pytest.mark.parametrize("bad", _GARBAGE, ids=_GARBAGE_IDS)
def test_a_labels_file_with_a_malformed_line_is_refused(fixture_spec: Path, bad: str) -> None:
    _write_labels(fixture_spec, ['{"id": "clean-run", "is_cheat": false}', bad])
    with pytest.raises(ValueError, match=MALFORMED):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, MALFORMED)


@pytest.mark.parametrize("bad", _GARBAGE, ids=_GARBAGE_IDS)
def test_a_labels_file_whose_only_line_is_malformed_is_refused(
    fixture_spec: Path, bad: str
) -> None:
    _write_labels(fixture_spec, [bad])
    with pytest.raises(ValueError, match=MALFORMED):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, MALFORMED)


def test_a_malformed_line_before_valid_rows_is_not_skipped(fixture_spec: Path) -> None:
    _write_labels(
        fixture_spec,
        [
            "{not json",
            '{"id": "clean-run", "is_cheat": false}',
            '{"id": "weak-run", "is_cheat": true}',
        ],
    )
    with pytest.raises(ValueError, match=MALFORMED):
        hd.load_labels(_labels_path(fixture_spec), repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, MALFORMED)


def test_a_labels_file_with_blank_lines_still_loads(fixture_spec: Path) -> None:
    path = _labels_path(fixture_spec)
    path.write_text(
        '\n{"id": "clean-run", "is_cheat": false}\n\n   \n{"id": "weak-run", "is_cheat": true}\n\n',
        encoding="utf-8",
    )
    assert hd.load_labels(path, repo_root=fixture_spec.parent) == {
        "clean-run": False,
        "weak-run": True,
    }
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
