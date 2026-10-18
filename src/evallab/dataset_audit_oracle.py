"""Ungraded history references from the selected, never-started task image.

HAR-191's retained extractor owns commit selection and source-patch generation.
This adapter owns original-image binding, local Docker cleanup and solution-only
variant lineage. Extraction is never a completed oracle control.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import posixpath
import re
import shlex
import subprocess
import tempfile
import tomllib
import uuid
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

from evallab.dataset_audit_contracts import AuditObservation, AuditSource, AuditTask, StageStatus
from evallab.dataset_audit_sources import file_source
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_variants import derive_task, materialize

_EXTRACTOR = Path("research/experiments/leak-oracle/extract.py")
_TRANSFORM = "history-oracle@1"
_SUCCESS = frozenset(("ok", "ok-divergent"))
_NO_SELECTION = frozenset(
    ("no-identifiable-fix", "test-only-fix", "empty-diff", "patch-no-apply", "needs-tip-decision")
)
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}\Z")
_REQUIRED_FILES = ("task.toml", "instruction.md", "tests/test.patch")


class _OracleFailure(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _container_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise ValueError(f"unsupported absolute container path: {value!r}")
    return str(path)


def _definition(task: AuditTask) -> tuple[Path, str, str]:
    if task.path is None or task.package_digest is None or task.harbor_digest is None:
        raise ValueError("history extraction requires a selected, digest-bound task package")
    package = Path(task.path)
    if not package.is_dir() or package.is_symlink():
        raise ValueError("selected task package is not an ordinary directory")
    for relative in _REQUIRED_FILES:
        member = package / relative
        if not member.is_file() or member.is_symlink() or member.parent.is_symlink():
            raise ValueError(f"selected task package lacks an ordinary {relative}")
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    environment = config.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("selected task has no declared image environment")
    image, workdir = environment.get("docker_image"), environment.get("workdir")
    if not isinstance(image, str) or not (_IMAGE_ID.fullmatch(image) or _IMAGE_REF.fullmatch(image)):
        raise ValueError("selected docker_image must be an immutable repo@sha256 reference or content ID")
    if not isinstance(workdir, str) or not workdir:
        raise ValueError("selected task has no usable declared workdir")
    return package, image, _container_path(workdir)


def history_oracle_available(task: AuditTask) -> bool:
    """Read selected files only; never probe Docker, spawn a process or write."""
    try:
        _definition(task)
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def _verify_selected(task: AuditTask, package: Path) -> None:
    if task_directory_digest(package) != task.package_digest:
        raise ValueError("selected package bytes changed before history extraction")
    if harbor_task_digest(package) != task.harbor_digest:
        raise ValueError("selected native Harbor task identity changed before history extraction")


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """The sole Docker boundary; tests inject it without replacing extraction."""
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=120
    )


def _one_object(raw: str, label: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError(f"{label} did not describe exactly one object")
    return value[0]


def _copy_original_git(
    image: str, paths: tuple[str, ...], output: Path, facts: dict[str, Any]
) -> list[tuple[str, Path]]:
    commands: list[dict[str, Any]] = []
    facts["docker_commands"] = commands

    def command(args: list[str]) -> subprocess.CompletedProcess[str]:
        entry: dict[str, Any] = {"args": args}
        commands.append(entry)
        try:
            result = _docker(args)
        except (OSError, subprocess.SubprocessError) as exc:
            entry.update(error_type=type(exc).__name__, error=str(exc))
            raise
        entry.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        return result

    inspected = command(["image", "inspect", image])
    if inspected.returncode != 0:
        raise _OracleFailure("image-unavailable", "selected image is not inspectable locally; no pull attempted")
    (output / "image-inspect.json").write_text(inspected.stdout, encoding="utf-8")
    info = _one_object(inspected.stdout, "image inspection")
    image_id = info.get("Id")
    digests = info.get("RepoDigests") or []
    if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
        raise _OracleFailure("image-binding-error", "selected image inspection lacks an exact content ID")
    if not isinstance(digests, list) or not all(isinstance(item, str) for item in digests):
        raise _OracleFailure("image-binding-error", "selected image inspection has invalid repository digests")
    facts.update(image_id=image_id, image_repo_digests=digests)
    if _IMAGE_ID.fullmatch(image):
        bound = image_id == image
    else:
        declared_digest = image.rsplit("@", 1)[1]
        bound = any(item.rsplit("@", 1)[-1] == declared_digest for item in digests)
    if not bound:
        raise _OracleFailure("image-binding-error", "local image identity does not match the selected declared digest")

    name = "evallab-history-oracle-" + uuid.uuid4().hex
    facts["container_name"] = name
    copied: list[tuple[str, Path]] = []
    copies: list[dict[str, Any]] = []
    facts["git_copies"] = copies
    created_ok = False
    # Once create is attempted its unique, owned name must be cleaned even if
    # the CLI fails after allocating the container or a later copy fails.
    try:
        created = command([
            "create", "--name", name, "--network", "none", "--pull", "never",
            "--label", "evallab.history-oracle=" + name, "--entrypoint", "/bin/true", image_id,
        ])
        if created.returncode != 0:
            raise _OracleFailure("container-error", "could not create the inert original-image container")
        created_ok = True
        container = command(["container", "inspect", name])
        if container.returncode != 0:
            raise _OracleFailure("container-error", "could not bind the created container to the original image")
        (output / "container-inspect.json").write_text(container.stdout, encoding="utf-8")
        binding = _one_object(container.stdout, "container inspection")
        state = binding.get("State") or {}
        if (
            binding.get("Image") != image_id
            or state.get("Status") != "created"
            or state.get("Running") is not False
            or state.get("StartedAt") != "0001-01-01T00:00:00Z"
            or (binding.get("HostConfig") or {}).get("NetworkMode") != "none"
            or (binding.get("Config") or {}).get("Entrypoint") != ["/bin/true"]
            or ((binding.get("Config") or {}).get("Labels") or {}).get("evallab.history-oracle") != name
        ):
            raise _OracleFailure("image-binding-error", "container is not the bound, never-started inert image")
        for index, git_path in enumerate(paths):
            destination = output / "original-git" / f"{index:03d}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = command(["cp", f"{name}:{git_path}", str(destination)])
            copy = {"git_path": git_path, "path": str(destination)}
            copies.append(copy)
            if result.returncode != 0:
                diagnostic = result.stderr.lower()
                if f"could not find the file {git_path.lower()}" in diagnostic:
                    copy["status"] = "missing"
                    continue
                raise _OracleFailure("copy-error", f"could not copy original-image Git at {git_path}")
            copy["status"] = "copied"
            copied.append((git_path, destination))
    finally:
        facts["container_cleanup"] = {"attempted": True, "removed": False}
        try:
            if not created_ok:
                ownership = command(["container", "inspect", name])
                if ownership.returncode != 0:
                    if "no such container" in ownership.stderr.lower():
                        facts["container_cleanup"]["absent"] = True
                    else:
                        raise _OracleFailure("cleanup-error", "could not confirm ownership after failed container creation")
                else:
                    owner = _one_object(ownership.stdout, "container ownership")
                    labels = (owner.get("Config") or {}).get("Labels") or {}
                    if labels.get("evallab.history-oracle") != name:
                        raise _OracleFailure("cleanup-error", "refusing to remove a container not owned by this extraction")
                    created_ok = True
            if created_ok:
                cleanup = command(["rm", "--force", "--volumes", name])
                facts["container_cleanup"]["removed"] = cleanup.returncode == 0
                if cleanup.returncode != 0:
                    raise _OracleFailure("cleanup-error", "owned history-extraction container cleanup failed")
        except _OracleFailure:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise _OracleFailure("cleanup-error", f"owned container cleanup could not finish: {exc}") from exc
    return copied


def _load_extractor(root: Path, output: Path, facts: dict[str, Any]) -> ModuleType:
    source = root / _EXTRACTOR
    raw = source.read_bytes()
    snapshot = output / "extractor-source.py"
    snapshot.write_bytes(raw)
    facts.update(extractor_source=str(source), extractor_source_sha256=_sha(raw),
                 extractor_snapshot=str(snapshot))
    spec = importlib.util.spec_from_file_location("evallab_har191_history_extractor", snapshot)
    if spec is None or spec.loader is None:
        raise ValueError("the retained HAR-191 extractor cannot be imported")
    module = importlib.util.module_from_spec(spec)
    # Only the exact retained extractor is imported. Interpreter caches stay
    # inside this invocation, never beside task inputs or experiment source.
    spec.loader.exec_module(module)
    return module


def _check_git_copy(extractor: ModuleType, git: Path) -> None:
    if git.is_symlink() or not git.is_dir() or any(path.is_symlink() for path in git.rglob("*")):
        raise _OracleFailure("unsupported-tree", "original-image Git must be a self-contained ordinary directory")
    for relative in ("commondir", "objects/info/alternates", "objects/info/http-alternates"):
        path = git / relative
        if path.exists() and path.read_bytes().strip():
            raise _OracleFailure("unsupported-tree", "external Git object stores are not copied or fabricated")
    # Git reads in a partial clone may fetch objects. Reject promisor/include
    # configuration rather than allowing the extractor to acquire a repository.
    config = extractor.git_process(
        str(git), "config", "--no-includes", "--get-regexp",
        r"^(extensions\.partialclone|remote\..*\.promisor|include\.path|includeif\..*\.path)$",
        encoding="utf-8", errors="replace",
    )
    if config.returncode not in (0, 1):
        raise _OracleFailure("git-error", "cannot inspect the copied original Git configuration")
    if config.returncode == 0 or any((git / "objects" / "pack").glob("*.promisor")):
        raise _OracleFailure("unsupported-tree", "partial or externally configured Git is not a local extraction input")


def _solve_script(workdir: str, patch: bytes) -> bytes:
    encoded = base64.b64encode(patch).decode("ascii")
    return (
        "#!/bin/sh\nset -eu\n"
        "patch=$(mktemp)\ntrap 'rm -f \"$patch\"' EXIT HUP INT TERM\n"
        f"printf '%s' '{encoded}' | base64 -d > \"$patch\"\n"
        "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n"
        "unset GIT_COMMON_DIR GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT\n"
        "export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0\n"
        "export GIT_TERMINAL_PROMPT=0\n"
        f"cd {shlex.quote(workdir)}\n"
        "git -c core.hooksPath=/dev/null -c core.fsmonitor=false apply --no-index --check \"$patch\"\n"
        "git -c core.hooksPath=/dev/null -c core.fsmonitor=false apply --no-index \"$patch\"\n"
    ).encode()


def prepare_history_oracle(
    task: AuditTask,
    *,
    repo_root: Path,
    output_dir: Path,
    git_paths: tuple[str, ...] = (),
) -> tuple[AuditTask | None, AuditObservation]:
    """Extract an ungraded source reference; only the parent runs its control.

    Each invocation owns a fresh directory below ``output_dir``. No image is
    pulled, started or set up; the selected immutable image is the only source
    of Git. Missing history and failed extraction are not oracle:none proof.
    """
    facts: dict[str, Any] = {
        "reference": "history-oracle", "package_digest": task.package_digest,
        "harbor_digest": task.harbor_digest, "requires_control": True,
        "selected_package": str(task.path) if task.path is not None else None,
        "selected_source_uri": task.source_uri, "selected_revision": task.revision,
        "selected_parent_source": dict(task.parent_source),
    }
    sources: list[AuditSource] = []
    output: Path | None = None
    variant: AuditTask | None = None
    status: StageStatus = "failed"
    reason: str | None = None
    try:
        package, image, workdir = _definition(task)
        _verify_selected(task, package)
        facts.update(image_ref=image, workdir=workdir)
        sources.extend(file_source(package / path, binding="selected-" + path) for path in _REQUIRED_FILES)
        candidates = tuple(dict.fromkeys((posixpath.join(workdir, ".git"), *(
            _container_path(path) for path in git_paths
        ))))
        base_output = Path(output_dir).resolve()
        if base_output == package.resolve() or package.resolve() in base_output.parents:
            raise ValueError("history extraction output must not be inside the selected package")
        base_output.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", task.task_id.lower()).strip("-")[:32] or "task"
        output = Path(tempfile.mkdtemp(prefix=slug + "-", dir=base_output))
        facts["output_dir"] = str(output)
        extractor = _load_extractor(Path(repo_root), output, facts)
        copied = _copy_original_git(image, candidates, output, facts)
        _verify_selected(task, package)
        attempts: list[dict[str, Any]] = []
        facts["extraction_attempts"] = attempts
        selected: tuple[dict[str, Any], Path, bytes] | None = None
        for index, (git_path, git) in enumerate(copied):
            extraction_dir = output / "extractions" / f"{index:03d}"
            attempt: dict[str, Any] = {"git_path": git_path, "git_copy": str(git)}
            attempts.append(attempt)
            try:
                _check_git_copy(extractor, git)
                base = extractor.run_git(str(git), "rev-parse", "--verify", "HEAD^{commit}").strip()
                attempt["base"] = base
                evidence = extractor.extract(package, str(git), extraction_dir)
                _verify_selected(task, package)
                attempt["extraction"] = evidence
                attempt["status"] = evidence.get("status")
                evidence_path = extraction_dir / "evidence.json"
                sources.append(file_source(evidence_path, binding="history-extraction-evidence"))
                sources.append(file_source(git / "HEAD", binding="original-image-git-head"))
                if evidence.get("base") not in (None, base):
                    raise _OracleFailure("extraction-binding-error", "extractor evidence differs from the original-image HEAD")
                if evidence.get("status") not in _SUCCESS:
                    continue
                if evidence.get("base") != base or evidence.get("apply_check_on_base") is not True:
                    raise _OracleFailure("extraction-binding-error", "extraction does not bind the actual original-image HEAD and apply check")
                if evidence.get("image") not in (None, image) or (
                    evidence.get("workdir") is not None
                    and _container_path(evidence["workdir"]) != workdir
                ):
                    raise _OracleFailure("extraction-binding-error", "extractor metadata differs from the selected image/workdir")
                patch_path = extraction_dir / "solution.patch"
                patch = patch_path.read_bytes()
                patch_files = extractor.parse_test_patch(patch.decode("utf-8"))
                if (
                    not patch.strip() or not patch_files or patch_path.is_symlink()
                    or len(patch) != evidence.get("solution_patch_bytes")
                    or set(patch_files) != set(extractor.non_test_files_of(patch_files, evidence.get("test_patch_files", [])))
                ):
                    raise _OracleFailure("extraction-binding-error", "extraction lacks a nonempty, source-only bound patch")
                attempt["patch_sha256"] = _sha(patch)
                selected = attempt, patch_path, patch
                break
            except (_OracleFailure, extractor.ExtractionError) as exc:
                attempt.update(status=exc.status, reason=str(exc))
        if selected is None:
            failures = [attempt for attempt in attempts if attempt.get("status") not in _NO_SELECTION]
            facts["extraction_status"] = (
                failures[0]["status"] if failures else attempts[-1]["status"] if attempts else "no-original-git"
            )
            status = "failed" if failures else "unavailable"
            reason = "no usable source reference extracted; this is not a completed oracle control"
        else:
            attempt, patch_path, patch = selected
            facts.update(
                extraction_status=attempt["status"], extraction=attempt["extraction"],
                git_path=attempt["git_path"], base=attempt["base"],
                patch_sha256=_sha(patch), patch_path=str(patch_path),
            )
            _verify_selected(task, package)
            records = output / "variant-records"
            variants = output / "variants"
            record = derive_task(
                package, changes={"solution/solve.sh": _solve_script(workdir, patch)},
                transform=_TRANSFORM,
                rationale="Ungraded HAR-191 source-only reference extracted from the selected original image; native oracle control required",
                created_by="har195-audit",
                inputs={key: facts[key] for key in (
                    "package_digest", "harbor_digest", "image_ref", "image_id", "workdir",
                    "git_path", "base", "extractor_source_sha256", "patch_sha256",
                )},
                parent_source={"kind": "local", "path": str(package.resolve())},
                repo_root=repo_root, records_dir=records, variants_root=variants,
            )
            record_path = records / record.task_slug / f"{record.digest12}.json"
            derived = materialize(record, package, repo_root=repo_root, records_dir=records, variants_root=variants)
            _verify_selected(task, package)
            if (
                record.parent.digest != task.package_digest
                or record.parent.harbor_digest != task.harbor_digest
                or record.components_changed != ["solution"]
                or [item.path for item in record.files] != ["solution/solve.sh"]
            ):
                raise _OracleFailure("variant-binding-error", "history variant changed more than the selected solution")
            lineage = {
                **task.parent_source, "oracle_transform": record.transform,
                "oracle_parent_digest": record.parent.digest,
                "oracle_variant_digest": record.variant_digest,
                "oracle_record": str(record_path),
            }
            facts.update(
                oracle_transform=record.transform, oracle_parent_digest=record.parent.digest,
                oracle_variant_digest=record.variant_digest,
                oracle_variant_harbor_digest=record.variant_harbor_digest,
                oracle_record=str(record_path),
            )
            sources.append(file_source(patch_path, binding="history-source-patch"))
            sources.append(file_source(record_path, binding="history-reference-variant"))
            variant = task.model_copy(update={
                "path": derived, "package_digest": record.variant_digest,
                "harbor_digest": record.variant_harbor_digest, "parent_source": lineage,
            })
            status, reason = "recorded", "source reference extracted but not graded; a bound native control is still required"
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, _OracleFailure):
            extraction_status = exc.status
        elif isinstance(exc, subprocess.TimeoutExpired):
            extraction_status = "docker-timeout"
        elif isinstance(exc, (OSError, subprocess.SubprocessError)):
            extraction_status = "io-error"
        else:
            extraction_status = "input-error"
        facts["extraction_status"] = extraction_status
        status, reason, variant = "failed", str(exc), None
    except Exception as exc:
        facts["extraction_status"] = "internal-error"
        status, reason, variant = "failed", f"{type(exc).__name__}: {exc}", None
    if output is not None:
        try:
            receipt = output / "original-image.json"
            receipt.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            sources.append(file_source(receipt, binding="selected-original-image-extraction"))
            for filename, binding in (
                ("extractor-source.py", "har191-extractor-source"),
                ("image-inspect.json", "selected-image-content-identity"),
                ("container-inspect.json", "never-started-original-image-container"),
            ):
                path = output / filename
                if path.is_file():
                    sources.append(file_source(path, binding=binding))
        except OSError as exc:
            status, reason, variant = "failed", f"cannot retain extraction receipt: {exc}", None
    return variant, AuditObservation(stage="oracle", status=status, facts=facts, sources=tuple(sources), reason=reason)
