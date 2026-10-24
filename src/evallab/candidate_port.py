"""Offline PM-port materialization for the native Harbor grader.

The original task still supplies the agent image, instructions and artifact
contract. A separate verifier uses the complete port Containerfile, locked
Python environments, original setup-data bytes and the port's own Step/judge.
Nothing here executes setup code, candidates, providers or container tools on
the host. Static admission is not a claim that an image has built or run.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tomllib
import zipfile
import zlib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import tomlkit

from evallab.upstream_grader import GraderRefusal, _resources, _tree_digest

PM_FIXED_PIN = "a3ec9e22725b490a30b35d127bc9e7ac8a28de82"
UPSTREAM_EXPORT_PIN = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
_DATA_DIRS = ("student_data", "shared_data", "root_data", "intermediate_data", "image_data")
_COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
_PROVIDER_IMPORTS = {"openai", "anthropic", "litellm", "google.genai", "google.generativeai"}

# Runs only inside the dedicated native verifier image. It calls the actual
# Step pre-scoring custody hook and exact ExecutableJudge argv, never a selected
# assertion or fixture label. The port's outer CLI reports infrastructure
# crashes as score=0; these known wrapper failures deliberately produce no
# Harbor reward, so infrastructure does not become a measured candidate miss.
_DRIVER = r'''from __future__ import annotations
import json
import math
import os
import shutil
import subprocess
import traceback
from pathlib import Path, PurePosixPath

LOG = Path("/logs/verifier")


def transfer(source: Path, destination: Path) -> None:
    if not source.exists() and not source.is_symlink():
        return  # Missing submissions must reach the real scorer as missing.
    for entry in (source, *source.rglob("*")) if source.is_dir() else (source,):
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise RuntimeError(f"unsupported submission file type: {entry}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def source_for(destination: Path, artifacts: list[str]) -> Path:
    relative = destination.relative_to(Path("/workdir"))
    matches = []
    for source in artifacts:
        original = PurePosixPath(source)
        if original.parts[1:] == ("app", *relative.parts):
            matches.append(Path(source))
        elif original.parts[1:] == ("workdir", *relative.parts):
            matches.append(Path(source))
        elif original == PurePosixPath("/app"):
            matches.append(Path(source) / relative)
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        # Port submission dirs need not mirror the upstream artifact dir
        # (e.g. upstream collects /results/gsea_stats.tsv while the port
        # expects /workdir/output/gsea_stats.tsv). A unique basename match
        # is an unambiguous binding; anything else stays an infrastructure
        # error at runtime, never a synthetic grade.
        by_name = [Path(source) for source in artifacts if PurePosixPath(source).name == destination.name]
        if len(by_name) == 1:
            return by_name[0]
    raise RuntimeError(f"no unambiguous native artifact binding for {destination}")


def records(metadata: dict, prefix: str = "") -> list[dict]:
    result = []
    checks = metadata.get("checks")
    if isinstance(checks, dict):
        for name, value in checks.items():
            if value == "passed" or (isinstance(value, str) and value.startswith("failed:")):
                result.append({"name": prefix + str(name), "status": "passed" if value == "passed" else "failed", "message": value})
    failed = metadata.get("failed_tests")
    if isinstance(failed, dict):
        result.extend({"name": prefix + str(name), "status": "failed", "message": str(value)} for name, value in failed.items())
    for key in ("ok", "deterministic"):
        if isinstance(metadata.get(key), bool):
            result.append({"name": prefix + key, "status": "passed" if metadata[key] else "failed", "message": str(metadata[key])})
    for key, value in metadata.items():
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    label = item.get("variant", item.get("kind", index))
                    result.extend(records(item, prefix + f"{key}/{label}/"))
    return result


def main() -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    settings = json.loads(Path("/tests/candidate-port-config.json").read_text())
    from karotte import EvaluationRunConfig
    from karotte.judges import ExecutableJudge
    from environment import get_tasks
    config = EvaluationRunConfig(run_id="native-candidate-port", task_id=settings["task_id"], model="candidate-no-model", use_fake_model=True, save_artifacts=False, extra_config={})
    classes = [task for task in get_tasks() if task.id == settings["task_id"]]
    if len(classes) != 1:
        raise RuntimeError("the pinned port must expose exactly one matching Task")
    task = classes[0](config=config)
    if task.required_hardware != settings["required_hardware"]:
        raise RuntimeError("runtime port hardware differs from admitted metadata")
    steps = list(task.steps)
    if len(steps) != 1:
        raise RuntimeError("multi-step port scoring is not admitted")
    step = steps[0]
    paths = step.submission_paths
    if not paths:
        raise RuntimeError("the fixed scorer has no file submission contract")
    bindings = []
    for destination in paths:
        source = source_for(destination, settings["artifacts"])
        bindings.append({"source": str(source), "destination": str(destination), "present": source.exists()})
        if source != destination:
            transfer(source, destination)
    (LOG / "port-submission-bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")
    # Do not run task.pre_hook(): native Harbor already ran the candidate in
    # the original environment, and a port pre-hook would replace its work.
    step.pre_scoring_hook()
    judge = step.judge
    if type(judge) is not ExecutableJudge:
        raise RuntimeError("only the pinned file-based ExecutableJudge is admitted")
    command = list(judge.subprocess_run_args)
    if not command or not all(isinstance(part, str) for part in command):
        raise RuntimeError("the fixed scorer command is not a string argv")
    command[-1] = str(LOG / "port-score.json")
    (LOG / "port-invocation.json").write_text(json.dumps({"mode": "full", "argv": command, "cwd": judge.cwd, "grader_digest": settings["grader_digest"]}, indent=2) + "\n")
    with (LOG / "port-stdout.txt").open("w") as stdout, (LOG / "port-stderr.txt").open("w") as stderr:
        completed = subprocess.run(command, cwd=judge.cwd, stdout=stdout, stderr=stderr, check=False)
    if completed.returncode:
        raise RuntimeError(f"the fixed scorer exited {completed.returncode}")
    payload = json.loads((LOG / "port-score.json").read_text())
    score, metadata = payload["score"], payload["metadata"]
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1 or not isinstance(metadata, dict):
        raise RuntimeError("the fixed scorer emitted an invalid score/metadata payload")
    error = metadata.get("error", "")
    if isinstance(error, str) and (error.startswith("Grading timed out") or error.startswith("Grading crashed") or error.startswith("Grader timed out") or error.startswith("Grader crashed")):
        raise RuntimeError(f"fixed scorer infrastructure failure: {error}")
    # Only explicitly named results are normalized. Aggregate pass counts do
    # not create invented passing cases; the lossless full metadata survives.
    tests = records(metadata)
    (LOG / "ctrf.json").write_text(json.dumps({"results": {"tool": {"name": "pm-fixed-port-full"}, "tests": tests}, "port": {"mode": "full", "metadata": metadata, "per_test_scope": "explicitly named scorer results; aggregate counts retained in metadata"}}, indent=2) + "\n")
    (LOG / "reward.json").write_text(json.dumps({"reward": score}) + "\n")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        LOG.mkdir(parents=True, exist_ok=True)
        (LOG / "port-infrastructure-error.txt").write_text(traceback.format_exc())
        raise
'''
_TEST_SH = "#!/bin/bash\nset -euo pipefail\nexec /root/.venv/bin/python /tests/candidate-port-driver.py\n"
_IMAGE_SUFFIX = "\n# Native fixed-port replay; no candidate bytes are baked into this image.\nCOPY candidate-port-driver.py candidate-port-config.json test.sh /tests/\nUSER root\nWORKDIR /root\n"


_NOCAPS_STEP = "RUN printf 'tsflags=nocaps\\n' >> /etc/dnf/dnf.conf  # Modal builder cannot set file capabilities; see _image_dockerfile"


def _image_dockerfile(text: str) -> str:
    """Make a dnf-based port Containerfile buildable on Modal.

    Modal's image builder cannot apply rpm file capabilities
    (``cpio: cap_set_file failed - Operation not supported`` when
    shadow-utils installs newgidmap/newuidmap), so the image never builds.
    ``tsflags=nocaps`` tells dnf/rpm to skip setting file capabilities at
    install time. That drops only the cap_setgid/cap_setuid metadata on
    those two helpers (used solely by rootless user namespaces); every
    package, file, scoring script and assertion is installed unchanged,
    and the verifier runs as root and demotes with setuid syscalls
    directly. Local Docker builds get the same image definition.
    """
    if "dnf " not in text:
        return text
    lines = []
    for line in text.splitlines():
        lines.append(line)
        if line.lstrip().upper().startswith("FROM "):
            lines.append(_NOCAPS_STEP)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _gitkeep_only(source: Path, assets: list[dict[str, str]], relative: str) -> bool:
    """True when a build-context dir would vanish from Modal's context sync.

    Modal uploads drop dotfiles, so a directory containing only ``.gitkeep``
    (the port's four data dirs and ``.karotte`` in source form) arrives as
    "source path does not exist" and its COPY fails. Prepared assets can
    fill such a dir for real; only still-empty ones need the rewrite.
    """
    if not source.is_dir():
        return False
    if any(not entry.name.startswith(".") for entry in source.rglob("*") if entry.is_file()):
        return False
    return not any(asset["destination"].startswith(relative.rstrip("/") + "/") for asset in assets)


def _gitkeep_rewrites(text: str, port: Path, assets: list[dict[str, str]]) -> list[str]:
    """COPY sources (relative dirs) rewritten to their ``.gitkeep`` file."""
    rewritten = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0].upper() != "COPY" or "--from" in line:
            continue
        args = [part for part in parts[1:] if not part.startswith("--")]
        if len(args) < 2:
            continue
        for source in args[:-1]:
            relative = source.rstrip("/")
            if _gitkeep_only(port / relative, assets, relative):
                rewritten.append(relative)
    return rewritten


def _copy_gitkeep_form(text: str, port: Path, assets: list[dict[str, str]]) -> str:
    """Replace COPYs of dotfile-only dirs with an mkdir of the destination.

    Modal's context sync drops dotfiles entirely (even an explicit
    ``COPY dir/.gitkeep`` arrives as "source path does not exist"), while
    Docker would copy exactly one zero-byte placeholder. Creating the
    destination directory instead reproduces the only grading-visible
    effect (the directory exists, owned as the COPY declared); the
    ``.gitkeep`` placeholder itself is a repo bookkeeping file, not task
    data, and is absent in the Modal image.
    """
    rewritten = set(_gitkeep_rewrites(text, port, assets))
    if not rewritten:
        return text
    out = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].upper() == "COPY" and "--from" not in line:
            args = [part for part in parts[1:] if not part.startswith("--")]
            flags = [part for part in parts[1:] if part.startswith("--")]
            if len(args) == 2 and args[0].rstrip("/") in rewritten:
                destination = args[1].rstrip("/")
                chown = next((flag.split("=", 1)[1] for flag in flags if flag.startswith("--chown=")), None)
                line = f"RUN mkdir -p {destination}" + (f" && chown {chown} {destination}" if chown else "")
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _read_config(task: Path) -> dict[str, Any]:
    try:
        return tomllib.loads((task / "task.toml").read_text())
    except (OSError, ValueError) as exc:
        raise GraderRefusal(f"invalid original native task: {exc}") from exc


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise GraderRefusal(f"unsafe setup-data path: {value!r}")
    return path


class _RecipeReturn(Exception):
    """Internal: a setup body's top-level return ends interpretation."""


class _HelperReturn:
    def __init__(self, value: Any) -> None:
        self.value = value


class _SetupRecipe:
    """Interpret pinned data declarations and bounded deterministic transforms.

    External bytes are resolved only from an explicitly supplied hash-keyed
    cache. No setup Python, network request, or ambient cache lookup runs here.
    """

    def __init__(
        self,
        tree: ast.Module,
        task_id: str,
        upstream: Path | None = None,
        *,
        asset_cache: Path | str | None = None,
        port_context: Path | None = None,
    ) -> None:
        self.upstream = Path(upstream) if upstream is not None else None
        self.asset_cache = Path(asset_cache).expanduser().absolute() if asset_cache is not None else None
        self.port_context = Path(port_context) if port_context is not None else None
        self.external_inputs: list[dict[str, str]] = []
        self.directories: dict[str, int] = {}
        self.cleared_directories: list[str] = []
        # Deterministic local transforms write into this virtual staged FS;
        # reads resolve only to hash-pinned staged inputs or earlier writes.
        self.generated: dict[str, dict[str, Any]] = {}
        self.deletions: list[str] = []
        self.names: dict[str, Any] = {"ctx": {name: PurePosixPath(name) for name in _DATA_DIRS}}
        # Port recipes that address data dirs through module constants
        # (ROOT / "intermediate_data", INTERMEDIATE / "file") resolve offline
        # to the same relative data-dir paths as ctx attributes.
        self.names["ROOT"] = PurePosixPath("")
        self.helpers = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name not in {"setup", "main"}}
        # Some ports stage into extra ROOT-relative dirs declared as
        # DataContext fields (e.g. mysql_seed); they are data dirs too.
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "DataContext":
                for field in node.body:
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name) and field.value is not None:
                        with contextlib.suppress(ValueError, TypeError, KeyError):
                            value = self.expression(field.value)
                            if isinstance(value, PurePosixPath) and len(value.parts) == 1:
                                self.names["ctx"][field.target.id] = value
        self.task_id = task_id
        self.assets: list[dict[str, str]] = []
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
                if isinstance(target, ast.Name) and statement.value is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        self.names[target.id] = ast.literal_eval(statement.value)
                    if target.id not in self.names or isinstance(self.names.get(target.id), ast.AST):
                        with contextlib.suppress(ValueError, TypeError, KeyError):
                            value = self.expression(statement.value)
                            if isinstance(value, (str, PurePosixPath, dict, tuple, list, int, float)):
                                self.names[target.id] = value
        self.helpers = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name not in {"setup", "main"}}
        setup = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "setup"), None)
        if setup is None:
            self._declared_file_maps(tree)
        elif len(setup.args.args) != 1:
            raise GraderRefusal("setup_data.py has no single-argument offline setup recipe")
        else:
            self.names[setup.args.args[0].arg] = self.names["ctx"]
            with contextlib.suppress(_RecipeReturn):
                self.statements(setup.body)
        if not self.assets and not self.generated:
            raise GraderRefusal("setup_data.py declared no locally resolvable hash-pinned assets")

    @staticmethod
    def _pinned_digest(digest: str) -> str:
        if not isinstance(digest, str):
            raise GraderRefusal("setup-data asset requires an exact sha256")
        value = digest.removeprefix("sha256:")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise GraderRefusal("setup-data asset requires an exact lowercase sha256")
        return value

    @staticmethod
    @contextlib.contextmanager
    def _asset_file(source: Path) -> Iterator[BinaryIO]:
        """Open only a regular file, without following the final path link."""
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise GraderRefusal(f"pinned setup asset is not a regular file: {source}")
                yield handle
        except OSError as exc:
            raise GraderRefusal(f"cannot read pinned setup asset without following symlinks: {source}: {exc}") from exc

    @staticmethod
    def _asset_bytes(source: Path, digest: str) -> bytes:
        with _SetupRecipe._asset_file(source) as handle:
            data = handle.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise GraderRefusal(f"local setup asset differs from pinned sha256:{digest}: {source}")
        return data

    def _cached_asset(self, digest: str) -> Path:
        """Resolve only explicit cache/<bare-sha256>, checking every lookup."""
        digest = self._pinned_digest(digest)
        if self.asset_cache is None:
            raise GraderRefusal(
                f"setup-data recipe requires nonlocal/transforming external asset sha256:{digest}; "
                f"supply explicit asset_cache containing {digest}; no download is attempted"
            )
        source = self.asset_cache / digest
        if self.asset_cache.is_symlink() or source.is_symlink():
            raise GraderRefusal(f"offline asset cache rejects symlinks for sha256:{digest}: {source}")
        if not self.asset_cache.is_dir() or not source.is_file():
            raise GraderRefusal(f"offline asset cache needs regular file sha256:{digest} at {source}; no download is attempted")
        with self._asset_file(source) as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
                raise GraderRefusal(f"local setup asset differs from pinned sha256:{digest}: {source}")
        return source

    def _cached_bytes(self, digest: str) -> tuple[Path, bytes]:
        digest = self._pinned_digest(digest)
        source = self._cached_asset(digest)
        return source, self._asset_bytes(source, digest)

    def _data_path(self, value: Any) -> PurePosixPath:
        if not isinstance(value, PurePosixPath):
            raise GraderRefusal("setup-data destination must be a port data-directory path")
        relative = _relative(str(value))
        if not relative.parts or relative.parts[0] not in {str(path) for path in self.names["ctx"].values()}:
            raise GraderRefusal("setup-data destination is outside the port data directories")
        return relative

    def _fetch_external(self, url: Any, destination: Any, digest: Any) -> None:
        if not isinstance(url, str) or not url:
            raise GraderRefusal("external setup-data asset has no source URL")
        relative = self._data_path(destination)
        if len(relative.parts) < 2:
            raise GraderRefusal("external setup-data file cannot replace a data-directory root")
        if self._pinned_digest(digest) != digest:
            raise GraderRefusal("ctx.fetch requires an exact bare lowercase sha256")
        source = self._cached_asset(digest)
        self.generated.pop(str(relative), None)
        self.assets[:] = [asset for asset in self.assets if asset["destination"] != str(relative)]
        self.assets.append({
            "source": str(source), "source_kind": "asset_cache", "source_url": url,
            "destination": str(relative), "sha256": digest,
        })

    @staticmethod
    def _tar_keep_filter(node: ast.AST | None) -> str:
        if node is None:
            return "all"
        if isinstance(node, ast.Lambda) and len(node.args.args) == 1:
            name = node.args.args[0].arg
            for expression, label in (
                (f"lambda {name}: True", "all"),
                (f"lambda {name}: not Path({name}.name).name.startswith('._')", "exclude-appledouble-basename"),
            ):
                expected = ast.parse(expression, mode="eval").body
                if ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False):
                    return label
        raise GraderRefusal("ctx.fetch_tar requires an explicitly supported deterministic keep filter")

    def _fetch_tar(self, call: ast.Call) -> None:
        if len(call.args) != 3 or len(call.keywords) > 1 or any(keyword.arg != "keep" for keyword in call.keywords):
            raise GraderRefusal("unsupported ctx.fetch_tar arguments")
        keep = self._tar_keep_filter(call.keywords[0].value if call.keywords else None)
        url, digest, destination = map(self.expression, call.args)
        if not isinstance(url, str) or not url:
            raise GraderRefusal("external setup-data archive has no source URL")
        destination = self._data_path(destination)
        if self._pinned_digest(digest) != digest:
            raise GraderRefusal("ctx.fetch_tar requires an exact bare lowercase sha256")
        source, data = self._cached_bytes(digest)
        marker = f".extracted-{digest[:16]}"
        marker_path = str(destination / marker)
        if marker_path in self.generated:
            provenance = self.generated[marker_path].get("provenance", {})
            if provenance.get("kind") == "tar-extraction-marker" and provenance.get("archive_sha256") == digest:
                return
            raise GraderRefusal("setup archive has a preexisting extraction marker without verified extraction custody")
        if any(asset["destination"] == marker_path for asset in self.assets):
            raise GraderRefusal("setup archive has a preexisting extraction marker without verified extraction custody")
        if self.port_context is not None and (self.port_context / marker_path).exists():
            raise GraderRefusal("setup archive has a preexisting extraction marker without verified extraction custody")
        files: dict[str, tuple[bytes, int]] = {}
        directories: set[str] = {str(destination)}
        seen: dict[str, str] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as bundle:
                for member in bundle:
                    path = PurePosixPath(member.name)
                    if (
                        not member.name or path.is_absolute() or ".." in path.parts
                        or "\\" in member.name or "\x00" in member.name
                    ):
                        raise GraderRefusal(f"unsafe setup archive member path: {member.name!r}")
                    name = str(path)
                    if not (member.isfile() or member.isdir()) or member.sparse is not None:
                        raise GraderRefusal(f"setup archive contains a link/special/non-regular member: {member.name!r}")
                    if member.isfile() and (member.name.endswith("/") or member.name.rsplit("/", 1)[-1] == "."):
                        raise GraderRefusal(f"setup archive regular member has a directory-shaped path: {member.name!r}")
                    if member.mode < 0 or member.mode & ~0o777:
                        raise GraderRefusal(f"setup archive contains unsafe permission bits: {member.name!r}")
                    if name in seen or (path.parts and path.parts[0] == marker) or any(parent.as_posix() in seen and seen[parent.as_posix()] == "file" for parent in path.parents):
                        raise GraderRefusal(f"setup archive member overwrite/conflict: {member.name!r}")
                    if member.isfile() and (name == "." or any(other.startswith(name + "/") for other in seen)):
                        raise GraderRefusal(f"setup archive member overwrite/conflict: {member.name!r}")
                    seen[name] = "dir" if member.isdir() else "file"
                    # Validate every member, including entries excluded by the
                    # known filter; a filter cannot hide a dangerous archive.
                    if keep == "exclude-appledouble-basename" and path.name.startswith("._"):
                        continue
                    target = destination if name == "." else destination / path
                    for parent in (target, *target.parents):
                        if parent == destination or parent.is_relative_to(destination):
                            if member.isdir() or parent != target:
                                directories.add(str(parent))
                    if member.isfile():
                        handle = bundle.extractfile(member)
                        if handle is None:
                            raise GraderRefusal(f"setup archive cannot read regular member: {member.name!r}")
                        with handle:
                            content = handle.read()
                        if len(content) != member.size:
                            raise GraderRefusal(f"setup archive member size differs: {member.name!r}")
                        # Python tarfile's filter="data": never retain
                        # group/other write or an execute bit not owned by user.
                        mode = member.mode & 0o755
                        if not mode & 0o100:
                            mode &= ~0o111
                        files[str(target)] = (content, mode | 0o600)
        except (tarfile.TarError, EOFError) as exc:
            raise GraderRefusal("external setup asset is not a readable pinned tar archive") from exc
        if self.port_context is not None:
            for relative in (destination, *destination.parents):
                if str(relative) == ".":
                    continue
                existing = self.port_context / relative
                if existing.exists() and not existing.is_dir():
                    raise GraderRefusal(f"setup archive destination conflicts with a port file: {relative}")
        prefix = str(destination)
        self.assets[:] = [asset for asset in self.assets if asset["destination"] != prefix and not asset["destination"].startswith(prefix + "/")]
        for mapping in (self.generated, self.directories):
            for name in [name for name in mapping if name == prefix or name.startswith(prefix + "/")]:
                del mapping[name]
        self.cleared_directories.append(prefix)
        self.directories.update({name: 0o755 for name in directories})
        provenance = {"kind": "tar-member", "source_url": url, "archive_sha256": digest, "filter": keep}
        for name, (content, mode) in files.items():
            self._write_staged(PurePosixPath(name), content, mode)
            self.generated[name]["provenance"] = {**provenance, "member": str(PurePosixPath(name).relative_to(destination))}
        self._write_staged(destination / marker, b"", 0o644)
        self.generated[str(destination / marker)]["provenance"] = {**provenance, "kind": "tar-extraction-marker"}
        self.external_inputs.append({
            "source": str(source), "source_kind": "asset_cache", "source_url": url, "sha256": digest,
            "destination": str(destination), "operation": "fetch_tar", "filter": keep,
        })

    def _read_staged(self, relative: PurePosixPath) -> bytes:
        key = str(relative)
        if key in self.generated:
            return self.generated[key]["content"]
        for asset in self.assets:
            if asset["destination"] == key:
                if asset.get("source_kind") == "asset_cache":
                    return self._cached_bytes(asset["sha256"])[1]
                if self.upstream is None:
                    break
                source = self.upstream / asset["source"]
                if any(path.is_symlink() for path in (source, *source.parents) if path == self.upstream or path.is_relative_to(self.upstream)):
                    raise GraderRefusal(f"pinned setup asset uses a symlink: {asset['source']}")
                return self._asset_bytes(source, asset["sha256"])
        raise GraderRefusal(f"setup transform reads a file that is not a pinned staged input: {key}")

    def _write_staged(self, relative: PurePosixPath, data: bytes, mode: int | None = None) -> None:
        relative = _relative(str(relative))
        if relative.parts[0] not in {str(value) for value in self.names["ctx"].values()}:
            raise GraderRefusal("setup transform writes outside the port data directories")
        entry = {"destination": str(relative), "sha256": hashlib.sha256(data).hexdigest(), "content": data}
        if mode is not None:
            entry["mode"] = mode
        self.generated[str(relative)] = entry

    def _extract_archive(self, archive: PurePosixPath, destination: PurePosixPath, expected: tuple[str, ...] = ()) -> None:
        """Safely extract a pinned local archive into generated staged files.

        Same contract as the ports' unzip/unpack helpers: regular files
        only, no symlinks or path escapes; member modes are recorded.
        """
        data = self._read_staged(archive)
        members: dict[str, tuple[bytes, int | None]] = {}
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                for info in bundle.infolist():
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0o7777
                    if mode & 0o170000 == 0o120000:
                        raise GraderRefusal("setup archive contains a symlink member")
                    _relative(info.filename)
                    # The ports' unzip helper normalizes member modes to
                    # 0755 (any exec bit) or 0644; archives like the GSEA
                    # JDK store 0777, which the port's own permission check
                    # rejects as world-writable.
                    members[info.filename] = (bundle.read(info.filename), 0o755 if mode & 0o111 else 0o644)
        else:
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as bundle:
                    for member in bundle.getmembers():
                        if member.isdir():
                            continue
                        if not member.isfile():
                            raise GraderRefusal("setup archive contains a non-file member")
                        _relative(member.name)
                        handle = bundle.extractfile(member)
                        assert handle is not None
                        members[member.name] = (handle.read(), (member.mode & 0o777) or None)
            except tarfile.TarError as exc:
                raise GraderRefusal("setup transform input is not a readable pinned archive") from exc
        for name, (content, mode) in sorted(members.items()):
            self._write_staged(destination / name, content, mode)
        for name in expected:
            if str(destination / name) not in self.generated:
                raise GraderRefusal(f"pinned archive lacks expected member {name}")

    def _fetch_image_files(self, repo: Any, layer: Any, files: Any, registry: Any = "registry-1.docker.io") -> None:
        """Select exact regular members from a hash-pinned offline OCI blob.

        Like the source helper, this copies bytes into fresh files rather than
        extracting archive permissions. No image, registry or Docker operation
        is performed, and every selected member is checked before staging.
        """
        if not isinstance(repo, str) or not repo or not isinstance(registry, str) or not registry:
            raise GraderRefusal("setup image-layer fetch requires an explicit repository and registry")
        if not isinstance(layer, str) or not layer.startswith("sha256:") or len(layer) != 71 or any(char not in "0123456789abcdef" for char in layer[7:]):
            raise GraderRefusal("setup image-layer fetch requires an exact sha256 layer identity")
        if not isinstance(files, dict) or not files:
            raise GraderRefusal("setup image-layer fetch requires a nonempty pinned member map")
        selected: dict[str, tuple[PurePosixPath, str]] = {}
        destinations: set[str] = set()
        member_paths: set[str] = set()
        for name, entry in files.items():
            if not isinstance(name, str) or not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise GraderRefusal("setup image-layer member has no safe destination and exact sha256")
            member = _relative(name)
            destination, digest = entry
            if not member.parts or str(member) in member_paths:
                raise GraderRefusal("setup image-layer member map has ambiguous paths")
            member_paths.add(str(member))
            if not isinstance(destination, PurePosixPath):
                raise GraderRefusal("setup image-layer member has no safe destination and exact sha256")
            destination = _relative(str(destination))
            if len(destination.parts) < 2 or destination.parts[0] not in {str(value) for value in self.names["ctx"].values()}:
                raise GraderRefusal("setup image-layer member writes outside the port data directories")
            if str(destination) in destinations:
                raise GraderRefusal("setup image-layer members have ambiguous destinations")
            destinations.add(str(destination))
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise GraderRefusal("setup image-layer member has no exact sha256 identity")
            selected[name] = (destination, digest)
        blob = self._cached_asset(layer)
        contents: dict[str, bytes] = {}
        seen: set[str] = set()
        try:
            with blob.open("rb") as stream:
                # Bind the opened blob too: the cache path could have changed
                # after lookup, before this descriptor was opened.
                if hashlib.file_digest(stream, "sha256").hexdigest() != layer[7:]:
                    raise GraderRefusal("setup image-layer blob differs from pinned sha256")
                stream.seek(0)
                with tarfile.open(fileobj=stream, mode="r|*") as bundle:
                    for member in bundle:
                        relative = _relative(member.name)
                        if not relative.parts and not member.isdir():
                            raise GraderRefusal("setup image layer contains an unsafe empty member path")
                        key = str(relative)
                        if key in seen:
                            raise GraderRefusal(f"setup image layer contains duplicate or ambiguous member {member.name!r}")
                        seen.add(key)
                        if member.name not in selected:
                            continue
                        if not member.isfile():
                            raise GraderRefusal(f"setup image layer selected member is not a regular file: {member.name}")
                        source = bundle.extractfile(member)
                        if source is None:
                            raise GraderRefusal(f"setup image layer selected member is not readable: {member.name}")
                        with source:
                            content = source.read()
                        if hashlib.sha256(content).hexdigest() != selected[member.name][1]:
                            raise GraderRefusal(f"setup image-layer member differs from pinned sha256: {member.name}")
                        contents[member.name] = content
        except (tarfile.TarError, EOFError, zlib.error) as exc:
            raise GraderRefusal("setup image-layer blob is not a readable pinned tar archive") from exc
        missing = selected.keys() - contents.keys()
        if missing:
            raise GraderRefusal(f"setup image layer lacks selected member {sorted(missing)[0]}")
        for name, (destination, digest) in selected.items():
            self._write_staged(destination, contents[name])
            self.generated[str(destination)]["provenance"] = {
                "kind": "oci-layer-member", "registry": registry, "repository": repo,
                "layer": layer, "member": name, "sha256": digest,
            }

    def _add_asset(self, source: Any, destination: Any, digest: Any) -> None:
        if not isinstance(source, str) or not isinstance(digest, str):
            raise GraderRefusal("setup-data asset has no safe destination and exact sha256")
        relative = _relative(str(destination))
        _relative(source)
        if relative.parts[0] not in {str(value) for value in self.names["ctx"].values()} or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise GraderRefusal("setup-data asset has no safe destination and exact sha256")
        self.assets.append({"source": source, "destination": str(relative), "sha256": digest})

    def _declared_file_maps(self, tree: ast.Module) -> None:
        """Port recipes with no setup(ctx): a declared {source: (dest, sha)} map.

        Some ports (e.g. vf2) ship a FILES dict and a network main() instead
        of the DataContext convention. The dict itself is the same
        hash-pinned copy declaration, so it resolves offline identically.
        """
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
                continue
            if not isinstance(statement.value, ast.Dict):
                continue
            try:
                mapping = self.expression(statement.value)
            except (ValueError, TypeError, KeyError):
                continue
            if not isinstance(mapping, dict) or not mapping:
                continue
            if all(isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], PurePosixPath) for value in mapping.values()):
                for source, (destination, digest) in mapping.items():
                    self._add_asset(source, destination, digest)
            elif all(isinstance(value, str) and len(value) == 64 for value in mapping.values()) and "destination" in self.helpers and len(self.helpers["destination"].args.args) == 1:
                # Flat {hub_path: sha} map plus a pure destination() helper
                # (a main()-style recipe); destinations resolve offline.
                destination_helper = ast.Call(func=ast.Name(id="destination", ctx=ast.Load()), args=[ast.Constant(value="")], keywords=[])
                for source, digest in mapping.items():
                    destination_helper.args[0] = ast.Constant(value=source)
                    self._add_asset(source, self._call_helper(destination_helper), digest)

    def expression(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.names:
            return self.names[node.id]
        if isinstance(node, ast.Attribute):
            base = self.expression(node.value)
            if isinstance(base, dict) and node.attr in base:
                return base[node.attr]
            if isinstance(base, PurePosixPath) and node.attr in {"parts", "name", "parent"}:
                return getattr(base, node.attr)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            items: list[Any] = []
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    items.extend(self.expression(item.value))
                else:
                    items.append(self.expression(item))
            if isinstance(node, ast.Tuple):
                return tuple(items)
            if isinstance(node, ast.Set):
                return set(items)
            return items
        if isinstance(node, ast.Dict):
            result: dict[Any, Any] = {}
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    result.update(self.expression(value))
                else:
                    result[self.expression(key)] = self.expression(value)
            return result
        if isinstance(node, ast.Subscript):
            value = self.expression(node.value)
            if isinstance(node.slice, ast.Slice):
                lower = self.expression(node.slice.lower) if node.slice.lower is not None else None
                upper = self.expression(node.slice.upper) if node.slice.upper is not None else None
                step = self.expression(node.slice.step) if node.slice.step is not None else None
                return value[slice(lower, upper, step)]
            return value[self.expression(node.slice)]
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for item in node.values:
                if isinstance(item, ast.FormattedValue):
                    parts.append(str(self.expression(item.value)))
                elif isinstance(item, ast.Constant):
                    parts.append(str(item.value))
                else:
                    raise GraderRefusal("unsupported f-string segment in setup-data recipe")
            return "".join(parts)
        if isinstance(node, ast.BinOp):
            left, right = self.expression(node.left), self.expression(node.right)
            if isinstance(node.op, ast.Div) and isinstance(left, PurePosixPath) and isinstance(right, str):
                return left / _relative(right)
            if isinstance(node.op, ast.BitOr) and isinstance(left, dict) and isinstance(right, dict):
                return left | right
            if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(node.op, ast.Mult) and isinstance(left, (str, int, float)) and isinstance(right, (int, float)):
                return left * right
            if isinstance(node.op, (ast.Add, ast.Sub)) and isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left + right if isinstance(node.op, ast.Add) else left - right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "items" and not node.args and not node.keywords:
            value = self.expression(node.func.value)
            if isinstance(value, dict):
                return tuple(value.items())
        if isinstance(node, ast.DictComp):
            result: dict[Any, Any] = {}
            def _walk_dict(index: int) -> None:
                if index == len(node.generators):
                    result[self.expression(node.key)] = self.expression(node.value)
                    return
                generator = node.generators[index]
                if generator.is_async:
                    raise GraderRefusal("conditional/async setup-data comprehension is unsupported")
                for value in self.expression(generator.iter):
                    self.assign(generator.target, value)
                    if all(self.expression(condition) for condition in generator.ifs):
                        _walk_dict(index + 1)
            _walk_dict(0)
            return result
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            values: list[Any] = []
            def _walk(index: int) -> None:
                if index == len(node.generators):
                    values.append(self.expression(node.elt))
                    return
                generator = node.generators[index]
                if generator.is_async:
                    raise GraderRefusal("conditional/async setup-data comprehension is unsupported")
                for value in self.expression(generator.iter):
                    self.assign(generator.target, value)
                    if all(self.expression(condition) for condition in generator.ifs):
                        _walk(index + 1)
            _walk(0)
            return set(values) if isinstance(node, ast.SetComp) else values
        if isinstance(node, ast.Compare):
            left = self.expression(node.left)
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                right = self.expression(comparator)
                outcome = self._compare(operator, left, right)
                if not outcome:
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            values = [self.expression(value) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not self.expression(node.operand)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) and isinstance(self.expression(node.operand), (int, float)):
            value = self.expression(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.IfExp):
            return self.expression(node.body if self.expression(node.test) else node.orelse)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"Path", "str", "int", "float", "list", "tuple", "set", "len", "sorted", "range", "chr"} and not node.keywords:
            args = [self.expression(arg) for arg in node.args]
            if node.func.id == "Path" and len(args) == 1:
                return args[0] if isinstance(args[0], PurePosixPath) else PurePosixPath(str(args[0]))
            if node.func.id == "len" and len(args) == 1:
                return len(args[0])
            if node.func.id == "sorted" and len(args) == 1:
                return sorted(args[0])
            if node.func.id == "range":
                return list(range(*args))
            if node.func.id == "chr":
                return chr(*args)
            constructor = {"str": str, "int": int, "float": float, "list": list, "tuple": tuple, "set": set}[node.func.id]
            return constructor(*args)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in {"json", "re", "zlib"}:
            args = [self.expression(arg) for arg in node.args]
            if node.func.value.id == "json" and node.func.attr == "dumps":
                kwargs = {kw.arg: self.expression(kw.value) for kw in node.keywords if kw.arg}
                return json.dumps(args[0], **kwargs)
            if node.func.value.id == "json" and node.func.attr == "loads":
                return json.loads(args[0])
            if node.func.value.id == "re" and node.func.attr == "compile" and len(args) == 1:
                return args[0]
            if node.func.value.id == "re" and node.func.attr == "findall" and len(args) == 2:
                import re as _re
                return _re.findall(args[0], args[1])
            if node.func.value.id == "zlib" and node.func.attr == "crc32" and len(args) == 1:
                return zlib.crc32(args[0])
            raise GraderRefusal("unsupported setup-data library call")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "hexdigest" and isinstance(node.func.value, ast.Call) and isinstance(node.func.value.func, ast.Attribute) and node.func.value.func.attr == "sha256" and isinstance(node.func.value.func.value, ast.Name) and node.func.value.func.value.id == "hashlib" and not node.keywords:
            return hashlib.sha256(self.expression(node.func.value.args[0])).hexdigest()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes"} and not node.args and not node.keywords:
            target = self.expression(node.func.value)
            if isinstance(target, PurePosixPath):
                data = self._read_staged(target)
                return data.decode() if node.func.attr == "read_text" else data
            raise GraderRefusal("setup transform reads a file that is not a pinned staged input")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"any", "all"} and len(node.args) == 1 and not node.keywords:
            values = self.expression(node.args[0])
            return any(values) if node.func.id == "any" else all(values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_sha256" and len(node.args) == 1 and not node.keywords:
            target = self.expression(node.args[0])
            if isinstance(target, PurePosixPath):
                import hashlib as _hashlib
                return _hashlib.sha256(self._read_staged(target)).hexdigest()
            raise GraderRefusal("setup transform reads a file that is not a pinned staged input")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.helpers and not node.keywords:
            return self._call_helper(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.keywords:
            base = self.expression(node.func.value)
            args = []
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    args.extend(self.expression(arg.value))
                else:
                    args.append(self.expression(arg))
            if isinstance(base, str) and node.func.attr == "findall" and len(args) == 1:
                import re as _re
                return _re.findall(base, args[0])
            if isinstance(base, str) and node.func.attr == "sub" and len(args) == 2:
                import re as _re
                return _re.sub(base, args[0], args[1])
            if isinstance(base, str) and node.func.attr == "split" and len(args) <= 1:
                return base.split(*args)
            if isinstance(base, str) and node.func.attr == "join" and len(args) == 1:
                return base.join(args[0])
            if isinstance(base, str) and node.func.attr in {"startswith", "removeprefix", "strip"} and len(args) <= 1:
                return getattr(base, node.func.attr)(*args)
            if isinstance(base, str) and node.func.attr in {"replace", "count", "index"} and 1 <= len(args) <= 3:
                return getattr(base, node.func.attr)(*args)
            if isinstance(base, dict) and node.func.attr == "get" and 1 <= len(args) <= 2:
                return base.get(*args)
            if isinstance(base, PurePosixPath) and node.func.attr == "is_dir" and not args:
                key = str(base)
                return any(dest == key or dest.startswith(key + "/") for dest in list(self.generated) + [asset["destination"] for asset in self.assets])
            if isinstance(base, PurePosixPath) and node.func.attr in {"is_file", "exists"} and not args:
                key = str(base)
                present = key in self.generated or any(asset["destination"] == key for asset in self.assets)
                return present if node.func.attr == "is_file" else present or key in _DATA_DIRS
            if isinstance(base, PurePosixPath) and node.func.attr == "joinpath" and all(isinstance(arg, str) for arg in args):
                result = base
                for arg in args:
                    result = result / _relative(arg)
                return result
        raise GraderRefusal(f"setup-data recipe needs unsupported expression {type(node).__name__}: {ast.unparse(node)[:80]}")

    @staticmethod
    def _compare(operator: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
        if isinstance(operator, ast.In):
            return left in right
        if isinstance(operator, ast.NotIn):
            return left not in right
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        if isinstance(operator, ast.GtE):
            return left >= right
        if isinstance(operator, ast.Is):
            return left is right
        if isinstance(operator, ast.IsNot):
            return left is not right
        raise GraderRefusal("unsupported setup-data comparison")

    def _call_helper(self, node: ast.Call) -> Any:
        """Evaluate a module-local pure helper (Assign/If/Return only).

        Recipe files use small destination/predicate helpers next to the
        fetch declarations. Only helpers whose bodies are assignments,
        conditionals and a return of supported expressions are callable;
        anything with side-effecting statements refuses as before.
        """
        assert isinstance(node.func, ast.Name)
        function = self.helpers[node.func.id]
        params = [arg.arg for arg in function.args.args]
        defaults = [self.expression(item) for item in function.args.defaults]
        args = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                args.extend(self.expression(arg.value))
            else:
                args.append(self.expression(arg))
        if len(args) > len(params) or len(args) < len(params) - len(defaults):
            raise GraderRefusal("unsupported setup-data helper arity")
        bindings = dict(zip(params, args, strict=False))
        if len(args) < len(params):
            bindings.update(zip(params[len(args):], defaults[-(len(params) - len(args)):], strict=True))
        saved = self.names
        self.names = {**saved, **bindings}
        try:
            result = self._helper_body(function.body, depth=0)
        finally:
            self.names = saved
        if isinstance(result, _HelperReturn):
            return result.value
        return None

    def _helper_body(self, statements: list[ast.stmt], depth: int) -> Any:
        if depth > 8:
            raise GraderRefusal("setup-data helper nesting is too deep")
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                if statement.value is None:
                    continue
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                value = self.expression(statement.value)
                for target in targets:
                    self.assign(target, value)
            elif isinstance(statement, ast.If):
                branch = statement.body if self.expression(statement.test) else statement.orelse
                result = self._helper_body(branch, depth + 1)
                if isinstance(result, _HelperReturn):
                    return result
            elif isinstance(statement, ast.Return):
                return _HelperReturn(self.expression(statement.value) if statement.value is not None else None)
            elif isinstance(statement, ast.Raise):
                raise GraderRefusal("setup-data helper raised; recipe is not offline-resolvable")
            elif isinstance(statement, ast.For) and not statement.orelse:
                for value in self.expression(statement.iter):
                    self.assign(statement.target, value)
                    result = self._helper_body(statement.body, depth + 1)
                    if isinstance(result, _HelperReturn):
                        return result
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                self._side_effect_call(statement.value)
            elif isinstance(statement, ast.Assert):
                if not self.expression(statement.test):
                    raise GraderRefusal("setup transform digest check failed")
            elif isinstance(statement, ast.Pass):
                continue
            else:
                raise GraderRefusal(f"setup-data recipe requires unsupported statement {type(statement).__name__}: {ast.unparse(statement)[:80]}")
        return None

    def assign(self, target: ast.AST, value: Any) -> None:
        if isinstance(target, ast.Name):
            self.names[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)) and len(target.elts) == len(value):
            for item, part in zip(target.elts, value, strict=True):
                self.assign(item, part)
        elif isinstance(target, ast.Subscript):
            self.expression(target.value)[self.expression(target.slice)] = value
        else:
            raise GraderRefusal("unsupported setup-data assignment")

    def _side_effect_call(self, call: ast.Call) -> None:
        """Filesystem effects of deterministic local transforms.

        Only paths under the port data directories, only content derived
        from pinned staged inputs or literals, and archive extraction is
        the safe member-checked primitive. External calls still refuse.
        """
        if isinstance(call.func, ast.Attribute):
            method = call.func.attr
            if isinstance(call.func.value, ast.Name) and call.func.value.id in {"ctx", "context"}:
                if method == "fetch" and len(call.args) == 3 and not call.keywords:
                    self._fetch_external(*map(self.expression, call.args))
                    return
                if method == "fetch_tar":
                    self._fetch_tar(call)
                    return
            if isinstance(call.func.value, ast.Name) and call.func.value.id == "shutil":
                args = [self.expression(arg) for arg in call.args]
                if method == "copyfile" and len(args) == 2 and all(isinstance(arg, PurePosixPath) for arg in args):
                    self._write_staged(args[1], self._read_staged(args[0]))
                    return
                if method == "rmtree" and args and isinstance(args[0], PurePosixPath):
                    prefix = str(args[0])
                    self.deletions.append(prefix)
                    for key in [key for key in self.generated if key == prefix or key.startswith(prefix + "/")]:
                        del self.generated[key]
                    return
                raise GraderRefusal(f"setup-data recipe requires nonlocal/transforming call shutil.{method}")
            target = self.expression(call.func.value)
            if isinstance(target, PurePosixPath):
                args = [self.expression(arg) for arg in call.args]
                if method in {"write_text", "write_bytes"} and len(args) == 1:
                    data = args[0].encode() if isinstance(args[0], str) else args[0]
                    if not isinstance(data, bytes):
                        raise GraderRefusal("setup transform writes a non-bytes value")
                    self._write_staged(target, data)
                    return
                if method == "mkdir":
                    return
                if method == "touch" and not args:
                    key = str(target)
                    if key not in self.generated and not any(asset["destination"] == key for asset in self.assets):
                        self._write_staged(target, b"")
                    return
                if method == "chmod" and len(args) == 1 and isinstance(args[0], int):
                    if str(target) in self.generated:
                        self.generated[str(target)]["mode"] = args[0] & 0o777
                    return
            raise GraderRefusal(f"setup-data recipe requires nonlocal/transforming call {ast.unparse(call.func)}")
        if isinstance(call.func, ast.Name) and call.func.id in self.helpers:
            helper_source = ast.unparse(self.helpers[call.func.id])
            args = [self.expression(arg) for arg in call.args]
            if ("tarfile" in helper_source or "zipfile" in helper_source) and len(args) >= 2 and all(isinstance(arg, PurePosixPath) for arg in args[:2]):
                expected = tuple(args[2]) if len(args) > 2 and isinstance(args[2], (tuple, list)) else ()
                self._extract_archive(args[0], args[1], expected)
                return
            self._call_helper(call)
            return
        raise GraderRefusal(f"setup-data recipe requires nonlocal/transforming call {ast.unparse(call.func)}")

    def statements(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                if statement.value is None:
                    continue
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                value = self.expression(statement.value)
                for target in targets:
                    self.assign(target, value)
            elif isinstance(statement, ast.AugAssign) and isinstance(statement.op, ast.BitOr):
                self.expression(statement.target).update(self.expression(statement.value))
            elif isinstance(statement, ast.For) and not statement.orelse:
                for value in self.expression(statement.iter):
                    self.assign(statement.target, value)
                    self.statements(statement.body)
            elif isinstance(statement, ast.If):
                # Only taken branches are interpreted; an untaken branch's
                # external/transforming calls never become assets.
                self.statements(statement.body if self.expression(statement.test) else statement.orelse)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "update" and len(call.args) == 1 and not call.keywords:
                    self.expression(call.func.value).update(self.expression(call.args[0]))
                elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id in {"ctx", "context"} and call.func.attr == "fetch_image_files":
                    if len(call.args) not in {3, 4} or len(call.keywords) > 1 or any(keyword.arg != "registry" for keyword in call.keywords) or len(call.args) == 4 and call.keywords:
                        raise GraderRefusal("setup image-layer fetch requires repo, layer, files and optional registry")
                    repo, layer, files = map(self.expression, call.args[:3])
                    registry = self.expression(call.args[3]) if len(call.args) == 4 else self.expression(call.keywords[0].value) if call.keywords else "registry-1.docker.io"
                    self._fetch_image_files(repo, layer, files, registry)
                elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id in {"ctx", "context"} and call.func.attr == "fetch_hub" and len(call.args) == 3 and not call.keywords:
                    task, revision, assets = map(self.expression, call.args)
                    if task != self.task_id or revision != "4.0.0" or not isinstance(assets, dict):
                        raise GraderRefusal("setup-data Hub task/revision differs from the matched original v4 task")
                    for source, (destination, digest) in assets.items():
                        self._add_asset(source, destination, digest)
                elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id in {"ctx", "context"} and call.func.attr == "fetch" and len(call.args) == 3 and not call.keywords and isinstance(call.args[0], ast.Call) and isinstance(call.args[0].func, ast.Attribute) and call.args[0].func.attr == "hub_url" and isinstance(call.args[0].func.value, ast.Name) and call.args[0].func.value.id in {"ctx", "context"} and len(call.args[0].args) == 3:
                    # ctx.fetch(ctx.hub_url(task, rev, path), dest, sha) is the
                    # per-file form of fetch_hub: same hub object, resolved
                    # offline from the local upstream tree and hash-checked.
                    task, revision, source = map(self.expression, call.args[0].args)
                    if task != self.task_id or revision != "4.0.0":
                        raise GraderRefusal("setup-data Hub task/revision differs from the matched original v4 task")
                    self._add_asset(source, self.expression(call.args[1]), self.expression(call.args[2]))
                else:
                    self._side_effect_call(call)
            elif isinstance(statement, ast.Assert):
                if not self.expression(statement.test):
                    raise GraderRefusal("setup transform digest check failed")
            elif isinstance(statement, ast.Return):
                raise _RecipeReturn()
            else:
                raise GraderRefusal(f"setup-data recipe requires unsupported statement {type(statement).__name__}: {ast.unparse(statement)[:80]}")


def _source_pin(root: Path) -> str | None:
    """Read a nearby checkout pin without invoking git or assuming an export is git."""
    for base in (root, root.parent):
        marker = base / ".git"
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            text = marker.read_text().strip()
            if not text.startswith("gitdir: "):
                continue
            git_dir = (base / text[8:]).resolve()
        else:
            continue
        head = (git_dir / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head if len(head) == 40 else None
        ref = head[5:]
        candidate = git_dir / ref
        if candidate.is_file():
            return candidate.read_text().strip()
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for row in packed.read_text().splitlines():
                if not row.startswith(("#", "^")) and row.endswith(" " + ref):
                    return row.split()[0]
    return None


def _interface(port: Path, task_id: str) -> dict[str, Any]:
    task_modules = sorted((port / "src/environment/tasks").glob("*/__init__.py"))
    matches = []
    for module in task_modules:
        tree = ast.parse(module.read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(isinstance(base, ast.Name) and base.id == "Task" for base in node.bases):
                ids = [value.value for assignment in node.body if isinstance(assignment, ast.Assign) for target in assignment.targets if isinstance(target, ast.Name) and target.id == "id" for value in [assignment.value] if isinstance(value, ast.Constant)]
                if task_id in ids:
                    matches.append((module, tree, node))
    if len(matches) != 1:
        raise GraderRefusal("port must contain exactly one Task with the original task identity")
    module, tree, task_class = matches[0]
    hardware = [statement.value.value for method in task_class.body if isinstance(method, ast.FunctionDef) and method.name == "required_hardware" for statement in method.body if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)]
    if len(hardware) != 1:
        raise GraderRefusal("port required_hardware must be an explicit literal resource declaration")
    declarations = {}
    for entry in hardware[0].split(","):
        key, separator, value = entry.partition("=")
        if not separator or key in declarations or key not in {"cpus", "memory_mb", "storage_mb", "gpus", "gpu_types"}:
            raise GraderRefusal("unsupported port hardware declaration")
        declarations[key] = value if key == "gpu_types" else int(value)
    if not all(key in declarations for key in ("cpus", "memory_mb", "storage_mb")):
        raise GraderRefusal("port must explicitly declare CPU, memory and storage")
    judges = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("Judge")}
    if judges != {"ExecutableJudge"}:
        raise GraderRefusal("custom/composite/model-backed port judge is not a qualified native replay shape")
    # Only dependency interfaces are inspected, never assertion bodies/findings.
    for source in sorted((port / "src/environment").rglob("*.py")):
        source_tree = ast.parse(source.read_text())
        imports = {alias.name for node in ast.walk(source_tree) if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module or "" for node in ast.walk(source_tree) if isinstance(node, ast.ImportFrom))
        if any(name == provider or name.startswith(provider + ".") for name in imports for provider in _PROVIDER_IMPORTS):
            raise GraderRefusal("fixed scorer imports an external model/provider dependency; separate authorization and an explicit adapter are required")
    constants: dict[str, int | float] = {}

    def number(node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        if isinstance(node, ast.BinOp):
            left, right = number(node.left), number(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Mult):
                return left * right
        raise ValueError("not a numeric resource declaration")

    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = number(node.value)
            except ValueError:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    timeout = constants.get("GRADING_TIMEOUT_S", constants.get("GRADER_TIMEOUT_S"))
    return {
        "module": ".".join(module.relative_to(port / "src").parts[:-1]),
        "required_hardware": hardware[0],
        "hardware": declarations,
        "declared_grading_timeout_sec": timeout,
    }


def _describe(
    upstream_task: Path | str,
    port_task: Path | str,
    *,
    asset_cache: Path | str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    upstream, port = Path(upstream_task).resolve(), Path(port_task).resolve()
    if not upstream.is_dir() or not port.is_dir() or upstream == port:
        raise GraderRefusal("original and port must be distinct existing task directories")
    config = _read_config(upstream)
    task_id = (config.get("task") or {}).get("name", upstream.name).rsplit("/", 1)[-1]
    if task_id != upstream.name or port.name != task_id:
        raise GraderRefusal("original task, port directory and declared task identity must match")
    if config.get("steps"):
        raise GraderRefusal("multi-step original tasks need an explicit artifact/lifecycle adapter")
    # A Compose file in the original *agent* environment is admitted: the
    # native grader runs it only on Modal (DinD inside the declared-resource
    # sandbox) and refuses it for local Docker. Compose files in the original
    # tests or in the port context itself stay refused: they would change
    # how the separate verifier environment itself is detected/launched.
    for directory in (upstream / "tests", port):
        if any((directory / name).exists() for name in _COMPOSE_NAMES):
            raise GraderRefusal("Compose/service task shapes are not supported by the strict native candidate runner")
    for required in ("Containerfile", "setup_data.py", "pyproject.toml", "uv.lock", ".manifest.json"):
        if not (port / required).is_file():
            raise GraderRefusal(f"port runtime dependency is missing: {required}")
    if not (upstream / "instruction.md").is_file() or not (upstream / "environment/Dockerfile").is_file() and not (config.get("environment") or {}).get("docker_image"):
        raise GraderRefusal("original native instructions/environment definition is missing")
    upstream_digest, port_digest = _tree_digest(upstream), _tree_digest(port)
    port_pin = _source_pin(port)
    if port_pin is not None and port_pin != PM_FIXED_PIN:
        raise GraderRefusal(f"port checkout is not the approved fixed-scorer pin {PM_FIXED_PIN}: {port_pin}")
    interface = _interface(port, task_id)
    agent_raw = config.get("environment") or {}
    agent_resource = _resources(agent_raw, role="agent")
    verifier_raw = dict(interface["hardware"])
    verifier_raw["build_timeout_sec"] = agent_resource["build_timeout_sec"]
    verifier_resource = _resources(verifier_raw, role="verifier")
    raw_artifacts = config.get("artifacts")
    # Harbor also declares artifacts as {source, service} / {source, exclude}
    # mappings; the port binding needs only the source path (the prepared
    # task.toml keeps the original mapping, so Harbor still collects it
    # with its service/exclude semantics).
    artifacts = []
    if isinstance(raw_artifacts, list):
        for entry in raw_artifacts:
            if isinstance(entry, str):
                artifacts.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("source"), str):
                artifacts.append(entry["source"])
    if not artifacts or len(artifacts) != len(raw_artifacts if isinstance(raw_artifacts, list) else []) or not all(PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts for value in artifacts):
        raise GraderRefusal("original task must declare absolute file/directory artifact sources")
    # Artifacts under /app or /workdir bind by path; any other absolute
    # artifact dir (e.g. /results) binds at runtime only by unique basename
    # against the port's submission paths, and ambiguous basenames refuse.
    _basenames = [PurePosixPath(value).name for value in artifacts]
    if len(set(_basenames)) != len(_basenames):
        raise GraderRefusal("native artifact basenames are ambiguous; an explicit port binding is required")
    recipe = _SetupRecipe(
        ast.parse((port / "setup_data.py").read_text()), task_id, upstream=upstream,
        asset_cache=asset_cache, port_context=port,
    )
    seen: dict[str, str] = {}
    cached_contents: dict[str, bytes] = {}
    for asset in recipe.assets:
        if asset["destination"] in seen and seen[asset["destination"]] != asset["sha256"]:
            raise GraderRefusal("setup-data recipe has conflicting destination identities")
        seen[asset["destination"]] = asset["sha256"]
        if asset.get("source_kind") == "asset_cache":
            _, cached_contents[asset["destination"]] = recipe._cached_bytes(asset["sha256"])
            continue
        source = upstream / asset["source"]
        if not source.is_file() or source.is_symlink():
            raise GraderRefusal(f"pinned setup asset is unavailable locally: {asset['source']}")
        recipe._asset_bytes(source, asset["sha256"])
    source_pin = _source_pin(upstream)
    generated = sorted(
        ({
            "destination": entry["destination"], "sha256": entry["sha256"],
            **({"mode": entry["mode"]} if "mode" in entry else {}),
            **({"provenance": entry["provenance"]} if "provenance" in entry else {}),
        } for entry in recipe.generated.values()),
        key=lambda entry: entry["destination"],
    )
    directories = [{"destination": name, "mode": mode} for name, mode in sorted(recipe.directories.items())]
    files = {asset["destination"] for asset in recipe.assets} | set(recipe.generated)
    required_directories = set(recipe.directories)
    for name in files:
        required_directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
    if files & required_directories:
        raise GraderRefusal("setup-data recipe has conflicting file/directory destinations")
    for name in files | required_directories:
        existing = port / name
        if any(name == prefix or name.startswith(prefix + "/") for prefix in recipe.cleared_directories):
            continue
        if existing.exists() and existing.is_dir() != (name in required_directories):
            raise GraderRefusal(f"setup-data destination conflicts with the immutable port layout: {name}")

    def asset_identity(asset: dict[str, str]) -> dict[str, str]:
        return {**asset, "source": "sha256:" + asset["sha256"]} if asset.get("source_kind") == "asset_cache" else asset

    identity: dict[str, Any] = {
        "port_tree": port_digest,
        "assets": sorted((asset_identity(asset) for asset in recipe.assets), key=lambda asset: (asset["destination"], asset["source"])),
        "generated": generated,
    }
    if recipe.external_inputs:
        identity["external_inputs"] = sorted((asset_identity(asset) for asset in recipe.external_inputs), key=_json)
    if directories:
        identity["directories"] = directories
    if recipe.cleared_directories:
        identity["cleared_directories"] = sorted(set(recipe.cleared_directories))
    grader_digest = _sha(_json(identity).encode())
    runtime = {"task_id": task_id, "required_hardware": interface["required_hardware"], "artifacts": artifacts, "grader_digest": grader_digest}
    containerfile_text = (port / "Containerfile").read_text()
    staged_paths = [*recipe.assets, *generated, *directories]
    generated_dockerfile = _copy_gitkeep_form(_image_dockerfile(containerfile_text), port, staged_paths) + _IMAGE_SUFFIX
    logical_image = _sha(_json({"grader_digest": grader_digest, "driver": _sha(_DRIVER.encode()), "config": runtime, "test_sh": _TEST_SH, "dockerfile": generated_dockerfile}).encode())
    result = {
        "schema": "evallab.candidate_port/v1", "task_dir": str(upstream), "upstream_task": str(upstream), "port_task": str(port), "task_id": task_id,
        "mode": "full", "grader_digest": grader_digest,
        "upstream_identity": {"path": str(upstream), "id": task_id, "exact_tree_digest": upstream_digest, "task_toml_sha256": _sha((upstream / "task.toml").read_bytes()), "source_pin": source_pin, "export_declared_pin": UPSTREAM_EXPORT_PIN if source_pin is None else None, "pin_verification": "git metadata" if source_pin else "export pin declaration; exact local bytes separately bound"},
        "port_identity": {"path": str(port), "id": task_id, "exact_tree_digest": port_digest, "source_pin": port_pin, "required_fixed_pin": PM_FIXED_PIN, "pin_verification": "git metadata" if port_pin else "no checkout metadata; exact local bytes bound, pin unverified", "containerfile_sha256": _sha((port / "Containerfile").read_bytes()), "lock_sha256": _sha((port / "uv.lock").read_bytes()), "manifest_sha256": _sha((port / ".manifest.json").read_bytes())},
        "resources": [agent_resource, verifier_resource], "port_interface": interface,
        "setup_data": {
            "mode": "offline exact hash-pinned copies", "asset_count": len(recipe.assets), "assets": recipe.assets, "generated": generated,
            **({"external_inputs": recipe.external_inputs} if recipe.external_inputs else {}),
            **({"directories": directories} if directories else {}),
            **({"cleared_directories": sorted(set(recipe.cleared_directories))} if recipe.cleared_directories else {}),
        },
        "native_image_identity": {
            "agent": {"source_context": str(upstream / "environment"), "exact_context_digest": _tree_digest(upstream / "environment"), "docker_image": agent_resource["docker_image"]},
            "verifier": {"source_context": str(port), "logical_digest": logical_image, "exact_context_digest": None, "materialization": "pending", "candidate_independent": True, "evidence_path_independent": True},
        },
        "support": {"static_admission": "candidate", "runtime_qualified": False, "local": "requires strict live Docker admission and a successful full-port image build", "modal": "requires upstream native platform/resource admission, one shared approval/cap/ledger and successful image hydration", "refusals": [], "runtime_network": "fixed verifier no-network; no provider credentials or model invocation", "limitations": ["Only one file-submission ExecutableJudge step; runtime checks exact original artifact bindings before scoring.", "Only explicitly supported deterministic setup-data recipes; external bytes require an explicit verified hash-keyed asset cache, never an automatic download.", "CPU/memory exact; declared storage is minimum free capacity, not a disk quota.", "Port metadata and explicitly named per-test results are retained; aggregate counts do not invent passing test identities."]},
        "_runtime": runtime,
        "_generated": [recipe.generated[entry["destination"]] for entry in generated],
        "_deletions": list(recipe.deletions),
        "_cached_contents": cached_contents,
        "_directories": directories,
        "_cleared_directories": sorted(set(recipe.cleared_directories)),
    }
    return result, recipe.assets


def describe_port_task(
    upstream_task: Path | str,
    port_task: Path | str,
    *,
    asset_cache: Path | str | None = None,
) -> dict[str, Any]:
    """Describe immutable source bytes; external assets need explicit cache/<sha256>."""
    try:
        result, _ = _describe(upstream_task, port_task, asset_cache=asset_cache)
    except (OSError, SyntaxError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GraderRefusal):
            raise
        raise GraderRefusal(f"invalid fixed-port package: {exc}") from exc
    result.pop("_runtime")
    result.pop("_generated", None)
    result.pop("_deletions", None)
    result.pop("_cached_contents", None)
    result.pop("_directories", None)
    result.pop("_cleared_directories", None)
    return result


def prepare_port_task(
    upstream_task: Path | str,
    port_task: Path | str,
    output_dir: Path | str,
    *,
    asset_cache: Path | str | None = None,
) -> dict[str, Any]:
    """Materialize exact pinned bytes without mutating sources or fetching assets."""
    try:
        result, assets = _describe(upstream_task, port_task, asset_cache=asset_cache)
        upstream, port, destination = Path(result["upstream_task"]), Path(result["port_task"]), Path(output_dir).resolve()
        sources = (upstream, port, Path(asset_cache).expanduser().resolve()) if asset_cache is not None else (upstream, port)
        if destination.exists() or any(destination == source or destination.is_relative_to(source) or source.is_relative_to(destination) for source in sources):
            raise GraderRefusal("port output_dir must be fresh and outside immutable source trees and the asset cache")
        runtime = result.pop("_runtime")
        generated = result.pop("_generated", [])
        deletions = result.pop("_deletions", [])
        cached_contents = result.pop("_cached_contents", {})
        directories = result.pop("_directories", [])
        cleared_directories = result.pop("_cleared_directories", [])
        # Resolve all refusal boundaries before creating any output bytes.
        config = tomlkit.parse((upstream / "task.toml").read_text())
        verifier = config.get("verifier", tomlkit.table())
        verifier["environment_mode"] = "separate"
        verifier["user"] = "root"
        grading_timeout = result["port_interface"]["declared_grading_timeout_sec"]
        if grading_timeout is not None:
            verifier["timeout_sec"] = grading_timeout
        verifier["network_mode"] = "no-network"
        verifier.pop("allowed_hosts", None)
        verifier_environment = tomlkit.table()
        for key, value in result["port_interface"]["hardware"].items():
            verifier_environment[key] = value
        verifier_environment["build_timeout_sec"] = result["resources"][1]["build_timeout_sec"]
        verifier_environment["network_mode"] = "no-network"
        verifier["environment"] = verifier_environment
        config["verifier"] = verifier
        destination.mkdir(parents=True)
        shutil.copytree(upstream / "environment", destination / "environment")
        shutil.copy2(upstream / "instruction.md", destination / "instruction.md")
        # Keep original solution assets for native --oracle, if present. They
        # never enter either image or the port's hidden scorer directory.
        if (upstream / "solution").is_dir():
            shutil.copytree(upstream / "solution", destination / "solution")
        (destination / "task.toml").write_text(tomlkit.dumps(config))
        tests = destination / "tests"
        shutil.copytree(port, tests)
        for name in cleared_directories:
            target = tests / name
            if target.is_dir():
                shutil.rmtree(target)
        for entry in sorted(directories, key=lambda entry: len(PurePosixPath(entry["destination"]).parts)):
            (tests / entry["destination"]).mkdir(parents=True, exist_ok=True)
        for asset in assets:
            target = tests / asset["destination"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if asset.get("source_kind") == "asset_cache":
                target.write_bytes(cached_contents[asset["destination"]])
                target.chmod(0o644)
            else:
                shutil.copy2(upstream / asset["source"], target)
        containerfile_text = (port / "Containerfile").read_text()
        (tests / "Dockerfile").write_text(_copy_gitkeep_form(_image_dockerfile(containerfile_text), port, [*assets, *generated, *directories]) + _IMAGE_SUFFIX)
        for entry in generated:
            target = tests / entry["destination"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry["content"])
            if "mode" in entry:
                target.chmod(entry["mode"])
        for entry in directories:
            (tests / entry["destination"]).chmod(entry["mode"])
        _final_destinations = {asset["destination"] for asset in assets} | {entry["destination"] for entry in generated} | {entry["destination"] for entry in directories}
        for prefix in deletions:
            if any(dest == prefix or dest.startswith(prefix + "/") for dest in _final_destinations):
                continue
            target = tests / prefix
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        (tests / "candidate-port-driver.py").write_text(_DRIVER)
        (tests / "candidate-port-config.json").write_text(_json(runtime) + "\n")
        (tests / "test.sh").write_text(_TEST_SH)
        for name in ("Dockerfile", "candidate-port-driver.py", "candidate-port-config.json"):
            (tests / name).chmod(0o644)
        (tests / "test.sh").chmod(0o755)
        result["task_dir"] = str(destination)
        result["native_image_identity"]["verifier"].update({"materialization": "prepared", "context": str(tests), "exact_context_digest": _tree_digest(tests)})
        result["native_image_identity"]["agent"]["context"] = str(destination / "environment")
        result["prepared_task_digest"] = _tree_digest(destination)
        return result
    except (OSError, SyntaxError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GraderRefusal):
            raise
        raise GraderRefusal(f"cannot prepare fixed-port package: {exc}") from exc
