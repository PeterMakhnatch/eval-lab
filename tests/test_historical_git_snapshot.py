from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import evallab.storage.data_backfill as data_backfill
import evallab.storage.historical_git_snapshot as git_snapshot
from evallab.storage.data_backfill import (
    HISTORICAL_CONTRACT_FILENAME,
    HistoricalContractSetVerificationError,
    HistoricalRegenerationConflict,
    HistoricalRegenerationExpectationMismatch,
    run_historical_contract_regeneration,
    verify_historical_contract_set,
)
from evallab.storage.historical_git_snapshot import (
    HistoricalSnapshotInvalid,
    HistoricalSnapshotUnavailable,
    capture_historical_source_snapshot,
    reopen_historical_source_snapshot,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class _BoundedReadSpy(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_sizes: list[int] = []
        self.readline_sizes: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("streaming reader attempted an unbounded payload read")
        self.read_sizes.append(size)
        return super().read(size)

    def readline(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("streaming reader attempted an unbounded header read")
        self.readline_sizes.append(size)
        return super().readline(size)


class _FakeBatchProcess:
    def __init__(self, output: bytes, *, returncode: int = 0) -> None:
        self.stdin = io.BytesIO()
        self.stdout = _BoundedReadSpy(output)
        self._returncode = returncode
        self._finished = False

    def wait(self, timeout: float) -> int:
        del timeout
        self._finished = True
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._finished else None

    def terminate(self) -> None:
        self._finished = True

    def kill(self) -> None:
        self._finished = True


class _FakeDeadlineReader:
    def __init__(self, stream: _BoundedReadSpy) -> None:
        self.stream = stream

    def read_line(self, limit: int) -> bytes:
        line = self.stream.readline(limit + 1)
        if not line.endswith(b"\n"):
            raise HistoricalSnapshotUnavailable("truncated or oversized batch header")
        return line[:-1]

    def read_payload(self, maximum: int) -> bytes:
        return self.stream.read(maximum)

    def read_one(self, phase: str) -> bytes:
        del phase
        return self.stream.read(1)

    def expect_eof(self) -> None:
        if self.stream.read(1):
            raise HistoricalSnapshotUnavailable("unexpected trailing batch output")

    def remaining_seconds(self, phase: str) -> float:
        del phase
        return 1.0

    def close(self) -> None:
        pass


def _install_fake_batch(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeBatchProcess,
) -> None:
    monkeypatch.setattr(
        git_snapshot,
        "_start_cat_file_batch",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        git_snapshot,
        "_open_cat_file_deadline_reader",
        lambda stream, deadline: _FakeDeadlineReader(stream),
    )


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_trial(root: Path, locator: str = "job/trial", *, truth: bool = True) -> Path:
    trial = root / "runs" / locator
    (trial / "artifacts/app/output").mkdir(parents=True, exist_ok=True)
    (trial / "verifier").mkdir(parents=True, exist_ok=True)
    (trial / "lock.json").write_text(
        json.dumps({"task": {"digest": DIGEST_A, "name": "name-is-not-authority"}}),
        encoding="utf-8",
    )
    verifier: dict[str, object] = {"reward": 1.0}
    if truth:
        verifier["truth_digest"] = DIGEST_A
    (trial / "verifier/result.json").write_text(json.dumps(verifier), encoding="utf-8")
    (trial / "result.json").write_text("{}", encoding="utf-8")
    (trial / "artifacts/manifest.json").write_text("[]", encoding="utf-8")
    (trial / "artifacts/app/output/benchmark-events.jsonl").write_text(
        '{"event_type":"complete"}\n',
        encoding="utf-8",
    )
    (trial / "artifacts/app/output/final-state.json").write_text(
        json.dumps({"final_digest": DIGEST_A}),
        encoding="utf-8",
    )
    return trial


def _init_repo(tmp_path: Path, *, trials: int = 1, truth: bool = True) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Historical Test")
    _git(repo, "config", "user.email", "historical@example.test")
    for index in range(trials):
        locator = "job/trial" if index == 0 else f"job-{index}/trial-{index}"
        _write_trial(repo, locator, truth=truth)
    _git(repo, "add", "--", "runs")
    _git(repo, "commit", "-qm", "source")
    return repo, _git(repo, "rev-parse", "HEAD")


def _commit_nested_runs(repo: Path) -> str:
    nested = repo / "nested"
    nested.mkdir()
    (repo / "runs").replace(nested / "runs")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "nest runs root")
    return _git(repo, "rev-parse", "HEAD")


def _open_fd_count() -> int:
    descriptor_root = Path("/dev/fd")
    if not descriptor_root.is_dir():
        descriptor_root = Path("/proc/self/fd")
    return len(os.listdir(descriptor_root))


def _capture_plan(
    repo: Path,
    revision: str,
    runs_root: Path = Path("runs"),
):
    capture = capture_historical_source_snapshot(
        repo_root=repo,
        runs_root=runs_root,
        source_revision=revision,
    )
    plan = data_backfill._plan_historical_contract_regeneration(
        capture,
        destination_runs_root=repo / runs_root,
    )
    return capture, plan


def _run(
    repo: Path,
    revision: str,
    *,
    runs_root: Path = Path("runs"),
    apply: bool = False,
    manifest_name: str = "historical-plan.json",
    promoted: int = 1,
    derivable: int = 1,
):
    capture, plan = _capture_plan(repo, revision, runs_root)
    return run_historical_contract_regeneration(
        repo_root=repo,
        runs_root=runs_root,
        source_revision=revision,
        manifest_out=repo / manifest_name,
        expect_promoted=promoted,
        expect_derivable=derivable,
        expect_source_snapshot=(capture.snapshot.snapshot_digest if apply else None),
        expect_plan_digest=(plan.manifest.content_digest if apply else None),
        apply=apply,
    )


def test_public_dry_run_and_apply_require_explicit_git_snapshot(tmp_path: Path) -> None:
    repo, revision = _init_repo(tmp_path)
    dry = _run(repo, revision)
    applied = _run(repo, revision, apply=True)
    output = repo / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME

    assert dry.resolved_commit == revision
    assert dry.manifest.schema_version == "historical-contract-regeneration/v2"
    assert dry.manifest.source_snapshot.authority == "git-selected-blobs"
    assert applied.created_output_count == 1
    assert json.loads(output.read_text())["schema_version"] == (
        "historical-descriptive-contract/v2"
    )


def test_same_selected_blobs_across_commits_are_byte_identical(tmp_path: Path) -> None:
    repo, first_revision = _init_repo(tmp_path)
    first_capture, first_plan = _capture_plan(repo, first_revision)
    (repo / "UNRELATED.txt").write_text("not selected", encoding="utf-8")
    _git(repo, "add", "UNRELATED.txt")
    _git(repo, "commit", "-qm", "unrelated")
    second_revision = _git(repo, "rev-parse", "HEAD")
    second_capture, second_plan = _capture_plan(repo, second_revision)

    assert first_revision != second_revision
    assert first_capture.snapshot == second_capture.snapshot
    assert first_plan.manifest_bytes == second_plan.manifest_bytes
    assert first_plan.outputs[0][1] == second_plan.outputs[0][1]


@pytest.mark.parametrize(
    "mutation",
    [
        "content",
        "path",
        "mode",
        "add-artifact",
        "remove-artifact",
        "add-marker",
        "remove-marker",
    ],
)
def test_every_selected_membership_or_identity_mutation_changes_snapshot(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo, first_revision = _init_repo(tmp_path)
    first = capture_historical_source_snapshot(
        repo_root=repo,
        runs_root=Path("runs"),
        source_revision=first_revision,
    )
    trial = repo / "runs/job/trial"
    if mutation == "content":
        (trial / "lock.json").write_text(json.dumps({"task": {"digest": DIGEST_B}}))
    elif mutation == "path":
        (trial / "result.json").rename(trial / "renamed-result.json")
    elif mutation == "mode":
        os.chmod(trial / "result.json", 0o755)
    elif mutation == "add-artifact":
        (trial / "new-artifact.bin").write_bytes(b"new")
    elif mutation == "remove-artifact":
        (trial / "artifacts/app/output/final-state.json").unlink()
    elif mutation == "add-marker":
        _write_trial(repo, "new-job/new-trial")
    else:
        (trial / "artifacts/manifest.json").unlink()
    _git(repo, "add", "-A", "--", "runs")
    _git(repo, "commit", "-qm", mutation)
    second_revision = _git(repo, "rev-parse", "HEAD")
    second = capture_historical_source_snapshot(
        repo_root=repo,
        runs_root=Path("runs"),
        source_revision=second_revision,
    )

    assert first.snapshot.snapshot_digest != second.snapshot.snapshot_digest


def test_tracked_generated_contract_is_excluded_from_snapshot_identity(tmp_path: Path) -> None:
    repo, first_revision = _init_repo(tmp_path)
    first_capture, first_plan = _capture_plan(repo, first_revision)
    generated = repo / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME
    generated.write_bytes(b"tracked-old-generated-output\n")
    _git(repo, "add", "--", str(generated.relative_to(repo)))
    _git(repo, "commit", "-qm", "tracked generated output")
    second_revision = _git(repo, "rev-parse", "HEAD")
    second_capture, second_plan = _capture_plan(repo, second_revision)

    assert first_capture.snapshot == second_capture.snapshot
    assert first_plan.manifest_bytes == second_plan.manifest_bytes


def test_selected_symlink_refuses_before_manifest_write(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path)
    lock = repo / "runs/job/trial/lock.json"
    lock.unlink()
    lock.symlink_to("result.json")
    _git(repo, "add", "-A", "--", "runs")
    _git(repo, "commit", "-qm", "symlink source")
    revision = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(HistoricalSnapshotInvalid):
        run_historical_contract_regeneration(
            repo_root=repo,
            runs_root=Path("runs"),
            source_revision=revision,
            manifest_out=repo / "refused.json",
            expect_promoted=1,
            expect_derivable=1,
        )
    assert not (repo / "refused.json").exists()


def test_selected_gitlink_refuses_before_manifest_write(tmp_path: Path) -> None:
    repo, revision = _init_repo(tmp_path)
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{revision},runs/job/trial/gitlink",
    )
    _git(repo, "commit", "-qm", "gitlink source")
    gitlink_revision = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(HistoricalSnapshotInvalid):
        _run(repo, gitlink_revision)
    assert not (repo / "historical-plan.json").exists()


def test_non_utf8_and_duplicate_tree_paths_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(b"100644 blob " + b"a" * 40 + b" 1\truns/job/trial/bad-\xff\x00"),
        stderr=b"",
    )
    monkeypatch.setattr(git_snapshot, "_run_git", lambda *args, **kwargs: completed)
    with pytest.raises(HistoricalSnapshotInvalid):
        git_snapshot._enumerate_tree(Path("."), "a" * 40, "runs")

    duplicate = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            b"100644 blob " + b"a" * 40 + b" 1\truns/job/trial/file\x00"
            b"100644 blob " + b"b" * 40 + b" 1\truns/job/trial/file\x00"
        ),
        stderr=b"",
    )
    monkeypatch.setattr(git_snapshot, "_run_git", lambda *args, **kwargs: duplicate)
    with pytest.raises(HistoricalSnapshotInvalid):
        git_snapshot._enumerate_tree(Path("."), "a" * 40, "runs")


@pytest.mark.parametrize(
    "response",
    [
        b"b" * 40 + b" blob 1\nX\n",
        b"a" * 40 + b" tree 1\nX\n",
        b"a" * 40 + b" blob 2\nX\n",
        b"a" * 40 + b" missing\n",
        b"a" * 40 + b" blob 1\nX!",
        b"a" * 40 + b" blob 1\nX\ntrailing",
        b"a" * 40 + b" blob 1",
    ],
)
def test_streamed_object_oid_type_size_delimiter_and_trailing_corruption_refuses(
    monkeypatch: pytest.MonkeyPatch,
    response: bytes,
) -> None:
    process = _FakeBatchProcess(response)
    _install_fake_batch(monkeypatch, process)
    with pytest.raises(HistoricalSnapshotUnavailable):
        git_snapshot._stream_blob_batch(
            Path("."),
            {"a" * 40: 1},
            retain_oids=frozenset(),
            on_blob=lambda authenticated: None,
        )


def test_streamed_cat_file_subprocess_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oid = "a" * 40
    process = _FakeBatchProcess(f"{oid} blob 1\n".encode() + b"X\n", returncode=7)

    def failing_process(*args, **kwargs):
        args[1].write(b"injected cat-file failure")
        return process

    _install_fake_batch(monkeypatch, process)
    monkeypatch.setattr(git_snapshot, "_start_cat_file_batch", failing_process)
    with pytest.raises(HistoricalSnapshotUnavailable, match="injected cat-file failure"):
        git_snapshot._stream_blob_batch(
            Path("."),
            {oid: 1},
            retain_oids=frozenset(),
            on_blob=lambda authenticated: None,
        )


def test_stream_reader_bounds_payload_reads_and_retains_only_requested_oid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_oid = "a" * 40
    second_oid = "b" * 40
    first_payload = b"A" * (git_snapshot._BLOB_READ_CHUNK_SIZE * 2 + 17)
    second_payload = b"B" * (git_snapshot._BLOB_READ_CHUNK_SIZE + 9)
    response = (
        f"{first_oid} blob {len(first_payload)}\n".encode()
        + first_payload
        + b"\n"
        + f"{second_oid} blob {len(second_payload)}\n".encode()
        + second_payload
        + b"\n"
    )
    process = _FakeBatchProcess(response)
    _install_fake_batch(monkeypatch, process)
    authenticated = []

    git_snapshot._stream_blob_batch(
        Path("."),
        {first_oid: len(first_payload), second_oid: len(second_payload)},
        retain_oids=frozenset({second_oid}),
        on_blob=authenticated.append,
    )

    assert [row.git_oid for row in authenticated] == [first_oid, second_oid]
    assert authenticated[0].retained_content is None
    assert authenticated[1].retained_content == second_payload
    assert process.stdout.read_sizes
    assert min(process.stdout.read_sizes) >= 0
    assert max(process.stdout.read_sizes) <= git_snapshot._BLOB_READ_CHUNK_SIZE
    assert process.stdout.readline_sizes == [git_snapshot._MAX_BATCH_HEADER_BYTES + 1] * 2


@pytest.mark.parametrize(
    ("stall", "expected_size"),
    [
        ("no-header", 1),
        ("partial-header", 1),
        ("partial-payload", 8),
        ("missing-delimiter", 1),
        ("missing-eof", 1),
    ],
)
def test_real_cat_file_stalls_timeout_and_child_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
    stall: str,
    expected_size: int,
) -> None:
    child_script = """
import signal
import sys
import time

mode = sys.argv[1]
oid = sys.stdin.buffer.readline().strip()
if mode == "missing-eof":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "partial-header":
    sys.stdout.buffer.write(oid[:8])
elif mode == "partial-payload":
    sys.stdout.buffer.write(oid + b" blob 8\\nabc")
elif mode == "missing-delimiter":
    sys.stdout.buffer.write(oid + b" blob 1\\nX")
elif mode == "missing-eof":
    sys.stdout.buffer.write(oid + b" blob 1\\nX\\n")
sys.stdout.buffer.flush()
time.sleep(60)
"""
    children: list[subprocess.Popen[bytes]] = []

    def start_stalled_child(repository: Path, stderr):
        del repository
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", child_script, stall],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            bufsize=0,
        )
        children.append(child)
        return child

    monkeypatch.setattr(git_snapshot, "_start_cat_file_batch", start_stalled_child)
    monkeypatch.setattr(git_snapshot, "_CAT_FILE_IO_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(git_snapshot, "_CAT_FILE_TERMINATE_GRACE_SECONDS", 0.15)
    started = time.monotonic()

    with pytest.raises(HistoricalSnapshotUnavailable, match="timed out"):
        git_snapshot._stream_blob_batch(
            Path("."),
            {"a" * 40: expected_size},
            retain_oids=frozenset(),
            on_blob=lambda authenticated: None,
        )

    assert time.monotonic() - started < 2.0
    assert len(children) == 1
    child = children[0]
    assert child.poll() is not None
    assert child.returncode in {-signal.SIGTERM, -signal.SIGKILL}
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)


def test_capture_discards_large_inventory_blobs_and_reopen_retains_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = _init_repo(tmp_path)
    trial = repo / "runs/job/trial"
    lock_bytes = (trial / "lock.json").read_bytes()
    (trial / "result.json").write_bytes(lock_bytes)
    (trial / "artifacts/large-a.bin").write_bytes(
        b"A" * (git_snapshot._BLOB_READ_CHUNK_SIZE * 2 + 31)
    )
    (trial / "artifacts/large-b.bin").write_bytes(
        b"B" * (git_snapshot._BLOB_READ_CHUNK_SIZE * 3 + 7)
    )
    _git(repo, "add", "--", "runs")
    _git(repo, "commit", "-qm", "large repeated selected blobs")
    revision = _git(repo, "rev-parse", "HEAD")
    original_stream = git_snapshot._stream_blob_batch
    retention_requests: list[frozenset[str]] = []
    retained_results: list[list[bool]] = []

    def observe_stream(
        repository: Path,
        expected_sizes,
        *,
        retain_oids,
        on_blob,
    ):
        retention_requests.append(frozenset(retain_oids))
        retained_results.append([])

        def observe_blob(authenticated):
            retained_results[-1].append(authenticated.retained_content is not None)
            on_blob(authenticated)

        return original_stream(
            repository,
            expected_sizes,
            retain_oids=retain_oids,
            on_blob=observe_blob,
        )

    monkeypatch.setattr(git_snapshot, "_stream_blob_batch", observe_stream)
    capture = capture_historical_source_snapshot(
        repo_root=repo,
        runs_root=Path("runs"),
        source_revision=revision,
    )
    reopen_historical_source_snapshot(repo_root=repo, snapshot=capture.snapshot)

    assert set(capture.document_bytes) == {
        "runs/job/trial/lock.json",
        "runs/job/trial/verifier/result.json",
    }
    assert retention_requests[0]
    assert retention_requests[1] == frozenset()
    assert any(retained_results[0])
    assert not any(retained_results[1])
    blobs_by_path = {blob.path: blob for blob in capture.snapshot.blobs}
    assert (
        blobs_by_path["runs/job/trial/lock.json"].git_oid
        == blobs_by_path["runs/job/trial/result.json"].git_oid
    )
    assert (
        blobs_by_path["runs/job/trial/lock.json"].sha256_digest
        == blobs_by_path["runs/job/trial/result.json"].sha256_digest
    )


def test_worktree_mutations_before_during_and_after_are_not_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision = _init_repo(tmp_path)
    clean_capture, clean_plan = _capture_plan(repo, revision)
    verifier = repo / "runs/job/trial/verifier/result.json"
    original_batch = git_snapshot._stream_blob_batch

    def mutate_during(repository: Path, expected_sizes, **kwargs):
        (repo / "runs/job/trial/lock.json").write_text("{}", encoding="utf-8")
        return original_batch(repository, expected_sizes, **kwargs)

    monkeypatch.setattr(git_snapshot, "_stream_blob_batch", mutate_during)
    dry = _run(repo, revision)
    verifier.write_text("not-json-after", encoding="utf-8")
    applied = _run(repo, revision, apply=True)
    output = repo / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME

    assert dry.manifest.source_snapshot == clean_capture.snapshot
    assert dry.manifest_bytes == clean_plan.manifest_bytes
    assert applied.manifest_bytes == clean_plan.manifest_bytes
    assert output.read_bytes() == clean_plan.outputs[0][1]


def test_ref_movement_after_resolution_cannot_redirect_blob_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision = _init_repo(tmp_path)
    expected = capture_historical_source_snapshot(
        repo_root=repo,
        runs_root=Path("runs"),
        source_revision=revision,
    )
    _git(repo, "branch", "moving", revision)
    original_enumerate = git_snapshot._enumerate_tree
    moved = False

    def move_then_enumerate(repository: Path, resolved: str, runs_root: str):
        nonlocal moved
        if not moved:
            moved = True
            (repo / "runs/job/trial/lock.json").write_text(
                json.dumps({"task": {"digest": DIGEST_B}}), encoding="utf-8"
            )
            _git(repo, "add", "--", "runs/job/trial/lock.json")
            _git(repo, "commit", "-qm", "move ref")
            _git(repo, "branch", "-f", "moving", "HEAD")
        return original_enumerate(repository, resolved, runs_root)

    monkeypatch.setattr(git_snapshot, "_enumerate_tree", move_then_enumerate)
    actual = capture_historical_source_snapshot(
        repo_root=repo,
        runs_root=Path("runs"),
        source_revision="moving",
    )

    assert actual.resolved_commit == revision
    assert actual.snapshot == expected.snapshot


def test_missing_object_is_typed_unavailable_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision = _init_repo(tmp_path)

    def unavailable(*args, **kwargs):
        raise HistoricalSnapshotUnavailable("missing selected object")

    monkeypatch.setattr(git_snapshot, "_stream_blob_batch", unavailable)
    with pytest.raises(HistoricalSnapshotUnavailable):
        _run(repo, revision)
    assert not (repo / "historical-plan.json").exists()


def test_snapshot_and_plan_reopen_in_separate_clean_clone(tmp_path: Path) -> None:
    repo, revision = _init_repo(tmp_path)
    capture, plan = _capture_plan(repo, revision)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(repo), str(clone)],
        check=True,
        capture_output=True,
    )
    reopened = capture_historical_source_snapshot(
        repo_root=clone,
        runs_root=Path("runs"),
        source_revision=revision,
    )
    reopened_plan = data_backfill._plan_historical_contract_regeneration(
        reopened,
        destination_runs_root=clone / "runs",
    )
    reopen_historical_source_snapshot(repo_root=clone, snapshot=capture.snapshot)

    assert reopened.snapshot == capture.snapshot
    assert reopened_plan.manifest_bytes == plan.manifest_bytes


def test_reopen_refuses_sha256_mismatch_even_with_self_consistent_snapshot_digest(
    tmp_path: Path,
) -> None:
    repo, revision = _init_repo(tmp_path)
    capture, _ = _capture_plan(repo, revision)
    body = capture.snapshot.model_dump(mode="json", exclude={"snapshot_digest"})
    body["blobs"][0]["sha256_digest"] = "sha256:" + "0" * 64
    forged = type(capture.snapshot).model_validate(
        {
            **body,
            "snapshot_digest": git_snapshot._domain_json_digest(
                git_snapshot.HISTORICAL_SOURCE_SNAPSHOT_DOMAIN,
                body,
            ),
        }
    )

    with pytest.raises(HistoricalSnapshotUnavailable):
        reopen_historical_source_snapshot(repo_root=repo, snapshot=forged)


def test_apply_requires_reviewed_digests_and_refuses_mismatch_before_preflight(
    tmp_path: Path,
) -> None:
    repo, revision = _init_repo(tmp_path)
    with pytest.raises(HistoricalRegenerationExpectationMismatch):
        run_historical_contract_regeneration(
            repo_root=repo,
            runs_root=Path("runs"),
            source_revision=revision,
            manifest_out=repo / "plan.json",
            expect_promoted=1,
            expect_derivable=1,
            apply=True,
        )
    capture, plan = _capture_plan(repo, revision)
    with pytest.raises(HistoricalRegenerationExpectationMismatch):
        run_historical_contract_regeneration(
            repo_root=repo,
            runs_root=Path("runs"),
            source_revision=revision,
            manifest_out=repo / "plan.json",
            expect_promoted=1,
            expect_derivable=1,
            expect_source_snapshot=capture.snapshot.snapshot_digest,
            expect_plan_digest="sha256:" + "0" * 64,
            apply=True,
        )
    with pytest.raises(HistoricalRegenerationExpectationMismatch):
        run_historical_contract_regeneration(
            repo_root=repo,
            runs_root=Path("runs"),
            source_revision=revision,
            manifest_out=repo / "plan.json",
            expect_promoted=1,
            expect_derivable=1,
            expect_source_snapshot="sha256:" + "0" * 64,
            expect_plan_digest=plan.manifest.content_digest,
            apply=True,
        )
    assert plan.outputs
    assert not (repo / "plan.json").exists()


def test_changed_snapshot_apply_preserves_old_output_and_refuses(tmp_path: Path) -> None:
    repo, first_revision = _init_repo(tmp_path)
    _run(repo, first_revision, apply=True)
    output = repo / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME
    original = output.read_bytes()
    (repo / "runs/job/trial/lock.json").write_text(
        json.dumps({"task": {"digest": DIGEST_B}}), encoding="utf-8"
    )
    _git(repo, "add", "--", "runs/job/trial/lock.json")
    _git(repo, "commit", "-qm", "changed selected source")
    second_revision = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(HistoricalRegenerationConflict):
        _run(repo, second_revision, apply=True, manifest_name="second-plan.json")
    assert output.read_bytes() == original
    assert not (repo / "second-plan.json").exists()


def test_partial_publication_rerun_converges_without_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision = _init_repo(tmp_path, trials=2)
    original_publish = data_backfill._atomic_create_or_verify_historical_anchored
    output_calls = 0

    def fail_second_output(anchor, relative_path: Path, content: bytes) -> bool:
        nonlocal output_calls
        output_calls += 1
        if output_calls == 2:
            raise HistoricalRegenerationConflict("injected second-output refusal")
        return original_publish(anchor, relative_path, content)

    monkeypatch.setattr(
        data_backfill,
        "_atomic_create_or_verify_historical_anchored",
        fail_second_output,
    )
    with pytest.raises(HistoricalRegenerationConflict):
        _run(repo, revision, apply=True, promoted=2, derivable=2)
    existing = list((repo / "runs").glob("**/historical-contract.json"))
    assert len(existing) == 1
    assert not (repo / "historical-plan.json").exists()

    monkeypatch.setattr(
        data_backfill,
        "_atomic_create_or_verify_historical_anchored",
        original_publish,
    )
    result = _run(repo, revision, apply=True, promoted=2, derivable=2)
    assert result.created_output_count == 1
    assert result.verified_output_count == 1


def _verified_world(tmp_path: Path):
    repo, revision = _init_repo(tmp_path)
    result = _run(repo, revision, apply=True)
    manifest_path = repo / "historical-plan.json"
    output = repo / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME
    return repo, result, manifest_path, output


def test_shared_verifier_authenticates_complete_contract_set(tmp_path: Path) -> None:
    repo, result, manifest_path, _ = _verified_world(tmp_path)
    verified = verify_historical_contract_set(
        repo_root=repo,
        runs_root=Path("runs"),
        manifest_bytes=manifest_path.read_bytes(),
        expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
        expected_plan_digest=result.manifest.content_digest,
    )
    assert verified == result.manifest


def test_verifier_refuses_static_intermediate_runs_symlink_without_external_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = _init_repo(tmp_path)
    revision = _commit_nested_runs(repo)
    result = _run(
        repo,
        revision,
        runs_root=Path("nested/runs"),
        apply=True,
    )
    manifest_bytes = (repo / "historical-plan.json").read_bytes()
    external_nested = tmp_path / "external-nested"
    (repo / "nested").replace(external_nested)
    (repo / "nested").symlink_to(external_nested, target_is_directory=True)
    before_fds = _open_fd_count()

    def forbid_external_read(*args, **kwargs):
        raise AssertionError("external historical output was read")

    monkeypatch.setattr(
        data_backfill,
        "_read_existing_historical_target",
        forbid_external_read,
    )
    with pytest.raises(HistoricalRegenerationConflict, match="symlinked"):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("nested/runs"),
            manifest_bytes=manifest_bytes,
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )

    assert _open_fd_count() == before_fds


@pytest.mark.parametrize("apply", [False, True])
def test_preflight_and_apply_refuse_intermediate_symlink_without_external_output(
    tmp_path: Path,
    apply: bool,
) -> None:
    repo, _ = _init_repo(tmp_path)
    revision = _commit_nested_runs(repo)
    external_nested = tmp_path / "external-apply-nested"
    (repo / "nested").replace(external_nested)
    (repo / "nested").symlink_to(external_nested, target_is_directory=True)
    before_fds = _open_fd_count()

    with pytest.raises(HistoricalRegenerationConflict, match="symlinked"):
        _run(
            repo,
            revision,
            runs_root=Path("nested/runs"),
            apply=apply,
        )

    assert not (
        external_nested / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME
    ).exists()
    assert not (repo / "historical-plan.json").exists()
    assert _open_fd_count() == before_fds


def test_intermediate_component_replacement_during_traversal_refuses_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = _init_repo(tmp_path)
    revision = _commit_nested_runs(repo)
    moved_nested = tmp_path / "moved-during-traversal"
    replaced = False
    before_fds = _open_fd_count()

    def replace_after_stat(stage: str, component: str) -> None:
        nonlocal replaced
        if stage == "initial_runs_root" and component == "nested" and not replaced:
            replaced = True
            (repo / "nested").replace(moved_nested)
            (repo / "nested").mkdir()

    monkeypatch.setattr(
        data_backfill,
        "_historical_anchor_boundary",
        replace_after_stat,
    )
    with pytest.raises(HistoricalRegenerationConflict, match="changed during traversal"):
        _run(repo, revision, runs_root=Path("nested/runs"), apply=True)

    assert replaced
    assert not (moved_nested / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME).exists()
    assert not (repo / "historical-plan.json").exists()
    assert _open_fd_count() == before_fds


def test_final_runs_inode_recheck_refuses_replacement_and_closes_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = _init_repo(tmp_path)
    revision = _commit_nested_runs(repo)
    result = _run(
        repo,
        revision,
        runs_root=Path("nested/runs"),
        apply=True,
    )
    manifest_bytes = (repo / "historical-plan.json").read_bytes()
    moved_runs = tmp_path / "moved-before-verifier-return"
    replaced = False
    before_fds = _open_fd_count()

    def replace_before_recheck(stage: str, component: str) -> None:
        nonlocal replaced
        if stage == "before_final_recheck" and not replaced:
            replaced = True
            (repo / "nested/runs").replace(moved_runs)
            (repo / "nested/runs").mkdir()

    monkeypatch.setattr(
        data_backfill,
        "_historical_anchor_boundary",
        replace_before_recheck,
    )
    with pytest.raises(HistoricalRegenerationConflict, match="replaced"):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("nested/runs"),
            manifest_bytes=manifest_bytes,
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )

    assert replaced
    assert _open_fd_count() == before_fds


def test_apply_rechecks_runs_inode_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = _init_repo(tmp_path)
    revision = _commit_nested_runs(repo)
    moved_runs = tmp_path / "moved-before-apply"
    replaced = False

    def replace_before_recheck(stage: str, component: str) -> None:
        nonlocal replaced
        if stage == "before_final_recheck" and not replaced:
            replaced = True
            (repo / "nested/runs").replace(moved_runs)
            (repo / "nested/runs").mkdir()

    monkeypatch.setattr(
        data_backfill,
        "_historical_anchor_boundary",
        replace_before_recheck,
    )
    with pytest.raises(HistoricalRegenerationConflict, match="replaced"):
        _run(repo, revision, runs_root=Path("nested/runs"), apply=True)

    assert replaced
    assert not (moved_runs / "job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME).exists()
    assert not (repo / "historical-plan.json").exists()


def test_successful_verifier_closes_anchor_descriptors(tmp_path: Path) -> None:
    repo, result, manifest_path, _ = _verified_world(tmp_path)
    before_fds = _open_fd_count()

    verify_historical_contract_set(
        repo_root=repo,
        runs_root=Path("runs"),
        manifest_bytes=manifest_path.read_bytes(),
        expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
        expected_plan_digest=result.manifest.content_digest,
    )

    assert _open_fd_count() == before_fds


def test_shared_verifier_accepts_authentic_heterogeneous_trial_inventories(
    tmp_path: Path,
) -> None:
    repo, _ = _init_repo(tmp_path, trials=2)
    second_trial = repo / "runs/job-1/trial-1"
    (second_trial / "artifacts/only-second.bin").write_bytes(b"distinct second inventory")
    (second_trial / "artifacts/app/output/final-state.json").write_text(
        json.dumps({"final_digest": DIGEST_B, "distinct": True}),
        encoding="utf-8",
    )
    _git(repo, "add", "--", "runs")
    _git(repo, "commit", "-qm", "heterogeneous trials")
    revision = _git(repo, "rev-parse", "HEAD")
    result = _run(repo, revision, apply=True, promoted=2, derivable=2)
    contracts = [
        json.loads((repo / "runs" / output.path).read_text()) for output in result.manifest.outputs
    ]

    verified = verify_historical_contract_set(
        repo_root=repo,
        runs_root=Path("runs"),
        manifest_bytes=(repo / "historical-plan.json").read_bytes(),
        expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
        expected_plan_digest=result.manifest.content_digest,
    )

    assert verified == result.manifest
    inventory_paths = [
        {artifact["path"] for artifact in contract["artifact_inventory"]} for contract in contracts
    ]
    assert inventory_paths[0] != inventory_paths[1]
    assert "artifacts/only-second.bin" in inventory_paths[0] | inventory_paths[1]


def _mixed_verifier_world(tmp_path: Path):
    repo, _ = _init_repo(tmp_path, trials=2)
    verifier_path = repo / "runs/job-1/trial-1/verifier/result.json"
    verifier = json.loads(verifier_path.read_text())
    verifier.pop("truth_digest")
    verifier_path.write_text(json.dumps(verifier), encoding="utf-8")
    _git(repo, "add", "--", "runs")
    _git(repo, "commit", "-qm", "one refused trial")
    revision = _git(repo, "rev-parse", "HEAD")
    result = _run(repo, revision, apply=True, promoted=2, derivable=1)
    return repo, result, repo / "historical-plan.json"


@pytest.mark.parametrize(
    "extra_relative",
    [
        "job-1/trial-1/artifacts/historical-contract.json",
        "unselected/trial/artifacts/historical-contract.json",
        "unrelated/nested/directory/historical-contract.json",
    ],
)
def test_shared_verifier_refuses_every_extra_output_namespace_location(
    tmp_path: Path,
    extra_relative: str,
) -> None:
    repo, result, manifest_path = _mixed_verifier_world(tmp_path)
    extra = repo / "runs" / extra_relative
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"unplanned historical output")

    with pytest.raises(HistoricalContractSetVerificationError, match="extra="):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=manifest_path.read_bytes(),
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )


@pytest.mark.parametrize("occurrence_kind", ["symlink", "directory", "fifo"])
def test_shared_verifier_refuses_named_symlink_and_nonregular_occurrences(
    tmp_path: Path,
    occurrence_kind: str,
) -> None:
    repo, result, manifest_path, _ = _verified_world(tmp_path)
    occurrence = repo / "runs/unrelated/historical-contract.json"
    occurrence.parent.mkdir(parents=True)
    if occurrence_kind == "symlink":
        external = repo / "external-historical-contract.json"
        external.write_bytes(b"external")
        occurrence.symlink_to(external)
    elif occurrence_kind == "directory":
        occurrence.mkdir()
    else:
        os.mkfifo(occurrence)

    with pytest.raises(HistoricalContractSetVerificationError, match="nonregular"):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=manifest_path.read_bytes(),
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )


def test_shared_verifier_does_not_traverse_symlinked_directory_trap(
    tmp_path: Path,
) -> None:
    repo, result, manifest_path, _ = _verified_world(tmp_path)
    outside = tmp_path / "outside"
    trapped = outside / "nested/historical-contract.json"
    trapped.mkdir(parents=True)
    (repo / "runs/trap").symlink_to(outside, target_is_directory=True)

    verified = verify_historical_contract_set(
        repo_root=repo,
        runs_root=Path("runs"),
        manifest_bytes=manifest_path.read_bytes(),
        expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
        expected_plan_digest=result.manifest.content_digest,
    )

    assert verified == result.manifest


@pytest.mark.parametrize("mutation", ["missing", "altered", "symlink", "old-snapshot"])
def test_shared_verifier_refuses_output_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo, result, manifest_path, output = _verified_world(tmp_path)
    if mutation == "missing":
        output.unlink()
    elif mutation == "altered":
        output.write_bytes(b"altered\n")
    elif mutation == "symlink":
        external = repo / "external-contract.json"
        output.replace(external)
        output.symlink_to(external)
    else:
        payload = json.loads(output.read_text())
        payload["source_snapshot_digest"] = "sha256:" + "0" * 64
        output.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((HistoricalContractSetVerificationError, HistoricalRegenerationConflict)):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=manifest_path.read_bytes(),
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )


def test_shared_verifier_refuses_manifest_substitution_and_missing_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, result, manifest_path, _ = _verified_world(tmp_path)
    substituted = manifest_path.read_bytes().replace(
        result.manifest.content_digest.encode(),
        ("sha256:" + "0" * 64).encode(),
        1,
    )
    with pytest.raises(HistoricalContractSetVerificationError):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=substituted,
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )

    monkeypatch.setattr(
        git_snapshot,
        "_stream_blob_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(HistoricalSnapshotUnavailable("missing")),
    )
    with pytest.raises(HistoricalSnapshotUnavailable):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=manifest_path.read_bytes(),
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )


def test_shared_verifier_refuses_offered_live_source_mismatch(tmp_path: Path) -> None:
    repo, result, manifest_path, _ = _verified_world(tmp_path)
    lock_path = repo / "runs/job/trial/lock.json"
    lock_path.write_text("mutated live source", encoding="utf-8")
    repo_relative = "runs/job/trial/lock.json"

    with pytest.raises(HistoricalContractSetVerificationError):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=manifest_path.read_bytes(),
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
            offered_live_sources={repo_relative: lock_path},
        )


def test_manifest_alone_never_implies_complete_verified_outputs(tmp_path: Path) -> None:
    repo, revision = _init_repo(tmp_path)
    result = _run(repo, revision)
    manifest_path = repo / "historical-plan.json"

    with pytest.raises(HistoricalContractSetVerificationError):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=manifest_path.read_bytes(),
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )


def test_verifier_refuses_unplanned_extra_contract(tmp_path: Path) -> None:
    repo, revision = _init_repo(tmp_path, truth=False)
    result = _run(repo, revision, apply=True, derivable=0)
    manifest_path = repo / "historical-plan.json"
    extra = repo / "runs/job/trial/artifacts" / HISTORICAL_CONTRACT_FILENAME
    extra.write_bytes(b"unplanned historical output\n")

    with pytest.raises(HistoricalContractSetVerificationError):
        verify_historical_contract_set(
            repo_root=repo,
            runs_root=Path("runs"),
            manifest_bytes=manifest_path.read_bytes(),
            expected_source_snapshot=result.manifest.source_snapshot.snapshot_digest,
            expected_plan_digest=result.manifest.content_digest,
        )
