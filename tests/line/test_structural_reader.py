"""The line's one reader: sealed inodes, size bound, read identity, symlinked batches, Station rows.

Every refusal below happens before a batch directory, a queue or an export exists; a check of an
existing batch fails closed.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab import rollout_admission
from evallab.line import cli, core
from evallab.line import io as line_io
from evallab.line.io import (
    LABEL_KEYS,
    canonical_hash,
    digest,
    read_bytes,
    read_json,
    read_rows,
    record_reads,
    write_json,
    write_rows,
)

SEALED = "hard link to sealed material"


# ---- shared helpers ----------------------------------------------------------------------------


def _check(batch: Path, root: Path, name: str) -> dict[str, str]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


def _failed(batch: Path, root: Path) -> dict[str, str]:
    return {
        c["name"]: c["details"]
        for c in core.check_batch(batch, root)["checks"]
        if c["status"] != "PASS"
    }


def _assert_nothing_written(spec_path: Path) -> None:
    spec = read_json(spec_path)
    assert not Path(spec["out_root"]).exists()
    assert not (spec_path.parent / "batches" / "synthetic").exists()
    assert not Path(spec["judge"]["queue"]).exists()


def _assert_refused(spec_path: Path, root: Path, pattern: str) -> None:
    """``run_line`` refuses with ``pattern`` and leaves no batch, out_root or queue behind."""
    with pytest.raises(ValueError, match=pattern):
        core.run_line(spec_path, root)
    _assert_nothing_written(spec_path)


def _rehash(batch: Path) -> None:
    """Re-hash and re-count every receipt reference that points inside the batch."""
    root = batch.resolve()
    for path in sorted((batch / "receipts").glob("*.json")):
        receipt = read_json(path)
        for side in ("inputs", "outputs"):
            for ref in receipt[side]:
                target = core.resolve_ref(batch, ref["path"])
                if root not in target.resolve().parents:
                    continue
                ref["sha256"] = digest(target)
                if "rows" in ref:
                    ref["rows"] = len(read_rows(target))
        write_json(path, receipt)


def _edit_receipt(batch: Path, stage: str, edit: Callable[[dict[str, Any]], None]) -> None:
    path = batch / "receipts" / f"{stage}.json"
    receipt = read_json(path)
    edit(receipt)
    write_json(path, receipt)


def _tree(batch: Path) -> dict[str, bytes]:
    return {
        p.relative_to(batch).as_posix(): p.read_bytes() for p in batch.rglob("*") if p.is_file()
    }


def _not_eligible(run_id: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "stage_decisions": {
            "autoreview": {
                "decision": "pass",
                "reasons": ["AUTOREVIEW_NOT_ELIGIBLE"],
                "details": {"eligible": False},
            }
        },
    }


def _declared(spec_path: Path, kind: str) -> Path:
    """The path of one declared input; ``cache`` and ``verdicts`` are created on demand."""
    spec = read_json(spec_path)
    if kind == "rollouts":
        return Path(spec["sources"][0]["rollouts"])
    if kind == "labels":
        return Path(spec["sources"][0]["labels"])
    if kind == "task_status":
        return Path(spec["task_status"])
    if kind == "verdicts":
        path = Path(spec["judge"]["verdicts"])
        write_rows(path, [_station_row(run_id="clean-run", verdict="legitimate")])
        return path
    assert kind == "cache"
    path = spec_path.parent / "cache.jsonl"
    write_rows(path, [_not_eligible("clean-run"), _not_eligible("weak-run")])
    spec["autoreview"] = {"mode": "cached", "cache": str(path), "max_usd": 0}
    spec["models"]["allowed"] = ["synthetic/model"]
    write_json(spec_path, spec)
    return path


def _station_row(**fields: Any) -> dict[str, Any]:
    """A Station verdict row as the Station writes it, reveal block and all."""
    row: dict[str, Any] = {
        "schema": "judge-verdict/v1",
        "id": "x",
        "judge": "j",
        "run_id": "a1",
        "blind_id": "b",
        "queue": "q",
        "source_queues": ["q"],
        "order": "o",
        "stratum": None,
        "verdict": "clean",
        "mechanism": None,
        "evidence_steps": [],
        "confidence": 3,
        "confidence_touched": False,
        "note": "",
        "seconds": 1.0,
        "opened_timeline": False,
        "card_hints": False,
        "seen_label_before": False,
        "revealed": {"label": {"state": "known", "text": "clean"}, "grader": None},
        "agreement": True,
        "detector_agreement": {},
        "ts": "2026-01-01T00:00:00Z",
    }
    row.update(fields)
    return row


def _sealed_root(tmp_path: Path) -> tuple[Path, Path]:
    """A repo root with sealed material; spec and inputs live outside ``research/``."""
    root = tmp_path / "repo"
    secret = root / "research" / "answer-keys" / "secret.jsonl"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"id": "clean-run", "is_cheat": true}\n')
    return root, secret


# ---- 1. sealed inodes --------------------------------------------------------------------------

KINDS = ["rollouts", "labels", "task_status", "verdicts", "cache"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_declared_input_that_is_a_hard_link_of_sealed_material_is_refused_before_any_output(
    fixture_spec: Path, tmp_path: Path, kind: str
) -> None:
    root, secret = _sealed_root(tmp_path)
    target = _declared(fixture_spec, kind)
    secret.write_bytes(target.read_bytes())  # identical bytes: only the link can be the reason
    target.unlink()
    os.link(secret, target)
    assert os.stat(target).st_ino == os.stat(secret).st_ino
    _assert_refused(fixture_spec, root, SEALED)


@pytest.mark.parametrize("kind", KINDS)
def test_the_same_layout_without_the_link_runs(
    fixture_spec: Path, tmp_path: Path, kind: str
) -> None:
    root, secret = _sealed_root(tmp_path)
    target = _declared(fixture_spec, kind)
    secret.write_bytes(target.read_bytes())  # sealed file exists, but is not linked
    batch = core.run_line(fixture_spec, root)
    assert read_json(batch / "check.json")["passed"]
    assert core.check_batch(batch, root)["passed"]


@pytest.mark.parametrize("kind", ["rollouts", "labels", "task_status", "cache"])
def test_a_declared_input_replaced_by_a_sealed_hard_link_after_the_run_fails_the_check(
    fixture_spec: Path, tmp_path: Path, kind: str
) -> None:
    root, secret = _sealed_root(tmp_path)
    target = _declared(fixture_spec, kind)
    batch = core.run_line(fixture_spec, root)
    assert core.check_batch(batch, root)["passed"]
    secret.write_bytes(target.read_bytes())
    target.unlink()
    os.link(secret, target)
    try:
        result = core.check_batch(batch, root)
    except ValueError as error:
        assert SEALED in str(error)
        return
    assert not result["passed"]
    problems = {c["name"]: c["details"] for c in result["checks"] if c["status"] != "PASS"}
    assert problems, result
    assert any(SEALED in text for text in problems.values()), problems


def test_a_sealed_hard_link_is_refused_by_the_reader_only_inside_the_scope(
    tmp_path: Path,
) -> None:
    root, secret = _sealed_root(tmp_path)
    alias = tmp_path / "plain.jsonl"
    os.link(secret, alias)
    assert read_bytes(alias) == secret.read_bytes()  # outside any scope: nothing is sealed
    with line_io.sealed_scope(root):
        with pytest.raises(ValueError, match=SEALED):
            read_bytes(alias)
        with pytest.raises(ValueError, match=SEALED):
            read_bytes(secret)
        other = tmp_path / "other.jsonl"
        other.write_text("{}\n")
        assert read_bytes(other) == b"{}\n"


# ---- 2. size bound -----------------------------------------------------------------------------


def _bound(fixture_spec: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Set the bound just above every file the run reads; return it."""
    spec = read_json(fixture_spec)
    files = [
        fixture_spec,
        Path(spec["sources"][0]["rollouts"]),
        Path(spec["sources"][0]["labels"]),
        Path(spec["task_status"]),
        fixture_spec.parent / "clean.py",
    ]
    limit = 1 << 15
    assert max(p.stat().st_size for p in files) < limit
    monkeypatch.setattr(line_io, "MAX_READ_BYTES", limit)
    monkeypatch.setattr(rollout_admission, "MAX_READ_BYTES", limit)
    return limit


def _pad(path: Path, size: int) -> None:
    data = path.read_bytes()
    assert len(data) <= size
    path.write_bytes(data + b" " * (size - len(data)))
    assert path.stat().st_size == size


@pytest.mark.parametrize("kind", ["labels", "task_status", "rollouts"])
def test_a_declared_input_bigger_than_the_bound_is_refused_before_any_output(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    limit = _bound(fixture_spec, monkeypatch)
    _pad(_declared(fixture_spec, kind), limit + 1)
    _assert_refused(fixture_spec, fixture_spec.parent, "larger than")


@pytest.mark.parametrize("kind", ["labels", "task_status", "rollouts"])
def test_a_declared_input_of_exactly_the_bound_is_read(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    limit = _bound(fixture_spec, monkeypatch)
    _pad(_declared(fixture_spec, kind), limit)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"]


def test_the_reader_refuses_a_file_one_byte_over_and_reads_one_at_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(line_io, "MAX_READ_BYTES", 64)
    exact = tmp_path / "exact"
    exact.write_bytes(b"x" * 64)
    assert read_bytes(exact) == b"x" * 64
    over = tmp_path / "over"
    over.write_bytes(b"x" * 65)
    with pytest.raises(ValueError, match="larger than 64 bytes"):
        read_bytes(over)


# ---- 3. read identity --------------------------------------------------------------------------


def test_a_file_that_changed_between_reads_through_two_names_is_unstable(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_bytes(b"one")
    b = tmp_path / "b.txt"
    os.link(a, b)
    with record_reads() as log:
        assert read_bytes(a) == b"one"
        a.write_bytes(b"two")  # same inode: b now holds the new bytes
        assert read_bytes(b) == b"two"
    unstable = log.unstable()
    assert len(unstable) == 1
    assert str(a) in unstable[0] and str(b) in unstable[0]


def test_two_reads_of_unchanged_bytes_through_an_alias_and_the_original_are_stable(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.txt"
    a.write_bytes(b"same")
    b = tmp_path / "b.txt"
    os.link(a, b)
    with record_reads() as log:
        read_bytes(a)
        read_bytes(b)
        read_bytes(a)
    assert log.unstable() == []


def test_distinct_files_with_different_bytes_are_not_unstable(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    with record_reads() as log:
        read_bytes(one)
        read_bytes(two)
    assert log.unstable() == []


def test_a_file_that_changed_between_reads_under_one_name_is_unstable(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_bytes(b"one")
    with record_reads() as log:
        read_bytes(a)
        a.write_bytes(b"two")
        read_bytes(a)
    assert log.unstable() == [str(a)]


def test_a_receipt_naming_an_input_through_a_hard_link_alias_sees_the_change_in_the_check(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    status = Path(read_json(fixture_spec)["task_status"])
    alias = root / "status-alias.json"
    os.link(status, alias)

    def name_the_alias(receipt: dict[str, Any]) -> None:
        for ref in receipt["inputs"]:
            if ref["path"] == str(status):
                ref["path"] = str(alias)
        assert any(ref["path"] == str(alias) for ref in receipt["inputs"])

    _edit_receipt(batch, "task", name_the_alias)
    assert core.check_batch(batch, root)["passed"]  # an alias of the declared file is fine

    original = status.read_bytes()
    real = hd.load_certified_tasks

    def parse_other_bytes(*args: Any, **kwargs: Any) -> Any:
        # The loader sees different (still valid) bytes than the receipt's alias will give.
        status.write_bytes(original + b" ")
        try:
            return real(*args, **kwargs)
        finally:
            status.write_bytes(original)

    monkeypatch.setattr(hd, "load_certified_tasks", parse_other_bytes)
    result = core.check_batch(batch, root)
    provenance = next(c for c in result["checks"] if c["name"] == "provenance")
    assert provenance["status"] == "FAIL"
    assert "changed while being checked" in provenance["details"]
    assert str(status) in provenance["details"] and str(alias) in provenance["details"]
    assert not result["passed"]


# ---- 4. a symlinked batch ----------------------------------------------------------------------


def _args(command: str, path: Path, *, as_json: bool = False) -> argparse.Namespace:
    name, _, flag = command.partition(" ")
    return argparse.Namespace(
        line_command=name, path=path, json=as_json, fast=flag == "--fast", command="line"
    )


COMMANDS = ["check", "check --fast", "scoreboard"]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("where", ["beside_the_batch_root", "in_the_out_root"])
@pytest.mark.parametrize("as_json", [False, True])
def test_a_symlinked_batch_path_is_refused_and_its_target_is_untouched(
    fixture_spec: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    where: str,
    as_json: bool,
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    link = (tmp_path / "link") if where == "beside_the_batch_root" else (batch.parent / "alias")
    link.symlink_to(batch, target_is_directory=True)
    before = _tree(batch)
    capsys.readouterr()
    code = cli._command(_args(command, link, as_json=as_json), root)
    out = capsys.readouterr().out
    assert code == 2
    assert "symlink" in out
    assert _tree(batch) == before


@pytest.mark.parametrize(
    "command,code", [("check", 0), ("check --fast", 3), ("scoreboard", 0)], ids=COMMANDS
)
def test_the_same_commands_on_the_real_batch_path_run(
    fixture_spec: Path, capsys: pytest.CaptureFixture[str], command: str, code: int
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    capsys.readouterr()
    assert cli._command(_args(command, batch), root) == code
    assert "symlink" not in capsys.readouterr().out


@pytest.mark.parametrize("command", COMMANDS)
def test_a_relative_symlink_to_a_valid_batch_is_refused_not_followed(
    fixture_spec: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    (tmp_path / "rel").symlink_to(os.path.relpath(batch, tmp_path), target_is_directory=True)
    before = _tree(batch)
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    assert cli._command(_args(command, Path("rel")), root) == 2
    assert "symlink" in capsys.readouterr().out
    assert _tree(batch) == before


# ---- 5. Station rows ---------------------------------------------------------------------------

NON_DEV = "declares a non-DEV split"
LABEL_FIELD = "evaluator/label field"


def _data(*rows: Any) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def _upper(key: str) -> str:
    return key.upper()


def test_a_real_station_verdict_row_is_accepted_with_its_reveal_block() -> None:
    row = _station_row()
    assert row["revealed"]["label"]["text"] == "clean"
    assert core._station_verdicts(_data(row), {"a1"}) == [row]


@pytest.mark.parametrize("block", ["revealed", "reveal", "detector_agreement"])
def test_label_keys_inside_the_stations_own_reveal_blocks_are_accepted(block: str) -> None:
    row = _station_row(**{block: {"label": {"text": "cheat"}, "is_cheat": True, "behavior": "x"}})
    assert core._station_verdicts(_data(row), {"a1"}) == [row]


@pytest.mark.parametrize("declared", ["dev", None])
def test_a_station_row_may_declare_the_dev_split_or_none(declared: Any) -> None:
    row = _station_row(split=declared)
    assert core._station_verdicts(_data(row), {"a1"}) == [row]


def test_an_undo_row_is_accepted_and_cancels_its_verdict() -> None:
    undo = {"schema": "judge-undo/v1", "undoes": "x", "judge": "j", "ts": "t", "run_id": "a1"}
    assert core._station_verdicts(_data(_station_row(), undo), {"a1"}) == []
    # An undo that undoes nothing leaves the other verdicts alone.
    other = _station_row(id="y", run_id="a1")
    assert core._station_verdicts(_data(other, undo), {"a1"}) == [other]


def test_station_rows_of_runs_that_were_not_admitted_are_dropped_but_still_validated() -> None:
    assert core._station_verdicts(_data(_station_row(run_id="ghost")), {"a1"}) == []
    with pytest.raises(ValueError, match=LABEL_FIELD):
        core._station_verdicts(_data(_station_row(run_id="ghost", is_cheat=True)), {"a1"})


BAD_SPLITS: dict[str, dict[str, Any]] = {
    "test": {"split": "test"},
    "Test": {"split": "Test"},
    "TEST": {"split": "TEST"},
    "train": {"split": "train"},
    "key in capitals": {"SPLIT": "test"},
    "number": {"split": 1},
    "nested object": {"meta": {"split": "test"}},
    "nested in a list": {"extra": [{"deeper": {"Split": "held"}}]},
    "inside the reveal block": {"revealed": {"label": None, "split": "test"}},
    "inside the note": {"note": {"split": "test"}},
}


@pytest.mark.parametrize("fields", BAD_SPLITS.values(), ids=BAD_SPLITS.keys())
def test_a_station_row_declaring_a_non_dev_split_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=NON_DEV):
        core._station_verdicts(_data(_station_row(**fields)), {"a1"})


BAD_LABELS: dict[str, dict[str, Any]] = {
    **{f"top-level {key}": {key: True} for key in sorted(LABEL_KEYS)},
    **{f"top-level {key} in capitals": {_upper(key): True} for key in sorted(LABEL_KEYS)},
    "mixed case": {"Is_Cheat": True},
    "inside a note object": {"note": {"is_cheat": True}},
    "inside a note list": {"note": [{"ground_truth": "cheat"}]},
    "deep in a list": {"extra": {"a": [{"b": [{"Behavior": "x"}]}]}},
    "a reveal key that is not top-level": {"note": {"revealed": {"is_cheat": True}}},
    "inside evidence": {"evidence": {"label": "cheat"}},
}


@pytest.mark.parametrize("fields", BAD_LABELS.values(), ids=BAD_LABELS.keys())
def test_a_station_row_carrying_a_label_field_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=LABEL_FIELD):
        core._station_verdicts(_data(_station_row(**fields)), {"a1"})


def test_a_bad_station_row_after_good_ones_is_still_refused_with_its_line_number() -> None:
    data = _data(_station_row(id="g1"), _station_row(id="g2"), _station_row(id="g3", label="x"))
    with pytest.raises(ValueError, match=rf"{LABEL_FIELD}.*line 3"):
        core._station_verdicts(data, {"a1"})


@pytest.mark.parametrize("line", ["[1, 2]", "7", '"x"', "null"])
def test_a_station_line_that_is_json_but_not_an_object_is_refused(line: str) -> None:
    with pytest.raises(ValueError, match="line 2 is not an object"):
        core._station_verdicts(_data(_station_row()) + line.encode() + b"\n", {"a1"})


@pytest.mark.parametrize(
    "bad",
    [
        {"is_cheat": True},
        {"note": {"ground_truth": "cheat"}},
        {"split": "test"},
        {"meta": {"Split": "test"}},
    ],
    ids=["label-key", "nested-label-key", "split", "nested-split"],
)
def test_run_line_refuses_a_station_file_with_such_a_row_before_any_output(
    fixture_spec: Path, bad: dict[str, Any]
) -> None:
    spec = read_json(fixture_spec)
    write_rows(
        Path(spec["judge"]["verdicts"]),
        [
            _station_row(id="v1", run_id="clean-run"),
            _station_row(id="v2", run_id="weak-run", **bad),
        ],
    )
    _assert_refused(fixture_spec, fixture_spec.parent, f"{LABEL_FIELD}|{NON_DEV}")


def test_run_line_accepts_the_real_shaped_station_rows(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    write_rows(
        Path(spec["judge"]["verdicts"]),
        [
            _station_row(id="v1", run_id="clean-run"),
            {
                "schema": "judge-undo/v1",
                "undoes": "gone",
                "judge": "j",
                "ts": "t",
                "run_id": "weak-run",
            },
        ],
    )
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"]
    assert [r["id"] for r in read_rows(batch / "verdicts.jsonl")] == ["v1"]


def _verdict_batch(fixture_spec: Path) -> Path:
    spec = read_json(fixture_spec)
    write_rows(
        Path(spec["judge"]["verdicts"]),
        [_station_row(id="v1", run_id="clean-run"), _station_row(id="v2", run_id="weak-run")],
    )
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    return batch


FORGED: dict[str, Callable[[list[dict[str, Any]]], None]] = {
    "top-level ground_truth": lambda rows: rows[0].update(ground_truth="cheat"),
    "is_cheat": lambda rows: rows[0].update(is_cheat=True),
    "nested in the note": lambda rows: rows[1].update(note={"behavior": "x"}),
    "a split": lambda rows: rows[0].update(split="test"),
}


@pytest.mark.parametrize("forge", list(FORGED))
def test_a_forged_verdicts_file_carrying_a_label_key_fails_judge_routing(
    fixture_spec: Path, forge: str
) -> None:
    batch = _verdict_batch(fixture_spec)
    rows = read_rows(batch / "verdicts.jsonl")
    FORGED[forge](rows)
    write_rows(batch / "verdicts.jsonl", rows)
    _rehash(batch)
    failed = _failed(batch, fixture_spec.parent)
    assert "judge_routing" in failed, failed
    assert "verdicts.jsonl differs from its Station snapshot" in failed["judge_routing"]


@pytest.mark.parametrize("forge", list(FORGED))
def test_a_forged_station_snapshot_with_a_label_key_fails_judge_routing_with_the_reason(
    fixture_spec: Path, forge: str
) -> None:
    batch = _verdict_batch(fixture_spec)
    [snapshot] = (batch / "evidence" / "station-inputs").glob("*.jsonl")
    rows = read_rows(snapshot)
    FORGED[forge](rows)
    write_rows(snapshot, rows)
    write_rows(batch / "verdicts.jsonl", rows)
    _rehash(batch)
    failed = _failed(batch, fixture_spec.parent)
    assert "judge_routing" in failed, failed
    assert LABEL_FIELD in failed["judge_routing"] or NON_DEV in failed["judge_routing"]


# ---- 6. non-object JSONL lines -----------------------------------------------------------------

NOT_OBJECT = ["[1,2]", '"x"', "7", "null", "true"]


def _labels_text(spec_path: Path, text: str) -> Path:
    path = _declared(spec_path, "labels")
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("line", NOT_OBJECT)
@pytest.mark.parametrize("position", ["after", "before"])
def test_a_labels_line_that_is_json_but_not_an_object_is_refused(
    fixture_spec: Path, line: str, position: str
) -> None:
    good = '{"id": "clean-run", "is_cheat": false}'
    text = f"{good}\n{line}\n" if position == "after" else f"{line}\n{good}\n"
    path = _labels_text(fixture_spec, text)
    with pytest.raises(ValueError, match="is not an object"):
        hd.load_labels(path, repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, fixture_spec.parent, "label row is not an object")


@pytest.mark.parametrize("line", ["7", '"x"'])
def test_a_labels_file_that_is_one_scalar_line_is_refused(fixture_spec: Path, line: str) -> None:
    path = _labels_text(fixture_spec, line + "\n")
    with pytest.raises(ValueError, match="label row is not an object"):
        hd.load_labels(path, repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, fixture_spec.parent, "label row is not an object")


@pytest.mark.parametrize("line", NOT_OBJECT)
@pytest.mark.parametrize("position", ["after", "before"])
def test_a_manifest_line_that_is_json_but_not_an_object_is_refused(
    fixture_spec: Path, line: str, position: str
) -> None:
    manifest = _declared(fixture_spec, "rollouts")
    good = manifest.read_text()
    manifest.write_text(good + line + "\n" if position == "after" else line + "\n" + good)
    with pytest.raises(ValueError, match="rollout manifest row is not an object"):
        hd.load_rollouts([manifest], repo_root=fixture_spec.parent)
    _assert_refused(fixture_spec, fixture_spec.parent, "rollout manifest row is not an object")


# ---- 7. the live trace row schema --------------------------------------------------------------

ROUTE = "synthetic/model"
OTHER_ROUTE = "synthetic/other"
AR_KEYS = ("stage", "decision", "reasons", "evidence_steps", "details")


@pytest.fixture
def live_spec(fixture_spec: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    steps = [
        {"step_id": 0, "source": "user", "message": "Fix the bug"},
        {
            "step_id": 1,
            "source": "assistant",
            "tool_calls": [{"name": "write", "arguments": {"path": "a.py", "content": "x = 1\n"}}],
        },
    ]
    rows = []
    for index in (1, 2):
        trajectory = fixture_spec.parent / f"t{index}.json"
        write_json(trajectory, {"steps": steps})
        rows.append({"id": f"run-{index}", "task_id": "plain-task", "path": str(trajectory)})
    spec = read_json(fixture_spec)
    write_rows(Path(spec["sources"][0]["rollouts"]), rows)
    write_rows(Path(spec["sources"][0]["labels"]), [])
    spec["autoreview"] = {
        "mode": "live",
        "route": ROUTE,
        "approval": "synthetic test",
        "max_usd": 1.0,
        "max_call_usd": 0.6,
    }
    spec["models"]["allowed"] = [ROUTE, OTHER_ROUTE]
    write_json(fixture_spec, spec)
    soft = hd.StageVerdict("l0", "review", ["L0_SOFT_TARGET_SURFACE_WRITE"], [], {})
    monkeypatch.setattr(hd, "evaluate_l0_stage", lambda _item: soft)
    return fixture_spec


def _stub(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(prompt: str, *, model: str, **_: Any) -> dict[str, Any]:
        return {
            "stdout": json.dumps({"suspicion": 10, "verdict": "clean"}),
            "served_models": [model],
            "n_fallbacks": 0,
            "usage": {"usd": 0.1},
        }

    monkeypatch.setattr(hd, "run_omp_session", fake)


def _live_batch(live_spec: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _stub(monkeypatch)
    batch = core.run_line(live_spec, live_spec.parent)
    assert core.check_batch(batch, live_spec.parent)["passed"]
    return batch


def _retrace(batch: Path, root: Path, mutate: Callable[[list[dict[str, Any]]], None]) -> None:
    """Edit trace.jsonl, then make every other file agree with the edited trace."""
    rows = read_rows(batch / "trace.jsonl")
    mutate(rows)
    write_rows(batch / "trace.jsonl", rows)
    spec = read_json(batch / "spec.json")
    items, intake = core._intake(spec, root)
    stored = {
        r["run_id"]: hd.StageVerdict(**{k: v for k, v in r["autoreview"].items() if k in AR_KEYS})
        for r in rows
    }
    decisions, _, _ = core._evaluate(spec, items, intake, core._tasks(spec, items), {}, stored)
    write_rows(batch / "decisions.jsonl", decisions)
    for decision, name in (("keep", "kept"), ("drop", "dropped"), ("review", "review")):
        write_rows(
            batch / "export" / f"{name}.jsonl", [d for d in decisions if d["decision"] == decision]
        )
    queue = core._queue(decisions)
    write_rows(batch / "judge-queue.jsonl", queue)
    check = read_json(batch / "check.json")
    check["decision_hash"] = check["second_decision_hash"] = canonical_hash(decisions)
    write_json(batch / "check.json", check)
    _rehash(batch)
    _edit_receipt(batch, "judge", lambda r: r["counts"].update(rows_out=len(queue)))
    spend = sum(r["autoreview"]["call"]["cost_usd"] for r in rows if "call" in r["autoreview"])
    routes = [{"role": "autoreview", "route": x} for x in sorted(core.used_routes(rows))]
    errors = core._trace_errors(rows)
    _edit_receipt(batch, "trace", lambda r: r.update(spend_usd=spend, models=routes, errors=errors))


def _row(rows: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    return next(r for r in rows if r["run_id"] == run_id)["autoreview"]


def _live_failure(
    batch: Path, root: Path, message: str, run_id: str, *, also: frozenset[str] = frozenset()
) -> None:
    determinism = _check(batch, root, "determinism")
    assert determinism["status"] == "FAIL", determinism
    assert message in determinism["details"] and run_id in determinism["details"], determinism
    assert "decision hashes differ" not in determinism["details"]
    assert "differs from recomputation" not in determinism["details"]
    assert _check(batch, root, "provenance")["status"] == "PASS"
    failed = _failed(batch, root)
    assert set(failed) - {"scoreboard_recompute"} - also == {"determinism"}, failed


def test_an_untouched_live_batch_passes_every_gate_and_so_does_a_no_op_retrace(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _live_batch(live_spec, monkeypatch)
    rows = read_rows(batch / "trace.jsonl")
    for row in rows:
        ar = row["autoreview"]
        assert set(ar) == {*AR_KEYS, "mode", "score", "cited_steps", "call"}
        assert ar["stage"] == "autoreview" and ar["mode"] == "live" and ar["score"] == 10
        assert ar["details"]["model"] == ROUTE and ar["call"]["requested_route"] == ROUTE
        assert set(ar["call"]) == {"requested_route", "served_models", "n_fallbacks", "cost_usd"}
    _retrace(batch, live_spec.parent, lambda _rows: None)
    assert not _failed(batch, live_spec.parent)


def _update(run_id: str, **fields: Any) -> Callable[[list[dict[str, Any]]], None]:
    return lambda rows: _row(rows, run_id).update(fields)


def _change_call(run_id: str, **fields: Any) -> Callable[[list[dict[str, Any]]], None]:
    return lambda rows: _row(rows, run_id)["call"].update(fields)


def _drop_call_key(run_id: str, key: str) -> Callable[[list[dict[str, Any]]], None]:
    return lambda rows: _row(rows, run_id)["call"].pop(key)


def _reroute(rows: list[dict[str, Any]]) -> None:
    ar = _row(rows, "run-1")
    ar["details"]["model"] = OTHER_ROUTE
    ar["call"].update(requested_route=OTHER_ROUTE, served_models=[OTHER_ROUTE])


def _fake_budget(rows: list[dict[str, Any]]) -> None:
    ar = _row(rows, "run-2")
    ar.pop("call")
    ar.update(
        decision="review",
        reasons=["AUTOREVIEW_BUDGET_EXCEEDED"],
        evidence_steps=[],
        cited_steps=[],
        details={"calls": 1, "spent_usd": 0.1},
        score=None,
    )


def _fake_no_trajectory(rows: list[dict[str, Any]]) -> None:
    ar = _row(rows, "run-1")
    ar.pop("call")
    ar.update(
        decision="review",
        reasons=["AUTOREVIEW_NO_TRAJECTORY"],
        evidence_steps=[],
        cited_steps=[],
        details={"note": "AutoReview requires ATIF trajectory run"},
        score=None,
    )


def _fake_error(details: dict[str, Any]) -> Callable[[list[dict[str, Any]]], None]:
    def mutate(rows: list[dict[str, Any]]) -> None:
        ar = _row(rows, "run-1")
        ar.pop("call")
        ar.update(
            decision="review",
            reasons=["AUTOREVIEW_ERROR:Foo"],
            evidence_steps=[],
            cited_steps=[],
            details=details,
            score=None,
        )

    return mutate


# A call record without its fallback count (or with a boolean one) also fails the spend gate.
SPEND_NOISE = frozenset({"a missing key in the call", "a boolean fallback count"})


LIVE_TAMPERS: dict[str, tuple[Callable[[list[dict[str, Any]]], None], str]] = {
    "an extra key in the row": (_update("run-1", extra="x"), "unexpected or missing fields"),
    "a missing key in the row": (
        lambda rows: _row(rows, "run-1").pop("cited_steps"),
        "unexpected or missing fields",
    ),
    "stage renamed": (_update("run-1", stage="l0"), "disagrees with its own reading"),
    "mode off": (_update("run-1", mode="off"), "disagrees with its own reading"),
    "mode cached": (_update("run-1", mode="cached"), "disagrees with its own reading"),
    "score 999": (_update("run-1", score=999), "disagrees with its own reading"),
    "score missing": (_update("run-1", score=None), "disagrees with its own reading"),
    "cited steps 999": (_update("run-1", cited_steps=[999]), "disagrees with its own reading"),
    "cited steps unlike the evidence": (
        _update("run-1", cited_steps=[0, 1]),
        "disagrees with its own reading",
    ),
    "an extra key in the call": (_change_call("run-1", model="x"), "unexpected shape"),
    "a missing key in the call": (_drop_call_key("run-1", "n_fallbacks"), "unexpected shape"),
    "a boolean fallback count": (_change_call("run-1", n_fallbacks=True), "unexpected shape"),
    "a negative cost": (_change_call("run-1", cost_usd=-0.1), "unexpected shape"),
    "a rerouted reading": (_reroute, "not backed by its call record"),
    "a budget exhaustion that did not happen": (_fake_budget, "skipped without cause"),
    "no trajectory on a run that has one": (_fake_no_trajectory, "skipped without cause"),
    "an error with empty details": (_fake_error({}), "error reading is not what the run could"),
    "an error with other details": (
        _fake_error({"error": "Bar"}),
        "error reading is not what the run could",
    ),
}


@pytest.mark.parametrize("tamper", list(LIVE_TAMPERS))
def test_a_tampered_live_trace_row_fails_determinism_with_its_message(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    batch = _live_batch(live_spec, monkeypatch)
    mutate, message = LIVE_TAMPERS[tamper]
    run_id = "run-2" if tamper == "a budget exhaustion that did not happen" else "run-1"
    _retrace(batch, live_spec.parent, mutate)
    noisy = frozenset({"spend_and_models"}) if tamper in SPEND_NOISE else frozenset()
    _live_failure(batch, live_spec.parent, message, run_id, also=noisy)


def test_the_validator_names_the_row_and_the_reason_directly(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _live_batch(live_spec, monkeypatch)
    spec = read_json(batch / "spec.json")
    items, _ = core._intake(spec, live_spec.parent)
    stored = read_rows(batch / "trace.jsonl")
    fresh = [{"run_id": r["run_id"], "l0": r["l0"]} for r in stored]
    core._validate_live_trace(stored, fresh, spec, items)  # the untouched trace is valid
    _row(stored, "run-1")["score"] = 999
    with pytest.raises(ValueError, match="disagrees with its own reading: run-1"):
        core._validate_live_trace(stored, fresh, spec, items)


def _tight_budget(live_spec: Path) -> None:
    spec = read_json(live_spec)
    spec["autoreview"].update(max_usd=0.1, max_call_usd=0.1)
    write_json(live_spec, spec)


def test_a_real_budget_exhaustion_passes_and_a_wrong_spend_in_it_fails(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tight_budget(live_spec)
    batch = _live_batch(live_spec, monkeypatch)
    rows = read_rows(batch / "trace.jsonl")
    assert _row(rows, "run-1")["reasons"] == ["AUTOREVIEW_CLEAN"]
    assert _row(rows, "run-2")["reasons"] == ["AUTOREVIEW_BUDGET_EXCEEDED"]
    _retrace(
        batch,
        live_spec.parent,
        lambda rows: _row(rows, "run-2")["details"].update(spent_usd=0.05),
    )
    _live_failure(batch, live_spec.parent, "differs from the walk of the budget", "run-2")


def test_a_real_budget_exhaustion_cannot_be_given_a_call_record(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tight_budget(live_spec)
    batch = _live_batch(live_spec, monkeypatch)

    def give_a_call(rows: list[dict[str, Any]]) -> None:
        _row(rows, "run-2")  # the row exists
        next(r for r in rows if r["run_id"] == "run-2")["autoreview"]["call"] = dict(
            _row(rows, "run-1")["call"]
        )

    _retrace(batch, live_spec.parent, give_a_call)
    _live_failure(
        batch,
        live_spec.parent,
        "differs from the walk of the budget",
        "run-2",
        also=frozenset({"spend_and_models"}),  # the planted call also passes the cap
    )
