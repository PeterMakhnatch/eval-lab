"""Deterministic unit tests for file-access records, framing, and helpers.

No Harbor import, no subprocess, no Docker: everything here runs in the
default unit environment. Behavioral boundaries covered: record shapes (and
their no-attribution contract), the agreed ``EVALLAB_FILE_ACCESS_PATHS``
shape, NUL-frame parsing (valid, torn, corrupt, loss, cross-chunk
reassembly), path classification, snapshot diffing, and the sandbox
helper's filesystem surface (no live ``inotifywait`` required).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab import file_access
from evallab import file_access_capture as capture
from evallab.file_access import (
    COVERAGE_LIMITS,
    FILE_ACCESS_SCHEMA,
    access_record,
    classify_path,
    coverage_record,
    diff_entries,
    file_change_record,
    parse_frames,
    parse_paths_env,
    parse_stream_chunk,
    scan_loss_tokens,
    split_frame_events,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_coverage_record_carries_limits_and_bounded_reason() -> None:
    record = coverage_record(
        window_id=1, state="active", reason="x" * 10_000, watched_paths=["/tests"]
    )
    assert record["schema"] == FILE_ACCESS_SCHEMA
    assert record["source"] == "inotifywait"
    assert record["phase"] == "agent"
    assert record["window_id"] == 1
    assert record["kind"] == "coverage"
    assert record["limits"] == list(COVERAGE_LIMITS)
    assert len(record["reason"]) == file_access.REASON_MAX_CHARS
    assert "pid" not in record and "command" not in record and "step" not in record


def test_coverage_record_rejects_unknown_state() -> None:
    with pytest.raises(ValueError):
        coverage_record(window_id=1, state="clean", reason="never a state")


def test_access_record_has_no_attribution() -> None:
    record = access_record(
        window_id=2,
        path="/tests/har180-control.txt",
        events=["OPEN"],
        is_directory=False,
        category="grader",
        protected_root="/tests",
    )
    assert record["kind"] == "access"
    assert record["events"] == ["OPEN"]
    assert record["is_directory"] is False
    assert "pid" not in record and "command" not in record
    with pytest.raises(ValueError):
        access_record(
            window_id=1,
            path="/x",
            events=["OPEN"],
            is_directory=False,
            category="verifier",
            protected_root="/x",
        )


def test_file_change_record_change_set() -> None:
    for change in ("modified", "created", "deleted", "type_changed", "unavailable"):
        record = file_change_record(
            window_id=1,
            path="/tests/a.txt",
            category="grader",
            change=change,
            before_sha256=None,
            after_sha256=None,
            baseline_ref="evaluator/file-access/baseline-w1.json",
        )
        assert record["change"] == change
    with pytest.raises(ValueError):
        file_change_record(
            window_id=1,
            path="/tests/a.txt",
            category="grader",
            change="edited",
            before_sha256=None,
            after_sha256=None,
            baseline_ref="b",
        )


def test_paths_env_agreed_shape() -> None:
    raw = json.dumps(
        {
            "git_objects": ["/testbed/.git/objects"],
            "git_refs": ["/testbed/.git/refs"],
            "grader": ["/tests", "/grader/custom.txt"],
        }
    )
    assert parse_paths_env(raw) == {
        "git_objects": ["/testbed/.git/objects"],
        "git_refs": ["/testbed/.git/refs"],
        "grader": ["/tests", "/grader/custom.txt"],
    }


def test_paths_env_partial_and_blank() -> None:
    assert parse_paths_env(json.dumps({"grader": ["/tests"]})) == {"grader": ["/tests"]}
    assert parse_paths_env(None) == {}
    assert parse_paths_env("   ") == {}


def test_paths_env_rejects_misconfiguration() -> None:
    with pytest.raises(ValueError):
        parse_paths_env("not json")
    with pytest.raises(ValueError):
        parse_paths_env(json.dumps(["/tests"]))
    with pytest.raises(ValueError):
        parse_paths_env(json.dumps({"verifier": ["/tests"]}))
    with pytest.raises(ValueError):
        parse_paths_env(json.dumps({"grader": ["relative/path"]}))
    with pytest.raises(ValueError):
        parse_paths_env(json.dumps({"grader": ["/tests"], "git_refs": ["/tests"]}))
    with pytest.raises(ValueError):
        parse_paths_env(json.dumps({"grader": ["-evil"]}))
    with pytest.raises(ValueError):
        parse_paths_env(json.dumps({"grader": "/tests"}))


def test_paths_env_normalizes_trailing_slash() -> None:
    assert parse_paths_env(json.dumps({"grader": ["/tests/"]})) == {"grader": ["/tests"]}


def test_classify_longest_prefix_wins() -> None:
    targets = [("/tests", "grader"), ("/testbed/.git/objects", "git_objects")]
    assert classify_path("/tests/har180-control.txt", targets) == ("grader", "/tests")
    assert classify_path("/tests", targets) == ("grader", "/tests")
    assert classify_path("/testbed/.git/objects/ab/cdef", targets) == (
        "git_objects",
        "/testbed/.git/objects",
    )
    assert classify_path("/testbed/.git/refs/heads/main", []) is None
    assert classify_path("/tests-other/a.txt", targets) is None


def test_classify_nested_extras_prefer_specific() -> None:
    targets = [("/tests", "grader"), ("/tests/har180-control.txt", "grader")]
    assert classify_path("/tests/har180-control.txt", targets) == (
        "grader",
        "/tests/har180-control.txt",
    )


def _frame(events: str, path: str) -> bytes:
    return events.encode() + b"\x00" + path.encode() + b"\x00EVALLAB_END\x00"


def test_split_frame_events() -> None:
    assert split_frame_events("OPEN") == (["OPEN"], False)
    assert split_frame_events("OPEN,ISDIR") == (["OPEN"], True)
    assert split_frame_events("ISDIR") is None
    assert split_frame_events("") is None


def test_parse_valid_stream() -> None:
    data = _frame("OPEN", "/tests/a.txt") + _frame("ACCESS", "/tests/b.txt")
    frames, invalid, truncated = parse_frames(data)
    assert frames == [(["OPEN"], False, "/tests/a.txt"), (["ACCESS"], False, "/tests/b.txt")]
    assert invalid == 0 and truncated is False


def test_parse_empty_stream() -> None:
    assert parse_frames(b"") == ([], 0, False)


def test_parse_torn_tail_is_rejected_not_fabricated() -> None:
    data = _frame("OPEN", "/tests/a.txt") + b"OPEN\x00/tests/par"
    frames, invalid, truncated = parse_frames(data)
    assert frames == [(["OPEN"], False, "/tests/a.txt")]
    assert truncated is True
    assert all(path == "/tests/a.txt" for _, _, path in frames)


def test_parse_odd_token_count_rejects_dangling_token() -> None:
    frames, invalid, truncated = parse_frames(b"OPEN\x00")
    assert frames == [] and truncated is True


def test_parse_empty_events_token_is_invalid() -> None:
    frames, invalid, truncated = parse_frames(_frame("", "/tests/a.txt"))
    assert frames == [] and invalid == 1 and truncated is False


def test_parse_loss_tokens_never_become_access_frames() -> None:
    data = (
        _frame("Q_OVERFLOW", "/tests")
        + _frame("IGNORED", "/tests")
        + _frame("OPEN", "/tests/ok.txt")
    )
    frames, invalid, truncated = parse_frames(data)
    assert frames == [(["OPEN"], False, "/tests/ok.txt")]
    assert invalid == 2 and truncated is False


def test_parse_isdir_flag_and_spaced_names() -> None:
    data = _frame("OPEN,ISDIR", "/tests/my dir") + _frame("ACCESS", "/tests/a b.txt")
    frames, invalid, _ = parse_frames(data)
    assert frames == [(["OPEN"], True, "/tests/my dir"), (["ACCESS"], False, "/tests/a b.txt")]
    assert invalid == 0


def test_parse_non_utf8_is_invalid() -> None:
    frames, invalid, _ = parse_frames(b"OPEN\x00/tests/\xff\x00EVALLAB_END\x00")
    assert frames == [] and invalid == 1


def test_stream_chunk_reassembles_split_frames() -> None:
    first = _frame("OPEN", "/tests/a.txt") + b"ACCESS\x00/tests/b"
    frames, invalid, carry = parse_stream_chunk(b"", first)
    assert frames == [(["OPEN"], False, "/tests/a.txt")]
    assert invalid == 0 and carry == b"ACCESS\x00/tests/b"
    frames, invalid, carry = parse_stream_chunk(carry, b".txt\x00EVALLAB_END\x00" + _frame("OPEN", "/t/c"))
    assert [path for _, _, path in frames] == ["/tests/b.txt", "/t/c"]
    assert invalid == 0 and carry == b""


def test_stream_chunk_holds_partial_frame_across_polls() -> None:
    frames, _, carry = parse_stream_chunk(b"", b"OPEN\x00")
    assert frames == [] and carry == b"OPEN\x00"
    frames, invalid, carry = parse_stream_chunk(carry, b"")
    assert frames == [] and invalid == 0 and carry == b"OPEN\x00"


def test_stream_chunk_counts_corrupt_pairs_per_flush() -> None:
    frames, invalid, carry = parse_stream_chunk(b"", _frame("", "/tests/a.txt"))
    assert frames == [] and invalid == 1 and carry == b""
    frames, invalid, carry = parse_stream_chunk(b"", b"")
    assert (frames, invalid, carry) == ([], 0, b"")


def test_scan_loss_tokens() -> None:
    data = _frame("Q_OVERFLOW", "/tests") + _frame("OPEN", "/tests/a.txt")
    assert scan_loss_tokens(data) == {"overflow": True, "ignored": False, "unmount": False}
    assert scan_loss_tokens(_frame("UNMOUNT", "/tests"))["unmount"] is True
    assert scan_loss_tokens(b"") == {"overflow": False, "ignored": False, "unmount": False}


def test_formatter_truncation_cannot_fabricate_a_path_from_the_next_event() -> None:
    data = (
        b"OPEN\x00/tests/truncated"
        + _frame("OPEN", "/tests/next")
        + _frame("ACCESS", "/tests/complete")
    )
    frames, invalid, truncated = parse_frames(data)
    assert frames == [(["ACCESS"], False, "/tests/complete")]
    assert invalid == 1
    assert truncated is False


def _entry(path: str, **overrides: object) -> dict:
    base: dict = {
        "path": path,
        "category": "grader",
        "type": "file",
        "size_bytes": 3,
        "sha256": _sha("old"),
        "hash_status": "complete",
    }
    base.update(overrides)
    return base


def test_diff_detects_all_change_kinds() -> None:
    before = {
        "/tests/mod.txt": _entry("/tests/mod.txt"),
        "/tests/del.txt": _entry("/tests/del.txt"),
        "/tests/type.txt": _entry("/tests/type.txt"),
        "/tests/gone-dark.txt": _entry("/tests/gone-dark.txt"),
        "/tests/same.txt": _entry("/tests/same.txt"),
    }
    after = {
        "/tests/mod.txt": _entry("/tests/mod.txt", sha256=_sha("new"), size_bytes=4),
        "/tests/type.txt": {
            "path": "/tests/type.txt",
            "category": "grader",
            "type": "directory",
        },
        "/tests/gone-dark.txt": _entry(
            "/tests/gone-dark.txt", sha256=None, hash_status="unreadable"
        ),
        "/tests/same.txt": _entry("/tests/same.txt"),
        "/tests/new.txt": _entry("/tests/new.txt", sha256=_sha("new")),
    }
    bodies = {body["path"]: body for body in diff_entries(before, after)}
    assert bodies["/tests/mod.txt"]["change"] == "modified"
    assert bodies["/tests/mod.txt"]["before_sha256"] == _sha("old")
    assert bodies["/tests/del.txt"]["change"] == "deleted"
    assert bodies["/tests/type.txt"]["change"] == "type_changed"
    assert bodies["/tests/gone-dark.txt"]["change"] == "unavailable"
    assert bodies["/tests/new.txt"]["change"] == "created"
    assert bodies["/tests/new.txt"]["before_sha256"] is None
    assert "/tests/same.txt" not in bodies


def test_helper_snapshot_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / "tests"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("control", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("transient", encoding="utf-8")
    code = capture.main(["snapshot", "--root", f"{root}:grader", "--max-bytes", "1024"])
    assert code == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["schema"] == "evallab.file_access_snapshot/v1"
    assert manifest["truncated"] is False
    by_path = {item["path"]: item for item in manifest["entries"]}
    assert by_path[str(root / "a.txt")]["sha256"] == _sha("control")
    assert by_path[str(root / "sub")]["type"] == "directory"


def test_helper_snapshot_bounds_and_absent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / "tests"
    root.mkdir()
    (root / "big.txt").write_text("12345", encoding="utf-8")
    code = capture.main(
        [
            "snapshot", "--root", f"{root}:grader",
            "--shallow-root", "/nope/objects:git_objects",
            "--max-bytes", "2", "--max-files", "2",
        ]
    )
    assert code == 0
    manifest = json.loads(capsys.readouterr().out)
    by_path = {item["path"]: item for item in manifest["entries"]}
    assert by_path[str(root / "big.txt")]["hash_status"] == "size_limit"
    assert manifest["truncated"] is True
    assert {
        "path": "/nope/objects",
        "category": "git_objects",
        "status": "absent",
    } in manifest["roots"]


def test_helper_entry_kinds(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert capture.entry_for(str(target), "grader", 100)["hash_status"] == "complete"
    assert capture.entry_for(str(link), "grader", 100)["type"] == "symlink"
    assert capture.entry_for(str(tmp_path / "missing"), "grader", 100) is None
    with pytest.raises(ValueError):
        capture.split_root_spec("no-colon-here")


def test_helper_probe_reports_schema(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    code = capture.main(["probe", "--candidate", str(tmp_path)])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "evallab.file_access_probe/v1"
    assert report["tests"]["path"] == "/tests"
    assert report["extras"] == [{"path": str(tmp_path), "exists": True, "is_dir": True}]


def test_helper_status_file_records_no_pid(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "chunks").mkdir(parents=True)
    supervisor = capture.CaptureSupervisor(str(out), ["/tests"], True)
    supervisor.stderr_lines.append("Watches established.\n")
    supervisor.write_status("complete", "observer stopped after agent window")
    status = json.loads((out / "capture-status.json").read_text(encoding="utf-8"))
    assert status["schema"] == "evallab.file_access_capture_status/v1"
    assert status["loss_events"] == "explicit" and status["ready"] is False
    assert "pid" not in json.dumps(status).lower()
    assert status["event_bytes"] == 0
