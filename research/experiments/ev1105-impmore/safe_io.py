"""Defensive post-agent host I/O; callers serialize operations per owned container.

Only trusted controller configuration and trusted, non-mounted evidence/source paths
belong here. No task stdout contributes to the authoritative execution status.
"""
from __future__ import annotations

import difflib
import hashlib
import http.client
import io
import json
import os
import re
import socket
import stat
import struct
import subprocess
import time
import unicodedata
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

# Each capture shares these limits across BOTH trees. Overflow is an error.
MAX_ENTRIES = 40_000
MAX_TOTAL_BYTES = 384 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64
MAX_TEXT_LINES = 100_000
MAX_TEXT_LINE_CHARS = 65_536
TAIL_BYTES = 4000
EXEC_SECONDS = 900
JSON_BYTES = 1024 * 1024
EXCLUDED = frozenset({".git", "__pycache__", ".pytest_cache"})
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _parts(value: str) -> list[str]:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError("Expected a nonempty trusted relative path")
    parts = value.split("/")
    if len(parts) > MAX_DEPTH:
        raise ValueError("Path depth limit exceeded")
    for part in parts:
        if part in ("", ".", "..") or any(
            unicodedata.category(char).startswith("C") or char in "\u2028\u2029"
            for char in part
        ):
            raise ValueError("Unsafe path component")
    return parts


def _identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _snapshot(info):
    return (_identity(info), info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _unchanged(parent, name, before, *, content=False):
    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    key = _snapshot if content else _identity
    if key(before) != key(after):
        raise RuntimeError("Traversal identity or content changed")


@contextmanager
def _root(path: Path):
    """Open every absolute ancestor separately, never resolving a symlink."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Absolute path required; do not resolve untrusted paths")
    parts = _parts(str(path)[1:])
    descriptors = [os.open("/", DIR_FLAGS)]
    edges = []
    try:
        for name in parts:
            parent = descriptors[-1]
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            child = os.open(name, DIR_FLAGS, dir_fd=parent)
            descriptors.append(child)
            if _identity(before) != _identity(os.fstat(child)):
                raise RuntimeError("Ancestor changed during open")
            edges.append((parent, name, before))
        yield descriptors[-1]
        for parent, name, before in reversed(edges):
            _unchanged(parent, name, before)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_regular(parent, name, before, limit):
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("Nonregular file or file byte limit exceeded")
    descriptor = os.open(name, FILE_FLAGS, dir_fd=parent)
    try:
        if _snapshot(before) != _snapshot(os.fstat(descriptor)):
            raise RuntimeError("File changed during open")
        chunks, count = [], 0
        while True:
            chunk = os.read(descriptor, min(65_536, limit + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > limit:
                raise ValueError("File byte limit exceeded")
        if _snapshot(before) != _snapshot(os.fstat(descriptor)):
            raise RuntimeError("File changed during read")
        _unchanged(parent, name, before, content=True)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _tree(root, budget):
    entries = {}

    def walk(descriptor, prefix, depth):
        if depth > MAX_DEPTH:
            raise ValueError("Tree depth limit exceeded")
        initial = os.fstat(descriptor)
        names = []
        with os.scandir(descriptor) as iterator:
            for item in iterator:
                _parts(item.name)
                budget["entries"] += 1
                if budget["entries"] > MAX_ENTRIES:
                    raise ValueError("Tree entry limit exceeded")
                if item.name not in EXCLUDED:
                    names.append(item.name)
        for name in sorted(names):
            rel = prefix + name
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            mode = stat.S_IMODE(before.st_mode)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, DIR_FLAGS, dir_fd=descriptor)
                try:
                    if _snapshot(before) != _snapshot(os.fstat(child)):
                        raise RuntimeError("Directory changed during open")
                    entries[rel] = {"kind": "directory", "mode": mode}
                    walk(child, rel + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(before.st_mode):
                target = os.readlink(name, dir_fd=descriptor)
                budget["bytes"] += len(os.fsencode(target))
                entries[rel] = {"kind": "symlink", "target": target, "mode": mode}
            elif stat.S_ISREG(before.st_mode):
                remaining = MAX_TOTAL_BYTES - budget["bytes"]
                data = _read_regular(descriptor, name, before, min(MAX_FILE_BYTES, remaining))
                budget["bytes"] += len(data)
                entries[rel] = {"kind": "regular", "data": data, "mode": mode}
            else:
                raise ValueError("Sockets, FIFOs and devices are not submissions")
            if budget["bytes"] > MAX_TOTAL_BYTES:
                raise ValueError("Total tree byte limit exceeded")
            _unchanged(descriptor, name, before, content=True)
        if _snapshot(initial) != _snapshot(os.fstat(descriptor)):
            raise RuntimeError("Directory changed during traversal")

    walk(root, "", 0)
    return entries


def _metadata(entry):
    if entry is None:
        return None
    result = {key: value for key, value in entry.items() if key != "data"}
    if entry["kind"] == "regular":
        result.update(bytes=len(entry["data"]), sha256=hashlib.sha256(entry["data"]).hexdigest())
    return result


def _text(entry):
    if entry is None:
        return ""
    if entry["kind"] != "regular" or b"\0" in entry["data"]:
        return None
    try:
        text = entry["data"].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    # Treat non-source control bytes as binary, not misleading replacement text.
    if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text):
        return None
    return text


def _render(before, after):
    stream, size, changed = io.StringIO(), 0, []

    def emit(piece):
        nonlocal size
        size += len(piece.encode("utf-8"))
        if size > MAX_DIFF_BYTES:
            raise ValueError("Submission diff byte limit exceeded")
        stream.write(piece)

    for rel in sorted(before.keys() | after.keys()):
        old, new = before.get(rel), after.get(rel)
        if old == new:
            continue
        changed.append("b/" + rel)
        emit("diff --safe-io " + json.dumps("a/" + rel) + " " + json.dumps("b/" + rel) + "\n")
        emit("# metadata " + json.dumps({"before": _metadata(old), "after": _metadata(new)},
                                        sort_keys=True, ensure_ascii=True) + "\n")
        emit("--- a/" + rel + "\n+++ b/" + rel + "\n")
        old_text, new_text = _text(old), _text(new)
        if old_text is None or new_text is None:
            emit("# metadata-only: symlink, binary, directory, or file-type transition; no source text\n")
            continue
        # Unified source hunks use LF boundaries, not Unicode/CR line splitting.
        old_lines = io.StringIO(old_text).readlines()
        new_lines = io.StringIO(new_text).readlines()
        if (max(len(old_lines), len(new_lines)) > MAX_TEXT_LINES or
                any(len(line) > MAX_TEXT_LINE_CHARS for line in old_lines + new_lines)):
            raise ValueError("Source diff line limit exceeded")
        hunks = difflib.unified_diff(old_lines, new_lines, fromfile="a/" + rel, tofile="b/" + rel)
        for index, line in enumerate(hunks):
            if index < 2:
                continue
            emit(line)
            if not line.endswith("\n"):
                emit("\n\\ No newline at end of file\n")
    return stream.getvalue(), changed


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


def _socket_path(config):
    endpoint = config["docker_env"]["DOCKER_HOST"]
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise ValueError("Only a local absolute Docker Unix socket is permitted")
    path = endpoint[len("unix://"):]
    if any(char in path for char in "\0\r\n?#"):
        raise ValueError("Invalid Docker socket path")
    return path


def _container_id(config):
    cid = config["container_id"]
    if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("Exact full container ID required")
    return cid


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Docker Engine deadline exceeded")
    return remaining


@contextmanager
def _request(config, method, path, body, expected, deadline):
    connection = _UnixHTTP(_socket_path(config), _remaining(deadline))
    try:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        connection.request(method, path, body=encoded, headers={"Content-Type": "application/json"})
        connection.sock.settimeout(_remaining(deadline))
        response = connection.getresponse()
        if response.status != expected:
            raise RuntimeError(f"Docker Engine {method} {path}: HTTP {response.status}")
        yield response, connection
    finally:
        connection.close()


def _json_request(config, method, path, body, expected, deadline):
    with _request(config, method, path, body, expected, deadline) as (response, connection):
        chunks, count = [], 0
        while True:
            connection.sock.settimeout(_remaining(deadline))
            data = response.read1(min(65_536, JSON_BYTES + 1 - count))
            if not data:
                break
            chunks.append(data)
            count += len(data)
            if count > JSON_BYTES:
                raise ValueError("Docker Engine JSON byte limit exceeded")
        return json.loads(b"".join(chunks))


def _owned(config, deadline):
    cid = _container_id(config)
    info = _json_request(config, "GET", f"/containers/{cid}/json", None, 200, deadline)
    if (info["Id"] != cid or info["Config"]["Labels"].get("ov1104.owner") != config["owner"]
            or info["Image"] != config["image_id"] or not info["State"]["Running"]
            or info["State"]["Paused"]):
        raise RuntimeError("Container identity, ownership or running state differs")
    mounts = info["Mounts"]
    if (len(mounts) != 1 or mounts[0]["Type"] != "bind"
            or mounts[0]["Source"] != config["workspace_source"]
            or mounts[0]["Destination"] != config["workdir"] or not mounts[0]["RW"]):
        raise RuntimeError("Container workspace mount differs")
    return cid


def _workspace(config, workspace):
    if str(workspace) != config["workspace_source"]:
        raise ValueError("Workspace must match the frozen absolute bind source exactly")


@contextmanager
def _paused(config, *, destroy=False):
    cid = _owned(config, time.monotonic() + 30)

    def docker(*actions):
        subprocess.run([config["docker"], *actions, cid], cwd=config["control"],
                       env=config["docker_env"], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30, check=True)

    # A lost pause acknowledgement still requires removal or an unpause attempt.
    try:
        docker("pause")
        yield
    finally:
        if destroy:
            docker("rm", "-f")
            _json_request(config, "GET", f"/containers/{cid}/json", None, 404, time.monotonic() + 30)
        else:
            docker("unpause")


def _write_new(parent, name, data):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(name, dir_fd=parent)
        raise


def capture_submission(config: dict, baseline: Path, workspace: Path, rdir: Path, *, destroy_agent=False) -> dict:
    """Capture bounded source/metadata evidence while the exact owned container pauses."""
    _workspace(config, workspace)
    budget = {"entries": 0, "bytes": 0}
    with _paused(config, destroy=destroy_agent):
        with _root(baseline) as base_fd, _root(workspace) as workspace_fd:
            before, after = _tree(base_fd, budget), _tree(workspace_fd, budget)
        body, changed = _render(before, after)
        encoded = body.encode("utf-8")
        metadata = {
            "capture_phase": "before_G1_before_test_restoration",
            "baseline": "agent_input_tree_including_constructed_tests",
            "format": "safe-io-source-unified-and-explicit-metadata-v1",
            "post_capture_action": "destroy_owned_agent" if destroy_agent else "resume_container",
            "diff_sha256": hashlib.sha256(encoded).hexdigest(), "diff_chars": len(body),
            "diff_bytes": len(encoded), "changed_files": changed,
            "limits": {"combined_tree_entries": MAX_ENTRIES, "combined_tree_bytes": MAX_TOTAL_BYTES,
                       "file_bytes": MAX_FILE_BYTES, "diff_bytes": MAX_DIFF_BYTES, "depth": MAX_DEPTH,
                       "text_lines_per_file": MAX_TEXT_LINES, "text_line_chars": MAX_TEXT_LINE_CHARS,
                       "overflow": "reject", "excluded_names": sorted(EXCLUDED)},
            "observed": budget,
        }
        with _root(rdir) as output_fd:
            _write_new(output_fd, "final.diff", encoded)
            _write_new(output_fd, "submitted-diff.json", (json.dumps(metadata, indent=2) + "\n").encode())
    return metadata


@contextmanager
def _parent(root, parts):
    descriptors, edges = [], []
    descriptor = root
    try:
        for name in parts:
            with suppress(FileExistsError):
                os.mkdir(name, 0o755, dir_fd=descriptor)
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(name, DIR_FLAGS, dir_fd=descriptor)
            descriptors.append(child)
            if _identity(before) != _identity(os.fstat(child)):
                raise RuntimeError("Restore parent changed during open")
            edges.append((descriptor, name, before))
            descriptor = child
        yield descriptor
        for parent, name, before in reversed(edges):
            _unchanged(parent, name, before)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def restore_tests(config: dict, sources: dict[str, Path], workspace: Path) -> None:
    """Replace test entries atomically; never open an existing destination for writing."""
    _workspace(config, workspace)
    prepared, total = [], 0
    for relative, source in sorted(sources.items()):
        parts = _parts(relative)
        source = Path(source)
        if source == workspace or workspace in source.parents:
            raise ValueError("Trusted restore source cannot be inside the model mount")
        with _root(source.parent) as parent:
            _parts(source.name)
            info = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
            data = _read_regular(parent, source.name, info, MAX_FILE_BYTES)
        total += len(data)
        if len(prepared) >= MAX_ENTRIES or total > MAX_TOTAL_BYTES:
            raise ValueError("Restore source limit exceeded")
        prepared.append((parts, data, stat.S_IMODE(info.st_mode) & 0o777))
    with _paused(config), _root(workspace) as root:
        for parts, data, mode in prepared:
            with _parent(root, parts[:-1]) as parent:
                name = parts[-1]
                try:
                    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    before = None
                if before is not None and not (stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)):
                    raise ValueError("Restore destination is a special file or directory")
                temporary = ".safe-io-" + uuid.uuid4().hex
                created = False
                try:
                    _write_new(parent, temporary, data)
                    created = True
                    os.chmod(temporary, mode, dir_fd=parent, follow_symlinks=False)
                    if before is not None:
                        _unchanged(parent, name, before, content=True)
                    else:
                        try:
                            os.stat(name, dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise RuntimeError("Restore destination appeared during write")
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                    created = False
                    os.fsync(parent)
                finally:
                    if created:
                        os.unlink(temporary, dir_fd=parent)


def image_environment(config: dict) -> list[str]:
    """Use only the immutable task image's environment, never the host environment."""
    image = _json_request(config, "GET", f"/images/{config['image_id']}/json", None, 200, time.monotonic() + 30)
    if image["Id"] != config["image_id"]:
        raise RuntimeError("Task image environment identity differs")
    environment = {}
    for entry in image["Config"].get("Env") or []:
        name, separator, value = entry.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or "\0" in value:
            raise ValueError("Invalid immutable image environment")
        environment[name] = value
    if "HOME" not in environment:
        if image["Config"].get("User", "") not in ("", "root", "0", "root:root", "0:0"):
            raise ValueError("Non-root image must declare its canonical HOME")
        environment["HOME"] = "/root"
    environment.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    environment.update(LANG="C.UTF-8", PYTHONDONTWRITEBYTECODE="1")
    return [name + "=" + value for name, value in sorted(environment.items())]


def run_tests(config: dict, command: str) -> dict:
    """Drain Engine multiplexed output and return only the Engine-inspected exit code."""
    if not isinstance(command, str) or "\0" in command:
        raise ValueError("Grade command must be a string without NUL")
    started = time.monotonic()
    deadline = started + EXEC_SECONDS
    cid = _owned(config, deadline)
    env = image_environment(config)
    created = _json_request(config, "POST", f"/containers/{cid}/exec", {
        "AttachStdin": False, "AttachStdout": True, "AttachStderr": True, "Tty": False,
        "Env": env, "WorkingDir": config["workdir"],
        "Cmd": ["/usr/bin/env", "-i", *env, "/bin/bash", "--noprofile", "--norc", "-c",
                'timeout 840 sh -c "$1"', "grade", command],
    }, 201, deadline)
    exec_id = created["Id"]
    if not isinstance(exec_id, str) or not re.fullmatch(r"[0-9a-f]{64}", exec_id):
        raise RuntimeError("Docker Engine returned an invalid exec ID")
    stdout, stderr, combined = bytearray(), bytearray(), bytearray()
    with _request(config, "POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": False},
                  200, deadline) as (response, connection):
        def exact(count, allow_eof=False):
            data = bytearray()
            while len(data) < count:
                connection.sock.settimeout(_remaining(deadline))
                chunk = response.read1(count - len(data))
                if not chunk:
                    if allow_eof and not data:
                        return None
                    raise RuntimeError("Truncated Docker Engine multiplex stream")
                data.extend(chunk)
            return bytes(data)

        while True:
            header = exact(8, allow_eof=True)
            if header is None:
                break
            channel, length = header[0], struct.unpack(">I", header[4:])[0]
            if channel not in (1, 2) or header[1:4] != b"\0\0\0":
                raise RuntimeError("Invalid Docker Engine multiplex header")
            while length:
                data = exact(min(length, 65_536))
                length -= len(data)
                selected = stdout if channel == 1 else stderr
                selected.extend(data)
                del selected[:-TAIL_BYTES]
                combined.extend(data)
                del combined[:-TAIL_BYTES]
    inspected = _json_request(config, "GET", f"/exec/{exec_id}/json", None, 200, deadline)
    code = inspected["ExitCode"]
    if (inspected["ID"] != exec_id or inspected["ContainerID"] != cid
            or inspected["Running"] is not False or type(code) is not int or not 0 <= code <= 255):
        raise RuntimeError("Docker Engine exec is incomplete or has invalid authoritative status")
    return {"rc": code, "docker_rc": 0, "exit_channel": "docker-engine-exec-inspect-v1",
            "exec_id": exec_id, "engine_exit_code": code, "completed": True,
            "container_id": cid,
            "image_environment_sha256": hashlib.sha256(json.dumps(env).encode()).hexdigest(),
            "engine_http_statuses": {"container_inspect": 200, "exec_create": 201,
                                     "exec_start": 200, "exec_inspect": 200},
            "tail": combined.decode("utf-8", errors="replace"),
            "stdout_tail": stdout.decode("utf-8", errors="replace"),
            "stderr_tail": stderr.decode("utf-8", errors="replace"),
            "wall_s": round(time.monotonic() - started, 3)}
