"""Sandbox-helper surfaces of ``evallab.file_access_capture``.

These tests exercise the probe command, snapshot walk bounds, git dir
resolution and every git failure arm on the host: no real ``inotifywait``,
Docker or sandbox. Git fixtures are local temporary repositories; module
constants and collaborators are monkeypatched where external state would
otherwise leak in. The live capture supervisor (``CaptureSupervisor``) is a
separate card and stays out of scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab import file_access_capture as capture


def _git_binary() -> str:
    binary = shutil.which("git")
    if binary is None:
        pytest.skip("git is not installed")
    assert binary is not None
    return binary


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="evallab",
        GIT_AUTHOR_EMAIL="evallab@example.com",
        GIT_COMMITTER_NAME="evallab",
        GIT_COMMITTER_EMAIL="evallab@example.com",
        GIT_AUTHOR_DATE="2020-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2020-01-01T00:00:00+00:00",
    )
    return env


def _git(repo: Path, *argv: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false",
         "-c", "init.templateDir=", *argv],
        cwd=str(repo),
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {' '.join(argv)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _tiny_repo(tmp_path: Path, name: str = "repo", count: int = 2) -> tuple[Path, list[str]]:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    commits = []
    for index in range(count):
        (repo / f"file{index}.txt").write_text(f"content {index}", encoding="utf-8")
        _git(repo, "add", f"file{index}.txt")
        _git(repo, "commit", "-q", "-m", f"file{index}.txt")
        commits.append(_git(repo, "rev-parse", "HEAD"))
    return repo, commits


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_chunk_output_writes_bytes(tmp_path: Path) -> None:
    target = tmp_path / "chunk.bin"
    with capture._chunk_output(str(target)) as output:
        output.write(b"framed")
    assert target.read_bytes() == b"framed"


def test_build_inotifywait_argv_with_and_without_stdbuf() -> None:
    plain = capture.build_inotifywait_argv(
        ["/tests/b", "/tests/a"], ["open", "access"], stdbuf=False
    )
    assert plain[:6] == ["inotifywait", "-m", "-r", "--format", capture.FRAME_FORMAT,
                         "--no-newline"]
    assert plain[6:] == ["-e", "open", "-e", "access", "/tests/a", "/tests/b"]
    prefixed = capture.build_inotifywait_argv(["/tests"], ["open"], stdbuf=True)
    assert prefixed[:3] == ["stdbuf", "-o0", "-e0"]
    assert prefixed[3:] == plain[:6] + ["-e", "open", "/tests"]


def test_stderr_helpers_report_readiness_and_watch_errors() -> None:
    marker = "  " + capture.READINESS_MARKER + "\n"
    assert capture.stderr_ready([marker]) is True
    assert capture.stderr_ready(["Setting up watches."]) is False
    assert capture.stderr_watch_errors(["Setting up watches."]) == []
    lines = [
        "Failed to watch /tests; upper limit on inotify watches reached",
        "Permission denied reading /var/lib/mimo",
        "failed to watch /workspace",
        "x" * 400,
    ]
    errors = capture.stderr_watch_errors(lines)
    assert errors == [
        "Failed to watch /tests; upper limit on inotify watches reached",
        "Permission denied reading /var/lib/mimo",
        "failed to watch /workspace",
    ]
    long_error = capture.stderr_watch_errors(["failed to watch " + "y" * 300])
    assert long_error == [("failed to watch " + "y" * 300)[:200]]
    assert capture.stderr_overflow(["inotify queue overflow"]) is True
    assert capture.stderr_overflow(["normal"]) is False


def test_entry_for_hashes_regular_files_and_caps_size(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"12345")
    hashed = capture.entry_for(str(target), "grader", 1024)
    assert hashed is not None
    assert hashed["type"] == "file"
    assert hashed["size_bytes"] == 5
    assert hashed["sha256"] == hashlib.sha256(b"12345").hexdigest()
    assert hashed["hash_status"] == "complete"
    capped = capture.entry_for(str(target), "grader", 4)
    assert capped is not None
    assert capped["sha256"] is None and capped["hash_status"] == "size_limit"
    assert capture.entry_for(str(tmp_path / "missing"), "grader", 1024) is None


def test_entry_for_marks_unreadable_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "locked.txt"
    target.write_text("secret", encoding="utf-8")
    real_open = open

    def deny(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path) == str(target):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny)
    entry = capture.entry_for(str(target), "grader", 1024)
    assert entry is not None
    assert entry["sha256"] is None and entry["hash_status"] == "unreadable"


def test_entry_for_describes_symlink_directory_and_special(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    described_link = capture.entry_for(str(link), "grader", 1024)
    assert described_link is not None
    assert described_link["type"] == "symlink"
    assert described_link["target"] == str(real)
    directory = tmp_path / "dir"
    directory.mkdir()
    described_dir = capture.entry_for(str(directory), "grader", 1024)
    assert described_dir is not None and described_dir["type"] == "directory"
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    described_fifo = capture.entry_for(str(fifo), "grader", 1024)
    assert described_fifo is not None and described_fifo["type"] == "other"


def test_walk_root_absent_and_non_directory_root(tmp_path: Path) -> None:
    assert capture.walk_root(str(tmp_path / "missing"), "grader", 1024, [10]) == ([], False)
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    budget = [10]
    entries, truncated = capture.walk_root(str(file_path), "grader", 1024, budget)
    assert [entry["type"] for entry in entries] == ["file"]
    assert truncated is False and budget == [9]


def test_walk_root_stops_at_budget(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(4):
        (root / f"f{index}.txt").write_text(str(index), encoding="utf-8")
    entries, truncated = capture.walk_root(str(root), "root", 1024, [1])
    assert [entry["path"] for entry in entries] == [str(root)]
    assert truncated is True
    entries, truncated = capture.walk_root(str(root), "root", 1024, [3])
    assert len(entries) == 3
    assert truncated is True


def test_walk_root_skips_unreadable_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "hidden.txt").write_text("x", encoding="utf-8")
    (root / "kept.txt").write_text("x", encoding="utf-8")
    real_scandir = os.scandir

    def guarded_scandir(path: Any) -> Any:
        if os.path.abspath(os.fspath(path)) == str(locked):
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(capture.os, "scandir", guarded_scandir)
    entries, truncated = capture.walk_root(str(root), "root", 1024, [50])
    paths = {entry["path"] for entry in entries}
    assert paths == {str(root), str(locked), str(root / "kept.txt")}
    assert "hidden.txt" not in json.dumps(entries)
    assert truncated is False


def test_walk_root_skips_child_that_vanished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    gone = root / "gone.txt"
    gone.write_text("x", encoding="utf-8")
    (root / "kept.txt").write_text("x", encoding="utf-8")
    real_entry_for = capture.entry_for

    def vanish_gone(path: str, category: str, max_hash_bytes: int) -> Any:
        if path == str(gone):
            return None
        return real_entry_for(path, category, max_hash_bytes)

    monkeypatch.setattr(capture, "entry_for", vanish_gone)
    entries, truncated = capture.walk_root(str(root), "root", 1024, [50])
    assert {entry["path"] for entry in entries} == {str(root), str(root / "kept.txt")}
    assert truncated is False


def test_walk_root_does_not_follow_symlinked_directories(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.txt").write_text("x", encoding="utf-8")
    (root / "linked").symlink_to(outside)
    entries, truncated = capture.walk_root(str(root), "root", 1024, [50])
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path[str(root / "linked")]["type"] == "symlink"
    assert str(outside / "outside.txt") not in by_path
    assert truncated is False


def test_cmd_snapshot_shallow_and_absent_roots(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = tmp_path / "tests"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    shallow = tmp_path / "shallow"
    shallow.mkdir()
    (shallow / "inner.txt").write_text("y", encoding="utf-8")
    absent = tmp_path / "gone"
    assert capture.main([
        "snapshot",
        "--root", f"{root}:grader",
        "--shallow-root", f"{shallow}:git_objects",
        "--shallow-root", f"{absent}:grader",
        "--max-files", "10",
    ]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["schema"] == capture.SCHEMA_SNAPSHOT
    roots = {entry["path"]: entry for entry in manifest["roots"]}
    assert roots[str(root)]["mode"] == "full" and roots[str(root)]["status"] == "present"
    assert roots[str(shallow)]["mode"] == "shallow"
    assert roots[str(absent)] == {
        "path": str(absent),
        "category": "grader",
        "status": "absent",
    }
    entry_paths = {entry["path"] for entry in manifest["entries"]}
    assert str(shallow) in entry_paths and str(shallow / "inner.txt") not in entry_paths
    assert manifest["truncated"] is False


def test_cmd_snapshot_marks_budget_exhaustion(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = tmp_path / "tests"
    root.mkdir()
    for index in range(3):
        (root / f"f{index}.txt").write_text(str(index), encoding="utf-8")
    assert capture.main(["snapshot", "--root", f"{root}:grader", "--max-files", "2"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["max_files"] == 2
    assert manifest["truncated"] is True


def test_cmd_snapshot_rejects_bad_spec(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        capture.main(["snapshot", "--root", str(tmp_path / "no-category")])


def test_capture_chunk_layer_seals_and_bounds_stream(tmp_path: Path) -> None:
    out = tmp_path / "out"
    supervisor = capture.CaptureSupervisor(
        str(out), ["/tests"], True, seal_bytes=8, max_bytes=10
    )
    assert supervisor.event_names() == ["open", "access", "q_overflow", "ignored", "unmount"]
    assert supervisor._part_path().endswith("c000000.part")
    assert supervisor._sealed_path().endswith("c000000.bin")
    supervisor._append_stream(b"dropped")  # no part is open yet
    assert supervisor.stream_bytes == 0
    os.makedirs(supervisor.chunks_dir)
    supervisor._open_part()
    supervisor._append_stream(b"12345678")
    assert (supervisor.stream_bytes, supervisor._part_bytes) == (8, 8)
    supervisor._append_stream(b"abcd")  # clipped to the remaining room
    assert (supervisor.stream_bytes, supervisor.truncated) == (10, True)
    supervisor._append_stream(b"more")  # no room left: counted, never buffered
    assert supervisor.stream_bytes == 10
    with supervisor._write_lock:
        supervisor._seal_part()
    assert (out / "chunks" / "c000000.bin").read_bytes() == b"12345678ab"
    assert (supervisor.sealed, supervisor._part) == (1, None)
    supervisor._seal_part()  # nothing open: no new chunk, no index change
    assert supervisor._part_index == 1
    supervisor._open_part()
    with supervisor._write_lock:
        supervisor._seal_part()  # an empty part is removed, not sealed
    assert supervisor._part_index == 2 and supervisor.sealed == 1
    assert not (out / "chunks" / "c000001.part").exists()


def test_resolve_git_dir_dotgit_and_absent(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".git").mkdir()
    assert capture.resolve_git_dir(str(plain), None) == (str(plain / ".git"), "dotgit")
    assert capture.resolve_git_dir(str(tmp_path / "missing"), None) == (None, "absent")
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    (bare / "file.txt").write_text("x", encoding="utf-8")
    assert capture.resolve_git_dir(str(bare), None) == (None, "absent")


def test_resolve_git_dir_gitfile_cli_and_pointer_fallback(tmp_path: Path) -> None:
    git_binary = _git_binary()
    repo, _ = _tiny_repo(tmp_path)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree))
    pointer = worktree / ".git"
    assert pointer.is_file()
    resolved, method = capture.resolve_git_dir(str(worktree), git_binary)
    assert method == "git-cli"
    assert resolved is not None and resolved.endswith("/.git/worktrees/wt")
    assert os.path.isdir(resolved)
    assert capture.resolve_git_dir(str(worktree), None) == (str(pointer), "git-pointer")
    missing_git = str(tmp_path / "no-such-git")
    assert capture.resolve_git_dir(str(worktree), missing_git) == (
        str(pointer),
        "git-pointer",
    )


def test_describe_repo_reports_objects_and_refs(tmp_path: Path) -> None:
    git_binary = _git_binary()
    repo, _ = _tiny_repo(tmp_path)
    described = capture.describe_repo(str(repo), git_binary)
    assert described["exists"] is True
    assert described["method"] == "dotgit"
    assert described["git_dir"] == str(repo / ".git")
    assert (described["objects"], described["refs"]) == ("present", "present")
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    bare_report = capture.describe_repo(str(bare), git_binary)
    assert bare_report["method"] == "git-cli"
    assert bare_report["git_dir"] == str(bare)
    plain = tmp_path / "plain"
    plain.mkdir()
    plain_report = capture.describe_repo(str(plain), git_binary)
    assert (plain_report["method"], plain_report["objects"]) == ("absent", "absent")
    assert (plain_report["refs"], plain_report["git_dir"]) == ("absent", None)
    missing = capture.describe_repo(str(tmp_path / "missing"), git_binary)
    assert missing == {
        "candidate": str(tmp_path / "missing"),
        "exists": False,
        "git_dir": None,
        "method": "absent",
    }


def test_run_git_reports_timeout_and_spawn_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert capture._run_git(None, str(tmp_path), ["rev-parse", "HEAD"], 5) == (None, False)
    past = capture.time.monotonic() - 1.0
    assert capture._run_git("git", str(tmp_path), ["rev-parse", "HEAD"], 5, past) == (
        None,
        True,
    )

    def time_out(cmd: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(capture.subprocess, "run", time_out)
    assert capture._run_git("git", str(tmp_path), ["rev-parse", "HEAD"], 5) == (None, True)
    monkeypatch.undo()
    assert capture._run_git(
        str(tmp_path / "no-such-git"), str(tmp_path), ["rev-parse", "HEAD"], 5
    ) == (None, False)


def test_git_head_ignores_unusable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: _proc(0, "  \n"))
    assert capture._git_head("git", "/repo/.git", 5) is None
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: _proc(0, "not-a-hash\n"))
    assert capture._git_head("git", "/repo/.git", 5) is None
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: _proc(0, "A" * 40 + "\n"))
    assert capture._git_head("git", "/repo/.git", 5) == "a" * 40


def test_git_ancestors_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "a" * 40
    parent = "b" * 40
    monkeypatch.setattr(capture, "_run_git", lambda *a, **k: (None, True))
    assert capture._git_ancestors("git", "/repo/.git", base, 5, 10) == ([], "timeout")
    monkeypatch.setattr(capture, "_run_git", lambda *a, **k: (_proc(1), False))
    assert capture._git_ancestors("git", "/repo/.git", base, 5, 10) == ([], "failed")
    monkeypatch.setattr(capture, "_run_git", lambda *a, **k: (_proc(0, "deadbeef\n"), False))
    assert capture._git_ancestors("git", "/repo/.git", base, 5, 10) == ([], "failed")
    monkeypatch.setattr(capture, "_run_git", lambda *a, **k: (_proc(0, parent + "\n"), False))
    assert capture._git_ancestors("git", "/repo/.git", base, 5, 10) == ([], "failed")
    monkeypatch.setattr(
        capture, "_run_git", lambda *a, **k: (_proc(0, f"{base}\n\n{parent}\n"), False)
    )
    assert capture._git_ancestors("git", "/repo/.git", base, 5, 10) == (
        [base, parent],
        "ok",
    )
    assert capture._git_ancestors("git", "/repo/.git", base, 5, 1) == ([base], "capped")


def test_git_is_shallow_with_unreadable_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    git_dir = tmp_path / "repo.git"
    git_dir.mkdir()
    (git_dir / "shallow").write_text("x", encoding="utf-8")
    assert capture._git_is_shallow(str(git_dir), None, 5) is True
    real_getsize = os.path.getsize

    def guarded_getsize(path: Any) -> int:
        if os.fspath(path) == str(git_dir / "shallow"):
            raise OSError("stat failed")
        return real_getsize(path)

    monkeypatch.setattr(capture.os.path, "getsize", guarded_getsize)
    assert capture._git_is_shallow(str(git_dir), None, 5) is None
    clean = tmp_path / "clean.git"
    clean.mkdir()
    monkeypatch.undo()
    monkeypatch.setattr(
        capture.subprocess, "run", lambda *a, **k: _proc(0, "false\n")
    )
    assert capture._git_is_shallow(str(clean), "git", 5) is False
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: _proc(0, "maybe\n"))
    assert capture._git_is_shallow(str(clean), "git", 5) is None


def test_describe_git_history_ancestor_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "c" * 40
    monkeypatch.setattr(capture, "_git_head", lambda *a, **k: base)
    monkeypatch.setattr(capture, "_git_ancestors", lambda *a, **k: ([], "timeout"))
    timed_out = capture.describe_git_history("/repo", "/repo/.git", "git")
    assert (timed_out["reason"], timed_out["complete"]) == ("timeout", False)
    monkeypatch.setattr(capture, "_git_ancestors", lambda *a, **k: ([], "failed"))
    failed = capture.describe_git_history("/repo", "/repo/.git", "git")
    assert (failed["reason"], failed["complete"]) == ("rev-list-failed", False)
    monkeypatch.setattr(capture, "_git_ancestors", lambda *a, **k: ([base], "ok"))
    monkeypatch.setattr(capture, "_git_is_shallow", lambda *a, **k: None)
    unknown = capture.describe_git_history("/repo", "/repo/.git", "git")
    assert (unknown["reason"], unknown["complete"]) == ("timeout", False)
    assert unknown["base_source"] == "pre_agent_head"


def test_describe_git_history_unresolvable_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "_git_head", lambda *a, **k: None)
    entry = capture.describe_git_history("/repo", "/repo/.git", "git", mimo_base="d" * 40)
    assert (entry["base_commit"], entry["base_source"]) == (None, "unknown")
    assert (entry["reason"], entry["complete"]) == ("base-unresolvable", False)


def test_collect_git_history_skips_junk_and_contains_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reports: list[Any] = [
        "junk",
        {},
        {"candidate": "/nowhere"},
        {"candidate": "/repo", "git_dir": str(tmp_path / "gitdir")},
    ]
    real_realpath = os.path.realpath

    def guarded_realpath(path: Any) -> str:
        if os.fspath(path) == str(tmp_path / "gitdir"):
            raise OSError("realpath failed")
        return real_realpath(path)

    def explode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(capture.os.path, "realpath", guarded_realpath)
    monkeypatch.setattr(capture, "describe_git_history", explode)
    history = capture.collect_git_history(
        reports, "git", mimo_base_path=str(tmp_path / "missing")
    )
    assert len(history) == 1
    entry = history[0]
    assert entry["git_dir"] == str(tmp_path / "gitdir")
    assert (entry["reason"], entry["complete"]) == ("rev-list-failed", False)
    assert entry["ancestor_commits"] == []


def _stub_probe_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> tuple[Path, Path]:
    mimo_hidden = tmp_path / "mimo"
    mimo_hidden.mkdir()
    (mimo_hidden / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    mimo_pointer = mimo_hidden / ".git"
    real_describe = capture.describe_repo

    def scripted_describe(candidate: str, git_binary: str | None) -> dict[str, Any]:
        if candidate == str(mimo_hidden):
            return {"candidate": candidate, "exists": True, "git_dir": None,
                    "method": "absent", "objects": "absent", "refs": "absent"}
        if candidate == str(mimo_pointer):
            return {"candidate": candidate, "exists": True,
                    "git_dir": str(repo / ".git"), "method": "dotgit",
                    "objects": "present", "refs": "present"}
        return real_describe(candidate, git_binary)

    monkeypatch.setattr(capture, "describe_repo", scripted_describe)
    monkeypatch.setattr(capture, "DISCOVERY_CANDIDATES", (str(repo), str(repo) + "/."))
    monkeypatch.setattr(capture, "MIMO_GIT_HIDDEN", str(mimo_hidden))
    monkeypatch.setattr(capture, "MIMO_BASE_FILE", str(tmp_path / "missing-base"))
    monkeypatch.setattr(capture.os, "getcwd", lambda: str(tmp_path / "cwd-free"))
    monkeypatch.setattr(
        capture.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None
    )
    return mimo_hidden, mimo_pointer


def test_main_probe_uses_injected_constants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repo, _ = _tiny_repo(tmp_path)
    mimo_hidden, _ = _stub_probe_seams(monkeypatch, tmp_path, repo)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    monkeypatch.setattr(capture, "TESTS_DIR", str(tests_dir))
    extra_dir = tmp_path / "extra-dir"
    extra_dir.mkdir()
    extra_file = tmp_path / "extra.txt"
    extra_file.write_text("x", encoding="utf-8")
    assert capture.main([
        "probe",
        "--candidate", str(extra_dir),
        "--candidate", str(extra_file),
        "--candidate", str(tmp_path / "missing-extra"),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == capture.SCHEMA_PROBE
    assert report["inotifywait"] is None
    assert report["git"] == "/usr/bin/git"
    assert [entry["candidate"] for entry in report["repos"]] == [
        str(repo),
        str(tmp_path / "cwd-free"),
    ]
    assert report["repos"][1]["exists"] is False
    mimo = report["mimo_git_hidden"]
    assert mimo["candidate"] == str(mimo_hidden)
    assert mimo["git_dir"] == str(repo / ".git")
    assert report["git_history"][0]["git_dir"] == str(repo / ".git")
    assert report["tests"] == {"path": str(tests_dir), "status": "present"}
    assert report["extras"] == [
        {"path": str(extra_dir), "exists": True, "is_dir": True},
        {"path": str(extra_file), "exists": True, "is_dir": False},
        {"path": str(tmp_path / "missing-extra"), "exists": False, "is_dir": False},
    ]


def test_main_probe_tests_dir_states(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    not_a_directory = tmp_path / "tests-file"
    not_a_directory.write_text("x", encoding="utf-8")
    monkeypatch.setattr(capture, "DISCOVERY_CANDIDATES", ())
    monkeypatch.setattr(capture, "MIMO_GIT_HIDDEN", str(tmp_path / "no-mimo"))
    monkeypatch.setattr(capture, "MIMO_BASE_FILE", str(tmp_path / "missing-base"))
    monkeypatch.setattr(capture.os, "getcwd", lambda: str(tmp_path / "cwd-free"))
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    states = [
        (str(not_a_directory), "not-a-directory"),
        (str(tmp_path / "absent-tests"), "absent"),
    ]
    for path, expected in states:
        monkeypatch.setattr(capture, "TESTS_DIR", path)
        assert capture.main(["probe"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["tests"] == {"path": path, "status": expected}
        assert [entry["candidate"] for entry in report["repos"]] == [
            str(tmp_path / "cwd-free")
        ]
        assert report["mimo_git_hidden"]["git_dir"] is None
        assert report["git_history"] == []


def test_main_probe_survives_history_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def explode(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("history is not fatal")

    monkeypatch.setattr(capture, "DISCOVERY_CANDIDATES", ())
    monkeypatch.setattr(capture, "MIMO_GIT_HIDDEN", str(tmp_path / "no-mimo"))
    monkeypatch.setattr(capture, "MIMO_BASE_FILE", str(tmp_path / "missing-base"))
    monkeypatch.setattr(capture, "TESTS_DIR", str(tmp_path / "tests"))
    monkeypatch.setattr(capture.os, "getcwd", lambda: str(tmp_path / "cwd-free"))
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    monkeypatch.setattr(capture, "collect_git_history", explode)
    assert capture.main(["probe"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["git_history"] == []
    assert report["cwd"] == str(tmp_path / "cwd-free")
