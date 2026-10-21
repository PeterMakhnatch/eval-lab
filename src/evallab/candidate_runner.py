"""Isolated artifact, unified-patch, and explicit solution-bundle replay.

``run_candidate(task, files={"answer.txt": "good"}, target_dir="/app")``
installs artifacts, not executable scripts. A directory passed as ``files`` has
exactly the same meaning even if it contains solve.sh. Only ``solution_bundle``
executes a caller's solve.sh, using the existing native solution-directory mode.
``patch`` is unified-diff text (or an explicit Path), applied by the task image's
``patch`` executable inside its fresh environment. Missing image dependencies
are execution errors, never host-side installation or an invented reward.

``run_candidates`` accepts an ordered sequence of candidate keyword mappings.
Execution policy is batch-wide: candidates cannot override the backend, cap,
approval, ledger, app, billing tag, timeout, or evidence locations. Modal uses
only the native runner's durable reservation ledger. The cap bounds admitted
conservative reservations, not provider invoices or a provider billing switch.
The known GA hosted-verifier-judge contract is refused before allocation: this
lane has no authorization or meter for that separate API. This is not a generic
detector of arbitrary external costs embedded in task scripts.
Local/auto trials serialize, including across this lane's cooperating processes;
Modal trials may overlap up to max_concurrency. Native Docker admission remains
in force independently of the lane lock.

Plans only read source inputs. They do not stage packages, import optional
runtimes, inspect/write the ledger, probe Docker, or call providers. Therefore
live admission and remaining-cap checks are explicitly deferred. A port plan
retains the adapter's pure description; its native reservation and build-context
identities become available after local package preparation during execution.
Image source digests are not cache keys/hits. Actual native environment/image
identities come exclusively from hydrated native runtime observations.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import threading
import tomllib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from evallab import upstream_grader as native
from evallab.docker_lock import default_state_dir

Grader = Literal["upstream", "port"]
Files = Mapping[str, str | bytes] | Path | str
_INPUT_FIELDS = frozenset(
    {"task", "files", "patch", "solution_bundle", "target_dir", "patch_strip", "grader", "port_task"}
)
_LOCAL_TRIAL_LOCK = threading.Lock()
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")
_PROTECTED = ("/solution", "/tests", "/logs", "/proc", "/sys", "/dev")


@dataclass(frozen=True)
class _Payload:
    kind: str
    identity: dict[str, Any]
    files: tuple[tuple[str, bytes, int], ...] = ()
    directories: tuple[tuple[str, int], ...] = ()
    patch: bytes | None = None
    patch_paths: tuple[str, ...] = ()
    patch_deletions: tuple[str, ...] = ()
    solution: Path | None = None
    sources: tuple[tuple[Path, str], ...] = ()


@dataclass
class _Prepared:
    task: Path
    payload: _Payload
    native_plan: dict[str, Any] | None
    adapter: dict[str, Any] | None
    target: str | None
    patch_strip: int
    sources: list[tuple[Path, str]] = field(default_factory=list)
    external_dependencies: list[dict[str, Any]] = field(default_factory=list)


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _source_digest(path: Path) -> str:
    if path.is_symlink():
        raise native.GraderRefusal(f"input symlinks are not supported: {path}")
    if path.is_dir():
        return native._tree_digest(path)
    if not path.is_file():
        raise native.GraderRefusal(f"input must be a regular file or directory: {path}")
    return native._file_digest(path)


def _source_path(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise native.GraderRefusal(f"input symlinks are not supported: {path}")
    return path.resolve()


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _input_roots(request: Mapping[str, Any], *, ignore_invalid: bool = False) -> list[Path]:
    roots = []
    for name in ("task", "port_task", "solution_bundle", "files", "patch"):
        value = request.get(name)
        # A patch string is always text, never an implicitly opened host path.
        if isinstance(value, Path) or (name != "patch" and isinstance(value, str)):
            try:
                roots.append(Path(value).expanduser().resolve())
            except (OSError, ValueError, TypeError):
                if not ignore_invalid:
                    raise
    return roots


def _check_locations(output: Path, roots: Sequence[Path], ledger: Path | str | None) -> None:
    if any(_overlaps(output, root) for root in roots):
        raise native.GraderRefusal("evidence directory must not overlap immutable inputs")
    if output.exists():
        raise native.GraderRefusal("evidence directory already exists; choose a fresh directory")
    if ledger is not None:
        ledger_path = Path(ledger).expanduser().resolve()
        if any(_overlaps(ledger_path, root) for root in roots):
            raise native.GraderRefusal("shared spend ledger must not overlap immutable inputs")
        if _overlaps(ledger_path, output):
            raise native.GraderRefusal("shared spend ledger must be outside fresh evidence directories")


def _relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise native.GraderRefusal(f"{label} must be a nonempty relative POSIX path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise native.GraderRefusal(f"{label} contains a control character")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise native.GraderRefusal(f"{label} must stay relative to the isolated target directory")
    return path.as_posix()


def _target_path(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value:
        raise native.GraderRefusal("target_dir is an absolute path inside the task environment")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise native.GraderRefusal("target_dir contains a control character")
    if ".." in value.split("/"):
        raise native.GraderRefusal("target_dir cannot contain parent traversal")
    target = PurePosixPath("/" + value.lstrip("/")).as_posix()
    if any(target == root or target.startswith(root + "/") for root in _PROTECTED):
        raise native.GraderRefusal("target_dir cannot overwrite runtime/verifier/transport paths")
    return target


def _patch_path(value: str, strip: int) -> str | None:
    path = value.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    if any(character.isspace() for character in path) or path.startswith('"'):
        raise native.GraderRefusal("patch filenames must be unquoted paths without whitespace")
    relative = _relative_path(path, label="patch path")
    parts = PurePosixPath(relative).parts
    if len(parts) <= strip:
        raise native.GraderRefusal("patch_strip removes the entire patch filename")
    return _relative_path("/".join(parts[strip:]), label="stripped patch path")


def _validate_patch(data: bytes, strip: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate regular-file unified hunks and return targets/final deletions."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise native.GraderRefusal("patch must be UTF-8 unified-diff text") from exc
    if "\x00" in text:
        raise native.GraderRefusal("binary patches are unsupported")
    lines = text.splitlines()
    index = 0
    paths: dict[str, bool] = {}
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        if line.startswith("diff --git "):
            names = line.removeprefix("diff --git ").split(" ")
            if len(names) != 2:
                raise native.GraderRefusal("unsupported quoted/ambiguous git patch paths")
            for name in names:
                _patch_path(name, strip)
            index += 1
            continue
        if re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100(?:644|755))?", line):
            index += 1
            continue
        if re.fullmatch(r"(?:new|deleted) file mode 100(?:644|755)", line):
            index += 1
            continue
        if not line.startswith("--- ") or index + 1 >= len(lines):
            raise native.GraderRefusal("patch must contain regular-file unified headers and hunks")
        old = _patch_path(line[4:], strip)
        if not lines[index + 1].startswith("+++ "):
            raise native.GraderRefusal("unified patch is missing its +++ header")
        new = _patch_path(lines[index + 1][4:], strip)
        if old is None and new is None or old is not None and new is not None and old != new:
            raise native.GraderRefusal("patch renames and ambiguous file identities are unsupported")
        paths[str(new or old)] = new is None
        index += 2
        hunks = 0
        while index < len(lines) and lines[index].startswith("@@ "):
            match = _HUNK.fullmatch(lines[index])
            if match is None:
                raise native.GraderRefusal("malformed unified patch hunk")
            old_remaining = int(match[2]) if match[2] is not None else 1
            new_remaining = int(match[4]) if match[4] is not None else 1
            index += 1
            hunks += 1
            while old_remaining or new_remaining:
                if index >= len(lines) or not lines[index] or lines[index][0] not in " +-":
                    raise native.GraderRefusal("unified patch hunk counts do not match its body")
                prefix = lines[index][0]
                old_remaining -= prefix in " -"
                new_remaining -= prefix in " +"
                if old_remaining < 0 or new_remaining < 0:
                    raise native.GraderRefusal("unified patch hunk counts do not match its body")
                index += 1
                if index < len(lines) and lines[index] == "\\ No newline at end of file":
                    index += 1
        if not hunks:
            raise native.GraderRefusal("each unified patch file needs at least one hunk")
    if not paths:
        raise native.GraderRefusal("patch contains no unified hunks")
    return tuple(paths), tuple(path for path, deleted in paths.items() if deleted)


def _payload(
    files: Files | None, patch: str | Path | None, solution_bundle: Path | str | None, strip: int
) -> _Payload:
    if sum(value is not None for value in (files, patch, solution_bundle)) != 1:
        raise native.GraderRefusal("select exactly one files, patch, or explicit solution_bundle input")
    if type(strip) is not int or strip < 0:
        raise native.GraderRefusal("patch_strip must be a nonnegative integer")
    if solution_bundle is not None:
        source = _source_path(solution_bundle)
        digest = _source_digest(source)
        if not source.is_dir() or not (source / "solve.sh").is_file():
            raise native.GraderRefusal("solution_bundle must be a directory containing solve.sh")
        return _Payload(
            "solution-bundle",
            {"kind": "solution-bundle", "path": str(source), "digest": digest},
            solution=source,
            sources=((source, digest),),
        )
    if patch is not None:
        if not isinstance(patch, str | Path):
            raise native.GraderRefusal("patch must be unified text or an explicit Path")
        sources: tuple[tuple[Path, str], ...] = ()
        if isinstance(patch, Path):
            source = _source_path(patch)
            before = _source_digest(source)
            if not source.is_file():
                raise native.GraderRefusal("patch Path must name a regular file")
            data = source.read_bytes()
            if _source_digest(source) != before:
                raise native.GraderRefusal("patch changed while reading; no execution")
            sources = ((source, before),)
        else:
            data = patch.encode("utf-8")
        paths, deletions = _validate_patch(data, strip)
        return _Payload(
            "patch",
            {
                "kind": "patch", "digest": _digest_bytes(data), "paths": list(paths),
                "deletions": list(deletions), "strip": strip,
            },
            patch=data,
            patch_paths=paths,
            patch_deletions=deletions,
            sources=sources,
        )
    entries: list[tuple[str, bytes, int]] = []
    directories: list[tuple[str, int]] = []
    sources = ()
    if isinstance(files, Mapping):
        for name, content in files.items():
            relative = _relative_path(name, label="artifact path")
            if not isinstance(content, str | bytes):
                raise native.GraderRefusal("artifact content must be str or bytes, never a host path")
            entries.append((relative, content.encode("utf-8") if isinstance(content, str) else content, 0o644))
    elif isinstance(files, Path | str):
        source = _source_path(files)
        before = _source_digest(source)
        if not source.is_dir():
            raise native.GraderRefusal("files path must name a directory; use a mapping for content")
        for item in sorted(source.rglob("*")):
            relative = _relative_path(item.relative_to(source).as_posix(), label="artifact path")
            mode = stat.S_IMODE(item.stat().st_mode)
            if item.is_dir():
                directories.append((relative, mode))
            else:
                entries.append((relative, item.read_bytes(), mode))
        if _source_digest(source) != before:
            raise native.GraderRefusal("artifact directory changed while reading; no execution")
        sources = ((source, before),)
    else:
        raise native.GraderRefusal("files must be a relative-path/content mapping or directory")
    entries.sort(key=lambda entry: entry[0])
    file_names = {entry[0] for entry in entries}
    for name in [*file_names, *(entry[0] for entry in directories)]:
        if any(parent.as_posix() in file_names for parent in PurePosixPath(name).parents):
            raise native.GraderRefusal("artifact paths contain a file/directory collision")
    manifest: dict[str, Any] = {
        "kind": "files",
        "files": [
            {"path": name, "digest": _digest_bytes(content), "mode": mode, "bytes": len(content)}
            for name, content, mode in entries
        ],
        "directories": [{"path": name, "mode": mode} for name, mode in directories],
    }
    manifest["digest"] = _digest_bytes(json.dumps(manifest, sort_keys=True).encode())
    return _Payload("files", manifest, tuple(entries), tuple(directories), sources=sources)


def _known_external_dependencies(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recognize the native GA hosted-judge contract, not arbitrary task costs."""
    environment = (config.get("verifier") or {}).get("env") or {}
    if (
        isinstance(environment, Mapping)
        and environment.get("GA_JUDGE_API")
        and str(environment.get("VERIFY_AGENT_JUDGE", "1")) != "0"
    ):
        return [
            {
                "kind": "configured-hosted-judge",
                "phase": "verifier",
                "configuration_keys": sorted(
                    key for key in environment if key.startswith("GA_JUDGE_")
                ),
                "approved": False,
                "reason": "the configured hosted verifier judge is not authorized or metered by the infrastructure reservation cap",
            }
        ]
    return []


def _prepare(
    task: Path | str,
    *,
    files: Files | None,
    patch: str | Path | None,
    solution_bundle: Path | str | None,
    target_dir: str | None,
    patch_strip: int,
    grader: Grader,
    port_task: Path | str | None,
    backend: native.Backend,
    platform: str | None,
    timeout_seconds: int,
) -> _Prepared:
    if backend not in {"auto", "docker", "modal"}:
        raise native.GraderRefusal(f"unsupported backend: {backend}")
    if grader not in {"upstream", "port"}:
        raise native.GraderRefusal(f"unsupported grader: {grader}")
    if (grader == "port") != (port_task is not None):
        raise native.GraderRefusal("grader='port' requires explicit port_task; upstream forbids it")
    candidate = _payload(files, patch, solution_bundle, patch_strip)
    target = _target_path(target_dir)
    if candidate.kind == "solution-bundle" and target_dir is not None:
        raise native.GraderRefusal("solution_bundle uses the native task workdir, not target_dir")
    package = _source_path(task)
    original_digest = _source_digest(package)
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    dependencies = _known_external_dependencies(config)
    adapter = None
    prepared_native = None
    sources = [(package, original_digest), *candidate.sources]
    if grader == "port":
        from evallab.candidate_port import describe_port_task

        assert port_task is not None
        adapter = describe_port_task(package, port_task)
        port_source = _source_path(port_task)
        sources.append((port_source, _source_digest(port_source)))
        # Check universal native options without allocating a temporary package.
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise native.GraderRefusal("timeout_seconds must be an explicit positive integer")
        if platform not in {None, "linux/amd64", "linux/arm64"}:
            raise native.GraderRefusal("platform must be linux/amd64 or linux/arm64")
    else:
        # Never invoke native Modal admission in a plan: it reads the ledger.
        prepared_native = native.plan_upstream_grader(
            package, blank=True, backend="auto", platform=platform, timeout_seconds=timeout_seconds
        )
    if _source_digest(package) != original_digest:
        raise native.GraderRefusal("task changed during preflight; no execution")
    if target is None and candidate.kind != "solution-bundle":
        target = _target_path((config.get("environment") or {}).get("workdir"))
    if target is not None:
        for entries in (candidate.files, candidate.directories):
            for entry in entries:
                _target_path(target.rstrip("/") + "/" + entry[0])
        for name in candidate.patch_paths:
            _target_path(target.rstrip("/") + "/" + name)
    return _Prepared(
        package, candidate, prepared_native, adapter, target, patch_strip, sources, dependencies
    )


def _source_images(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    contexts = []
    if plan is not None:
        package = Path(plan["task"]["path"])
        for resource in plan["resources"]:
            folder = package / resource["build_context"]
            contexts.append(
                {
                    "role": resource["role"],
                    "build_context": resource["build_context"],
                    "source_digest": native._tree_digest(folder) if folder.is_dir() else None,
                    "declared_image": resource["docker_image"],
                }
            )
    return {
        "source_contexts": contexts,
        "observed": [],
        "cache_hit": None,
        "semantics": "source digests are not native cache keys or observed cache hits; candidate bytes stay outside build contexts",
        "pending_port_preparation": plan is None,
    }


def _plan_result(prepared: _Prepared, grader: Grader, backend: native.Backend) -> dict[str, Any]:
    native_plan = prepared.native_plan
    adapter = prepared.adapter
    if native_plan is None:
        assert adapter is not None
        task_identity = adapter["upstream_identity"]
        resources = adapter["resources"]
    else:
        task_identity = native_plan["task"]
        resources = native_plan["resources"]
    images = _source_images(native_plan)
    if adapter is not None:
        images["adapter_source_identity"] = adapter.get("native_image_identity")
    return {
        "schema": "evallab.candidate.plan/v1",
        "grader": grader,
        "task": task_identity,
        "input": prepared.payload.identity,
        "target_dir": prepared.target,
        "target_semantics": "explicit/declared task workdir, otherwise the fresh image's runtime working directory",
        "backend": backend,
        "resources": resources,
        "modal_reservation": native_plan["modal_reservation"] if native_plan is not None else None,
        "native_plan": native_plan,
        "grader_provenance": adapter,
        "images": images,
        "external_dependencies": prepared.external_dependencies,
        "admission": "deferred: no Docker/provider/ledger access in candidate planning",
        "executed": False,
        "reserved_usd": 0.0,
        "reward": None,
        "passed": None,
        "runnable": not prepared.external_dependencies,
        "refusals": [item["reason"] for item in prepared.external_dependencies],
    }


_INSTALL_HEADER = r'''#!/bin/bash
set -euo pipefail
check_path() {
    local current="$1"
    while [ "$current" != / ] && [ -n "$current" ]; do
        if [ -L "$current" ]; then
            printf 'candidate destination has a symlink: %s\n' "$current" >&2
            return 1
        fi
        current="${current%/*}"
    done
}
check_destination() {
    case "$1" in
        /solution|/solution/*|/tests|/tests/*|/logs|/logs/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*)
            printf 'candidate destination is a protected runtime path: %s\n' "$1" >&2
            return 1;;
    esac
    check_path "$1"
}
'''


def _stage_payload(prepared: _Prepared, output: Path) -> Path:
    payload = prepared.payload
    if payload.solution is not None:
        return payload.solution
    bundle = output / "candidate-input"
    bundle.mkdir()
    target = shlex.quote(prepared.target) if prepared.target is not None else '"$PWD"'
    script = _INSTALL_HEADER + f"target={target}\ncheck_destination \"$target\"\nmkdir -p -- \"$target\"\n"
    script += 'target="$(cd -- "$target" && pwd -P)"\ncheck_destination "$target"\n'
    if payload.kind == "files":
        (bundle / "payload").mkdir()
        for name, _mode in payload.directories:
            destination = '"${target%/}"/' + shlex.quote(name)
            script += f"check_destination {destination}\nmkdir -p -- {destination}\n"
        for index, (name, content, mode) in enumerate(payload.files):
            source = bundle / "payload" / str(index)
            source.write_bytes(content)
            source.chmod(mode & 0o777)
            script += f"destination=\"${{target%/}}\"/{shlex.quote(name)}\n"
            script += 'check_destination "$destination"\nmkdir -p -- "$(dirname -- "$destination")"\n'
            script += f'cp -p -- /solution/payload/{index} "$destination"\n'
        # Apply directory modes last, so read-only input directories can be installed.
        for name, mode in reversed(payload.directories):
            # BSD chmod treats -- after the mode as a filename; paths are absolute.
            script += f"chmod {mode & 0o777:o} \"${{target%/}}\"/{shlex.quote(name)}\n"
    else:
        assert payload.patch is not None
        (bundle / "candidate.patch").write_bytes(payload.patch)
        for name in payload.patch_paths:
            script += f"check_destination \"${{target%/}}\"/{shlex.quote(name)}\n"
        script += (
            f'patch --batch --forward --fuzz=0 --no-backup-if-mismatch --reject-file=- '
            f'-p{prepared.patch_strip} -d "$target" < /solution/candidate.patch\n'
        )
        # BSD patch can retain an empty file for an explicit +++ /dev/null
        # deletion. Never use -E: that would also erase legitimate empty files.
        for name in payload.patch_deletions:
            script += f"destination=\"${{target%/}}\"/{shlex.quote(name)}\n"
            script += 'check_destination "$destination"\n'
            script += (
                'if [ -e "$destination" ]; then\n'
                '    if [ ! -f "$destination" ] || [ -s "$destination" ]; then\n'
                '        printf "explicit patch deletion left a nonempty/nonregular file: %s\\n" "$destination" >&2\n'
                '        exit 1\n'
                '    fi\n'
                '    rm -- "$destination"\n'
                'fi\n'
            )
    (bundle / "solve.sh").write_text(script, encoding="utf-8")
    return bundle


def _atomic_json(path: Path, data: Any) -> None:
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _local_lane(backend: native.Backend) -> Iterator[None]:
    if backend == "modal":
        yield
        return
    # Native slots=1 still applies. This lane lock also covers concurrent API
    # callers/processes, and intentionally serializes auto's possible Docker work.
    with _LOCAL_TRIAL_LOCK:
        directory = default_state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "candidate-trial.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _empty_result(status: str, error: str | None, *, planned: bool = False) -> dict[str, Any]:
    return {
        "schema": "evallab.candidate.plan/v1" if planned else "evallab.candidate.result/v1",
        "status": status,
        "reward": None,
        "rewards": None,
        "passed": None,
        "per_test": {"status": "unavailable", "tests": [], "sources": []},
        "logs": {"agent": [], "verifier": []},
        "error": error,
        "native_error": None,
        "executed": False,
        "spend": None,
        "cost": None,
        "output_dir": None,
        "runnable": False,
        "refusals": [error] if error else [],
    }


def _drift(prepared: _Prepared) -> list[str]:
    errors = []
    for path, expected in prepared.sources:
        try:
            if _source_digest(path) != expected:
                errors.append(f"immutable source changed: {path}")
        except (OSError, ValueError) as exc:
            errors.append(f"immutable source unavailable: {path}: {exc}")
    return errors


def _agent_logs(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    directory = Path(result["trial_dir"]) / "agent"
    logs = []
    try:
        for path in sorted(directory.rglob("*")):
            if native._safe_evidence_file(path, directory):
                logs.append({"path": str(path), "digest": native._file_digest(path), "bytes": path.stat().st_size})
    except OSError as exc:
        logs.append({"path": str(directory), "error": f"agent log inventory unavailable: {exc}"})
    return logs


def run_candidate(
    task: Path | str,
    files: Files | None = None,
    *,
    patch: str | Path | None = None,
    solution_bundle: Path | str | None = None,
    target_dir: str | None = None,
    patch_strip: int = 1,
    grader: Grader = "upstream",
    port_task: Path | str | None = None,
    backend: native.Backend = "auto",
    platform: str | None = None,
    timeout_seconds: int = 600,
    max_usd: float | None = None,
    approval: str | None = None,
    spend_ledger: Path | str | None = None,
    modal_app_name: str | None = None,
    billing_tag: str | None = None,
    output_dir: Path | str | None = None,
    plan: bool = False,
) -> dict[str, Any]:
    """Return a native grade or explicit unmeasured refusal/error.

    Mapping keys and directory members are relative to target_dir. str values
    are UTF-8 content; bytes are preserved. ``patch`` str is content, not a host
    filename. Pass Path for a patch file. Unified patches support regular-file
    add/change/delete hunks, not renames, binary data, or symlink creation.
    Existing inputs/evidence are never overwritten. Single-input refusals with
    unsafe evidence paths cannot write evidence and return output_dir=None.
    """
    output = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "runs" / ("candidate-" + uuid.uuid4().hex)).resolve()
    )
    request = {"task": task, "files": files, "patch": patch, "solution_bundle": solution_bundle, "port_task": port_task}
    try:
        if not isinstance(plan, bool):
            raise native.GraderRefusal("plan must be a boolean")
        _check_locations(output, _input_roots(request), spend_ledger)
    except (OSError, ValueError, TypeError) as exc:
        return _empty_result("refused", f"{type(exc).__name__}: {exc}", planned=bool(plan))
    prepared: _Prepared | None = None
    created = False
    result: dict[str, Any]
    try:
        prepared = _prepare(
            task, files=files, patch=patch, solution_bundle=solution_bundle,
            target_dir=target_dir, patch_strip=patch_strip, grader=grader, port_task=port_task,
            backend=backend, platform=platform, timeout_seconds=timeout_seconds,
        )
        planned = _plan_result(prepared, grader, backend)
        if plan:
            return planned
        output.mkdir(parents=True)
        created = True
        _atomic_json(output / "candidate-plan.json", planned)
        if not planned["runnable"]:
            raise native.GraderRefusal("; ".join(planned["refusals"]))
        solution = _stage_payload(prepared, output)
        native_task = prepared.task
        adapter = prepared.adapter
        if grader == "port":
            from evallab.candidate_port import prepare_port_task

            assert port_task is not None
            adapter = prepare_port_task(prepared.task, port_task, output / "port-task")
            native_task = Path(adapter["task_dir"])
            if prepared.adapter is not None and adapter["grader_digest"] != prepared.adapter["grader_digest"]:
                raise native.GraderRefusal("fixed grader changed between description and preparation")
        static = native.plan_upstream_grader(
            native_task, blank=True, backend="auto", platform=platform, timeout_seconds=timeout_seconds
        )
        images = _source_images(static)
        drift = _drift(prepared)
        if drift:
            raise native.GraderRefusal("; ".join(drift))
        with _local_lane(backend):
            observed = native.run_upstream_grader(
                native_task, solution,
                backend=backend, platform=platform, timeout_seconds=timeout_seconds,
                max_usd=max_usd, approval=approval, spend_ledger=spend_ledger,
                modal_app_name=modal_app_name, billing_tag=billing_tag,
                output_dir=output / "native",
            )
        result = dict(observed)
        images["observed"] = [
            {"role": item.get("role"), **item["native_image_identity"]}
            for item in observed.get("resource_observations", [])
            if isinstance(item.get("native_image_identity"), dict)
        ]
        result.update(
            schema="evallab.candidate.result/v1",
            native_schema=observed["schema"],
            native_output_dir=observed["output_dir"],
            output_dir=str(output),
            grader=grader,
            task=planned["task"],
            native_task=observed["task"],
            input=prepared.payload.identity,
            native_input=observed["input"],
            external_dependencies=prepared.external_dependencies,
            grader_provenance=adapter,
            images=images,
            target_dir=prepared.target,
            native_error=observed.get("error"),
            passed=None,
            pass_semantics="no generic threshold is inferred from native scalar/continuous reward; inspect reward and per_test",
            logs={"agent": _agent_logs(observed), "verifier": observed.get("raw_verifier_evidence", [])},
            cost={
                "reservation_cap_usd": max_usd,
                "ledger": str(Path(spend_ledger).expanduser().resolve()) if spend_ledger is not None else None,
                "native_spend": observed.get("spend"),
                "actual_invoice_usd": None,
                "semantics": "native conservative infrastructure reservations/estimates, not provider invoices or a billing kill switch",
            },
        )
        drift = _drift(prepared)
        if drift:
            result.update(status="error", reward=None, rewards=None, passed=None)
            result["error"] = "; ".join(filter(None, [result.get("error"), *drift]))
    except KeyboardInterrupt:
        result = _empty_result("cancelled", "candidate interrupted; inspect native evidence for cleanup")
    except (native.GraderRefusal, OSError, ValueError, TypeError) as exc:
        result = _empty_result("refused" if not output.exists() or isinstance(exc, native.GraderRefusal) else "error", f"{type(exc).__name__}: {exc}", planned=plan)
    except Exception as exc:
        result = _empty_result("error", f"{type(exc).__name__}: {exc}", planned=plan)
    if plan:
        return result
    if not created:
        try:
            output.mkdir(parents=True)
        except FileExistsError:
            result["evidence_error"] = "evidence directory appeared concurrently; refusing to overwrite it"
            return result
    result["output_dir"] = str(output)
    result.setdefault("grader", grader)
    if prepared is not None:
        result.setdefault("input", prepared.payload.identity)
        result.setdefault("external_dependencies", prepared.external_dependencies)
        result.setdefault("grader_provenance", prepared.adapter)
    _atomic_json(output / "candidate.json", result)
    return result


def run_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    max_concurrency: int = 2,
    backend: native.Backend = "auto",
    platform: str | None = None,
    timeout_seconds: int = 600,
    max_usd: float | None = None,
    approval: str | None = None,
    spend_ledger: Path | str | None = None,
    modal_app_name: str | None = None,
    billing_tag: str | None = None,
    output_dir: Path | str | None = None,
    plan: bool = False,
) -> list[dict[str, Any]]:
    """One ordered outcome per input, including refusals/errors/unstarted work.

    Each worker commits candidate.json and outcomes/INDEX.json atomically before
    returning. batch.json is an ordered checkpoint, not the sole result store.
    On Ctrl-C pending futures are cancelled; already-running native trials are
    allowed to finish their watchdog/cleanup and preserve their real outcomes.
    SIGKILL cannot produce a return value, but never removes completed receipts.
    """
    if isinstance(candidates, str | bytes) or not isinstance(candidates, Sequence):
        raise native.GraderRefusal("candidates must be an ordered sequence of request mappings")
    requests = [dict(item) if isinstance(item, Mapping) else item for item in candidates]
    try:
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise native.GraderRefusal("max_concurrency must be a positive integer")
        if not isinstance(plan, bool):
            raise native.GraderRefusal("plan must be a boolean")
        output = (
            Path(output_dir).expanduser().resolve()
            if output_dir is not None
            else (Path.cwd() / "runs" / ("candidate-batch-" + uuid.uuid4().hex)).resolve()
        )
        roots = []
        for request in requests:
            if isinstance(request, Mapping):
                roots.extend(_input_roots(request, ignore_invalid=True))
        _check_locations(output, roots, spend_ledger)
    except (OSError, ValueError, TypeError) as exc:
        return [
            {**_empty_result("refused", f"{type(exc).__name__}: {exc}", planned=bool(plan)), "batch_index": index}
            for index in range(len(requests))
        ]
    shared: dict[str, Any] = {
        "backend": backend, "platform": platform, "timeout_seconds": timeout_seconds,
        "max_usd": max_usd, "approval": approval, "spend_ledger": spend_ledger,
        "modal_app_name": modal_app_name, "billing_tag": billing_tag, "plan": plan,
    }
    results = [_empty_result("unstarted", "candidate has not started", planned=plan) for _ in requests]
    if not plan:
        output.mkdir(parents=True)
        (output / "outcomes").mkdir()

    def checkpoint() -> None:
        if not plan:
            _atomic_json(output / "batch.json", {"schema": "evallab.candidate.batch/v1", "results": results})

    def worker(index: int) -> dict[str, Any]:
        request = requests[index]
        directory = output / f"{index:05d}"
        try:
            if not isinstance(request, Mapping):
                raise native.GraderRefusal("each candidate must be a request mapping")
            unknown = set(request) - _INPUT_FIELDS
            if unknown:
                raise native.GraderRefusal("candidate cannot override batch policy; unknown fields: " + ", ".join(sorted(unknown)))
            if "task" not in request:
                raise native.GraderRefusal("candidate task is required")
            outcome = run_candidate(**request, **shared, output_dir=directory)
        except KeyboardInterrupt:
            outcome = _empty_result("cancelled", "candidate worker interrupted", planned=plan)
        except (OSError, ValueError, TypeError) as exc:
            outcome = _empty_result("refused", f"{type(exc).__name__}: {exc}", planned=plan)
        except Exception as exc:
            outcome = _empty_result("error", f"{type(exc).__name__}: {exc}", planned=plan)
        outcome["batch_index"] = index
        if not plan:
            directory.mkdir(exist_ok=True)
            outcome["output_dir"] = str(directory)
            _atomic_json(directory / "candidate.json", outcome)
            _atomic_json(output / "outcomes" / f"{index:05d}.json", outcome)
        return outcome

    checkpoint()
    # Plans are deliberately serial: no environments are being allocated.
    if plan:
        return [worker(index) for index in range(len(requests))]
    executor = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="candidate")
    futures: dict[Future[dict[str, Any]], int] = {}
    interrupted = False
    try:
        for index in range(len(requests)):
            futures[executor.submit(worker, index)] = index
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            checkpoint()
    except KeyboardInterrupt:
        interrupted = True
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=interrupted)
        by_index = {index: future for future, index in futures.items()}
        for index in range(len(requests)):
            future = by_index.get(index)
            if future is None or future.cancelled():
                outcome = _empty_result("unstarted", "batch interrupted before this candidate started")
                outcome["batch_index"] = index
                directory = output / f"{index:05d}"
                directory.mkdir(exist_ok=True)
                outcome["output_dir"] = str(directory)
                _atomic_json(directory / "candidate.json", outcome)
                _atomic_json(output / "outcomes" / f"{index:05d}.json", outcome)
                results[index] = outcome
            else:
                results[index] = future.result()
        checkpoint()
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?")
    parser.add_argument("--request", help="single JSON object, or - for stdin")
    parser.add_argument("--batch", help="JSON array of candidate requests, or - for stdin")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--files-dir", help="artifact directory; solve.sh is never implicitly executed")
    inputs.add_argument("--files-json", help="JSON relative-path/string-content mapping")
    inputs.add_argument("--patch-file", help="real UTF-8 unified patch, applied only inside the task")
    inputs.add_argument("--solution-bundle", help="explicit native solve.sh execution bundle")
    parser.add_argument("--target-dir")
    parser.add_argument("--patch-strip", type=int)
    parser.add_argument("--grader", choices=("upstream", "port"))
    parser.add_argument("--port-task")
    parser.add_argument("--backend", choices=("auto", "docker", "modal"))
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--approval")
    parser.add_argument("--spend-ledger")
    parser.add_argument("--modal-app-name")
    parser.add_argument("--billing-tag")
    parser.add_argument("--output-dir", help="fresh evidence directory (or fresh batch root)")
    parser.add_argument("--plan", action="store_true", default=None)
    return parser


def _json_input(path: str) -> Any:
    return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.request and args.batch:
            raise native.GraderRefusal("--request and --batch are mutually exclusive")
        request = _json_input(args.request) if args.request else {}
        if not isinstance(request, dict):
            raise native.GraderRefusal("single request must be a JSON object")
        flags = {
            name: getattr(args, name)
            for name in (
                "task", "target_dir", "patch_strip", "grader", "port_task", "backend", "platform",
                "timeout_seconds", "max_usd", "approval", "spend_ledger", "modal_app_name",
                "billing_tag", "output_dir", "plan",
            )
            if getattr(args, name) is not None
        }
        modes = [name for name in ("files_dir", "files_json", "patch_file", "solution_bundle") if getattr(args, name) is not None]
        if args.batch and (modes or any(name in flags for name in _INPUT_FIELDS)):
            raise native.GraderRefusal("batch input modes belong in each JSON candidate; policy is batch-wide")
        request.update(flags)
        if args.files_dir:
            request["files"] = args.files_dir
        if args.files_json:
            request["files"] = _json_input(args.files_json)
        if args.patch_file:
            request["patch"] = Path(args.patch_file)
        if args.solution_bundle:
            request["solution_bundle"] = args.solution_bundle
        with contextlib.redirect_stdout(sys.stderr):
            if args.batch:
                results = run_candidates(_json_input(args.batch), max_concurrency=args.max_concurrency, **request)
                result: dict[str, Any] | list[dict[str, Any]] = results
            else:
                if "task" not in request:
                    raise native.GraderRefusal("task directory is required")
                result = run_candidate(**request)
        outcomes = result if isinstance(result, list) else [result]
        if any(item.get("status") in {"cancelled", "unstarted"} for item in outcomes):
            code = 130
        elif any(item.get("status") == "error" for item in outcomes):
            code = 1
        elif any(item.get("status") == "refused" or item.get("runnable") is False for item in outcomes):
            code = 2
        else:
            code = 1 if any(item.get("error") for item in outcomes) else 0
    except (OSError, ValueError, TypeError) as exc:
        result = _empty_result("refused", f"{type(exc).__name__}: {exc}")
        code = 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return code


if __name__ == "__main__":
    from importlib import import_module

    raise SystemExit(import_module("evallab.candidate_runner").main())
