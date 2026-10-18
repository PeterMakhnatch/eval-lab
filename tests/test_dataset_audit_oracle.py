"""Real HAR-191 extraction with a deterministic, injected Docker boundary."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from evallab import dataset_audit_oracle as oracle
from evallab.dataset_audit_contracts import AuditObservation, AuditTask
from evallab.registry import harbor_task_digest, task_directory_digest

REPO = Path(__file__).resolve().parents[1]
IMAGE = "example/source@sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
SOURCE_BEFORE = "def frobnicate(value):\n    return value - 1\n"
SOURCE_AFTER = "def frobnicate(value):\n    return value + 1\n"
TEST_BEFORE = "from src.frob import frobnicate\n\ndef test_frobnicate():\n    assert frobnicate(4) == 3\n"
TEST_AFTER = TEST_BEFORE.replace("== 3", "== 5")


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _git(root: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
    )
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
         "-C", str(root), *args],
        env=env, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class History:
    root: Path
    base: str
    fix: str | None
    test_patch: str


def _history(root: Path, *, kind: str = "source", unreachable: bool = False) -> History:
    root.mkdir(parents=True)
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "History Fixture")
    _git(root, "config", "user.email", "history@example.invalid")
    _write(root / "src/frob.py", SOURCE_BEFORE)
    _write(root / "tests/test_frob.py", TEST_BEFORE)
    _git(root, "add", "src/frob.py", "tests/test_frob.py")
    _git(root, "commit", "-m", "Original frobnicate implementation")
    base = _git(root, "rev-parse", "HEAD")
    patch = "diff --git a/tests/test_frob.py b/tests/test_frob.py\n" + "".join(
        difflib.unified_diff(TEST_BEFORE.splitlines(keepends=True), TEST_AFTER.splitlines(keepends=True),
                             fromfile="a/tests/test_frob.py", tofile="b/tests/test_frob.py")
    )
    fix = None
    if kind != "no-history":
        if kind == "patch-no-apply":
            _write(root / "src/frob.py", SOURCE_BEFORE.replace("value - 1", "value * 2"))
            _git(root, "add", "src/frob.py")
            _git(root, "commit", "-m", "Intermediate source refactor")
        _write(root / "tests/test_frob.py", TEST_AFTER)
        if kind != "test-only":
            _write(root / "src/frob.py", b"\x00binary frobnicate\n" if kind == "binary" else SOURCE_AFTER)
        _git(root, "add", "src/frob.py", "tests/test_frob.py")
        _git(root, "commit", "-m", "Repair frobnicate increment behavior")
        fix = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "--detach", base)
        if unreachable:
            _git(root, "update-ref", "refs/heads/main", base)
    return History(root, base, fix, patch)


def _package(root: Path, history: History, *, image: str = IMAGE) -> tuple[AuditTask, Path]:
    package, workdir = root / "package", root / "post-setup-worktree"
    shutil.copytree(history.root, workdir, ignore=shutil.ignore_patterns(".git"))
    _write(package / "task.toml", (
        'schema_version = "1.4"\n[task]\nname = "example/history"\n'
        f"[environment]\ndocker_image = {json.dumps(image)}\nworkdir = {json.dumps(str(workdir))}\n"
    ))
    _write(package / "instruction.md", "Repair `frobnicate` increment behavior in `src/frob.py`.\n")
    _write(package / "tests/test.patch", history.test_patch)
    _write(package / "tests/test.sh", "#!/bin/sh\necho VERIFIER_MUST_NOT_CHANGE\n")
    _write(package / "environment/Dockerfile", "FROM example/source\n")
    _write(package / "environment/setup.sh", "#!/bin/sh\nexit 97 # extraction must never run setup\n")
    return AuditTask(
        dataset_id="example", task_id="history", task_name="example/history", path=package,
        package_digest=task_directory_digest(package), harbor_digest=harbor_task_digest(package),
        source_uri="test://original-selected-package", revision="fixture-revision",
    ), workdir


class DockerImage:
    """Only Docker is modeled; Git history and extractor behavior remain real."""

    def __init__(self, repositories: dict[str, Path], *, fault: str | None = None):
        self.repositories = repositories
        self.fault = fault
        self.calls: list[list[str]] = []
        self.name: str | None = None
        self.removed = False

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        stdout, stderr, code = "", "", 0
        if args[:2] == ["image", "inspect"]:
            stdout = json.dumps([{
                "Id": "sha256:" + "c" * 64 if self.fault == "image-content" else IMAGE_ID,
                "RepoDigests": ["example/source@sha256:" + "c" * 64] if self.fault == "image-digest" else [IMAGE],
            }])
        elif args[0] == "create":
            self.name = args[args.index("--name") + 1]
            assert args[-1] == IMAGE_ID
            assert args[args.index("--pull") + 1] == "never"
            assert args[args.index("--network") + 1] == "none"
            assert args[args.index("--entrypoint") + 1] == "/bin/true"
            assert args[args.index("--label") + 1] == "evallab.history-oracle=" + self.name
            stdout = "d" * 64 + "\n"
            if self.fault == "create-timeout":
                raise subprocess.TimeoutExpired(["docker", *args], 120)
            if self.fault in ("create", "create-conflict"):
                code, stderr = 1, "CLI failed during container allocation"
        elif args[:2] == ["container", "inspect"]:
            assert args[-1] == self.name
            stdout = json.dumps([{
                "Image": "sha256:" + "c" * 64 if self.fault == "container-image" else IMAGE_ID,
                "State": {"Status": "running" if self.fault == "started" else "created",
                          "Running": self.fault == "started",
                          "StartedAt": "2026-01-01T00:00:00Z" if self.fault == "started" else "0001-01-01T00:00:00Z"},
                "HostConfig": {"NetworkMode": "none"},
                "Config": {"Entrypoint": ["/bin/true"], "Labels": {
                    "evallab.history-oracle": "another-owner" if self.fault == "create-conflict" else self.name,
                }},
            }])
        elif args[0] == "cp":
            assert self.name is not None and args[1].startswith(self.name + ":")
            git_path = args[1].split(":", 1)[1]
            if self.fault == "copy-timeout":
                raise subprocess.TimeoutExpired(["docker", *args], 120)
            if self.fault == "copy":
                code, stderr = 1, "Error response from daemon: storage I/O failure"
            elif git_path not in self.repositories:
                code, stderr = 1, f"Error response from daemon: Could not find the file {git_path} in container {self.name}"
            else:
                shutil.copytree(self.repositories[git_path], args[2], symlinks=True)
        elif args[:2] == ["rm", "--force"]:
            assert args[-1] == self.name
            assert "--volumes" in args
            self.removed = self.fault != "cleanup"
            if not self.removed:
                code, stderr = 1, "Error response from daemon: cleanup failed"
        else:
            raise AssertionError(f"unapproved Docker operation: {args}")
        return subprocess.CompletedProcess(["docker", *args], code, stdout, stderr)


def _extract(task: AuditTask, output: Path) -> tuple[AuditTask | None, AuditObservation]:
    return oracle.prepare_history_oracle(task, repo_root=REPO, output_dir=output)


def _assert_ungraded(observation) -> None:
    assert observation.stage == "oracle"
    assert observation.facts["requires_control"] is True
    assert not {"label", "completed", "reward", "oracle_pass", "oracle_none"} & observation.facts.keys()


def test_availability_only_reads_selected_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _history(tmp_path / "graph")
    task, _ = _package(tmp_path / "selected", history)
    before = _snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("planning invoked an external process")

    monkeypatch.setattr(oracle.subprocess, "run", forbidden)
    assert oracle.history_oracle_available(task)
    assert not oracle.history_oracle_available(task.model_copy(update={"path": None}))
    for relative in ("task.toml", "instruction.md", "tests/test.patch"):
        path = task.path / relative
        original = path.read_bytes()
        path.unlink()
        assert not oracle.history_oracle_available(task)
        path.write_bytes(original)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("image", ["example/source:latest", "example/source", "example/source@sha256:bad"])
def test_mutable_or_malformed_image_is_unavailable_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image: str
) -> None:
    history = _history(tmp_path / "graph")
    task, _ = _package(tmp_path / "selected", history, image=image)
    docker = DockerImage({})
    monkeypatch.setattr(oracle, "_docker", docker)
    assert not oracle.history_oracle_available(task)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == "failed"
    assert not docker.calls and not (tmp_path / "out").exists()
    _assert_ungraded(observation)


@pytest.mark.parametrize("unreachable", [False, True])
def test_real_extractor_derives_source_only_reference_and_immutable_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreachable: bool
) -> None:
    history = _history(tmp_path / "graph", unreachable=unreachable)
    task, workdir = _package(tmp_path / "selected", history)
    before_package, before_git = _snapshot(task.path), _snapshot(history.root / ".git")
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is not None and observation.status == "recorded"
    _assert_ungraded(observation)
    assert observation.facts["extraction_status"] == "ok"
    assert observation.facts["base"] == history.base
    assert observation.facts["image_ref"] == IMAGE and observation.facts["image_id"] == IMAGE_ID
    evidence = observation.facts["extraction"]
    assert evidence["fix"]["sha"] == history.fix
    assert evidence["apply_check_on_base"] is True
    assert evidence["non_test_files"] == ["src/frob.py"]
    assert evidence["n_unreachable_commits"] == int(unreachable)
    patch = Path(observation.facts["patch_path"]).read_bytes()
    assert b"diff --git a/src/frob.py b/src/frob.py" in patch
    assert b"diff --git a/tests/" not in patch
    assert observation.facts["patch_sha256"] == _sha(patch)
    assert observation.facts["extractor_source_sha256"] == _sha((REPO / oracle._EXTRACTOR).read_bytes())
    record_path = Path(observation.facts["oracle_record"])
    record = json.loads(record_path.read_text())
    assert record["components_changed"] == ["solution"]
    assert [change["path"] for change in record["files"]] == ["solution/solve.sh"]
    assert record["parent"]["digest"] == task.package_digest
    assert record["inputs"]["image_id"] == IMAGE_ID
    assert record["inputs"]["base"] == history.base
    assert record["variant_digest"] == variant.package_digest == task_directory_digest(variant.path)
    assert record["variant_harbor_digest"] == variant.harbor_digest == harbor_task_digest(variant.path)
    assert variant.parent_source["oracle_record"] == str(record_path)
    assert set(_snapshot(variant.path)) == set(before_package) | {"solution/solve.sh"}
    for path, raw in before_package.items():
        assert (variant.path / path).read_bytes() == raw
    assert _snapshot(task.path) == before_package and _snapshot(history.root / ".git") == before_git
    assert all(source.sha256 for source in observation.sources if source.available)
    assert docker.removed


@pytest.mark.parametrize("dirty", [False, True])
def test_reference_applies_without_git_and_refuses_changed_base_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirty: bool
) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history)
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, _ = _extract(task, tmp_path / "out")
    assert variant is not None
    if dirty:
        _write(workdir / "src/frob.py", SOURCE_BEFORE.replace("value - 1", "'changed base'"))
    before = _snapshot(workdir)
    env = dict(os.environ, TMPDIR=str(tmp_path), GIT_DIR=str(tmp_path / "removed-git"),
               GIT_WORK_TREE=str(tmp_path / "wrong-worktree"))
    result = subprocess.run(["/bin/sh", str(variant.path / "solution/solve.sh")],
                            env=env, capture_output=True, text=True, check=False)
    assert not (workdir / ".git").exists()
    if dirty:
        assert result.returncode != 0
        assert _snapshot(workdir) == before
    else:
        assert result.returncode == 0, result.stderr
        assert (workdir / "src/frob.py").read_text() == SOURCE_AFTER
        assert (workdir / "tests/test_frob.py").read_text() == TEST_BEFORE


@pytest.mark.parametrize("kind,status,extract_status", [
    ("no-history", "unavailable", "no-identifiable-fix"),
    ("test-only", "unavailable", "test-only-fix"),
    ("patch-no-apply", "unavailable", "patch-no-apply"),
    ("binary", "failed", "unsupported-tree"),
])
def test_real_no_selection_and_unsupported_extraction_are_not_control_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, status: str, extract_status: str
) -> None:
    history = _history(tmp_path / "graph", kind=kind)
    task, workdir = _package(tmp_path / "selected", history)
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == status
    assert observation.facts["extraction_status"] == extract_status
    _assert_ungraded(observation)
    assert docker.removed
    assert not list((tmp_path / "out").rglob("solution.patch"))
    assert not list((tmp_path / "out").rglob("variant-records"))
    evidence = json.loads(next((tmp_path / "out").rglob("evidence.json")).read_text())
    assert evidence["status"] == extract_status
    assert evidence["base"] == history.base


def test_missing_original_git_is_unavailable_not_setup_fallback_or_negative_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _history(tmp_path / "graph")
    task, _ = _package(tmp_path / "selected", history)
    docker = DockerImage({})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == "unavailable"
    assert observation.facts["extraction_status"] == "no-original-git"
    _assert_ungraded(observation)
    assert docker.removed


def test_extra_original_git_path_is_explicit_and_evidence_is_unique_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history)
    extra = "/original-image/repository-history"
    docker = DockerImage({extra: history.root / ".git"})
    monkeypatch.setattr(oracle, "_docker", docker)
    first, first_observation = oracle.prepare_history_oracle(
        task, repo_root=REPO, output_dir=tmp_path / "out", git_paths=(extra, extra),
    )
    assert first is not None
    before = _snapshot(Path(first_observation.facts["output_dir"]))
    second, second_observation = oracle.prepare_history_oracle(
        task, repo_root=REPO, output_dir=tmp_path / "out", git_paths=(extra,),
    )
    assert second is not None and first.path != second.path
    assert first.package_digest == second.package_digest
    assert first_observation.facts["git_path"] == second_observation.facts["git_path"] == extra
    assert _snapshot(Path(first_observation.facts["output_dir"])) == before


@pytest.mark.parametrize("fault,extract_status", [
    ("image-digest", "image-binding-error"),
    ("create", "container-error"),
    ("create-timeout", "docker-timeout"),
    ("started", "image-binding-error"),
    ("container-image", "image-binding-error"),
    ("copy", "copy-error"),
    ("copy-timeout", "docker-timeout"),
    ("cleanup", "cleanup-error"),
])
def test_docker_refusals_and_operational_failures_preserve_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, extract_status: str
) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history)
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"}, fault=fault)
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == "failed"
    assert observation.facts["extraction_status"] == extract_status
    _assert_ungraded(observation)
    if fault == "image-digest":
        assert docker.name is None and not docker.removed
    else:
        assert observation.facts["container_cleanup"] == {"attempted": True, "removed": fault != "cleanup"}
    assert not list((tmp_path / "out").rglob("variant-records"))


def test_declared_content_id_must_match_local_image_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history, image=IMAGE_ID)
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"}, fault="image-content")
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.facts["extraction_status"] == "image-binding-error"
    assert docker.name is None and not docker.removed


@pytest.mark.parametrize("changed", ["instruction.md", "tests/test.patch", "task.toml", "harbor-digest"])
def test_selected_package_changes_are_refused_before_processes_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    history = _history(tmp_path / "graph")
    task, _ = _package(tmp_path / "selected", history)
    if changed == "harbor-digest":
        task = task.model_copy(update={"harbor_digest": _sha(b"wrong native identity")})
    else:
        path = task.path / changed
        path.write_bytes(path.read_bytes() + b"\n# changed after selection\n")
    docker = DockerImage({})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == "failed"
    assert not docker.calls and not (tmp_path / "out").exists()
    _assert_ungraded(observation)


def test_output_overlap_and_unsafe_extra_path_are_refused_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _history(tmp_path / "graph")
    task, _ = _package(tmp_path / "selected", history)
    before = _snapshot(task.path)
    docker = DockerImage({})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, task.path / "evidence")
    assert variant is None and observation.status == "failed"
    variant, observation = oracle.prepare_history_oracle(
        task, repo_root=REPO, output_dir=tmp_path / "out", git_paths=("/repo/../external",),
    )
    assert variant is None and observation.status == "failed"
    assert not docker.calls and _snapshot(task.path) == before


@pytest.mark.parametrize("external", ["alternates", "include", "promisor", "symlink"])
def test_copied_git_cannot_read_external_stores_or_fetch_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: str
) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history)
    git = history.root / ".git"
    if external == "alternates":
        _write(git / "objects/info/alternates", "/must-not-read-host-objects\n")
    elif external == "include":
        _git(history.root, "config", "include.path", "/must-not-read-host-config")
    elif external == "promisor":
        _git(history.root, "config", "remote.origin.promisor", "true")
    else:
        (git / "external-object-link").symlink_to("/must-not-read-host-object")
    docker = DockerImage({str(workdir / ".git"): git})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == "failed"
    assert observation.facts["extraction_status"] == "unsupported-tree"
    assert docker.removed
    _assert_ungraded(observation)


@pytest.mark.parametrize("workdir", ["", "relative/repo", "/repo/../escape", "/repo/\nunsafe"])
def test_unusable_workdir_is_unavailable_without_a_host_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workdir: str
) -> None:
    history = _history(tmp_path / "graph")
    task, _ = _package(tmp_path / "selected", history)
    definition = task.path / "task.toml"
    definition.write_text(
        'schema_version = "1.4"\n[task]\nname = "example/history"\n'
        f"[environment]\ndocker_image = {json.dumps(IMAGE)}\nworkdir = {json.dumps(workdir)}\n"
    )
    docker = DockerImage({})
    monkeypatch.setattr(oracle, "_docker", docker)
    assert not oracle.history_oracle_available(task)
    assert not docker.calls


def test_selected_bytes_changed_during_image_copy_are_refused_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history)
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"})

    def copying(args: list[str]) -> subprocess.CompletedProcess[str]:
        result = docker(args)
        if args[0] == "cp":
            _write(task.path / "instruction.md", "Changed while image extraction was in progress.\n")
        return result

    monkeypatch.setattr(oracle, "_docker", copying)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == "failed"
    assert observation.facts["extraction_status"] == "input-error"
    assert docker.removed
    assert "selected package bytes changed" in observation.reason
    assert not list((tmp_path / "out").rglob("variant-records"))
    _assert_ungraded(observation)


def test_failed_creation_never_deletes_an_unowned_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history)
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"}, fault="create-conflict")
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is None and observation.status == "failed"
    assert observation.facts["extraction_status"] == "cleanup-error"
    assert "not owned by this extraction" in observation.reason
    assert not docker.removed and not any(call[0] == "rm" for call in docker.calls)
    _assert_ungraded(observation)


@pytest.mark.parametrize("kind", ["external_corpus", "curated_derivative"])
def test_descriptive_plugin_provenance_retains_a_strict_local_variant_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    history = _history(tmp_path / "graph")
    task, workdir = _package(tmp_path / "selected", history)
    provenance = {"kind": kind, "corpus": "example-release", "selected_run": "opaque-run-17"}
    task = task.model_copy(update={"parent_source": provenance})
    docker = DockerImage({str(workdir / ".git"): history.root / ".git"})
    monkeypatch.setattr(oracle, "_docker", docker)
    variant, observation = _extract(task, tmp_path / "out")
    assert variant is not None and observation.status == "recorded"
    record = json.loads(Path(observation.facts["oracle_record"]).read_text())
    assert record["parent"]["source"]["kind"] == "local"
    assert record["parent"]["source"]["path"] == str(task.path.resolve())
    assert {key: variant.parent_source[key] for key in provenance} == provenance
    assert observation.facts["selected_parent_source"] == provenance
    assert record["parent"]["digest"] == task.package_digest
    _assert_ungraded(observation)
