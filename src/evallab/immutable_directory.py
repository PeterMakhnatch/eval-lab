"""No-replace, same-parent publication for immutable artifact directories."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_parent(parent: Path) -> None:
    cursor = parent
    while not os.path.lexists(cursor):
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if cursor.is_symlink() or cursor.resolve(strict=True) != cursor:
        raise ValueError(f"refusing symlink destination chain: {parent}")
    parent.mkdir(parents=True, exist_ok=True)


def _safe_relative_file(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"invalid staged output name: {value}")
    return path


def _write_new_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_and_fsync_tree(root: Path) -> tuple[str, ...]:
    inventory: list[str] = []
    directories: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_root)
        directories.append(current)
        for name in directory_names:
            child = current / name
            mode = os.lstat(child).st_mode
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise ValueError(f"staged output contains non-directory entry: {child}")
        for name in file_names:
            child = current / name
            mode = os.lstat(child).st_mode
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise ValueError(f"staged output contains non-regular file: {child}")
            inventory.append(child.relative_to(root).as_posix())
    for directory in reversed(directories):
        _fsync_directory(directory)
    return tuple(sorted(inventory))

def atomic_no_replace_rename(source: Path, destination: Path) -> None:
    """Atomically rename a directory while refusing an existing destination."""

    import ctypes
    import ctypes.util
    import errno
    import platform

    system = platform.system()
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if system == "Darwin" and hasattr(libc, "renameatx_np"):
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-2, source_bytes, -2, destination_bytes, ctypes.c_uint(0x00000004))
    elif system == "Linux":
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(-100, source_bytes, -100, destination_bytes, ctypes.c_uint(1))
        else:
            machine = platform.machine().lower()
            syscall_number = (
                276 if machine.startswith(("aarch", "arm64")) else 316
            )
            libc.syscall.argtypes = [
                ctypes.c_long,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            libc.syscall.restype = ctypes.c_long
            result = libc.syscall(
                ctypes.c_long(syscall_number),
                -100,
                source_bytes,
                -100,
                destination_bytes,
                ctypes.c_uint(1),
            )
    else:
        raise NotImplementedError(
            "atomic no-replace directory publication is unavailable on this platform"
        )
    if result != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(
                error,
                "immutable destination already exists",
                destination,
            )
        raise OSError(error, os.strerror(error), destination)


@contextmanager
def staged_immutable_directory(destination: Path) -> Iterator[Path]:
    """Yield an unpublished directory and atomically publish it on success."""

    absolute = Path(os.path.abspath(destination))
    if os.path.lexists(absolute):
        raise FileExistsError(f"immutable destination already exists: {destination}")
    parent = absolute.parent
    _prepare_parent(parent)
    staged = Path(tempfile.mkdtemp(prefix=f".{absolute.name}.staging-", dir=parent))
    published = False
    try:
        yield staged
        _verify_and_fsync_tree(staged)
        if os.path.lexists(absolute):
            raise FileExistsError(f"immutable destination already exists: {destination}")
        atomic_no_replace_rename(staged, absolute)
        published = True
        _fsync_directory(parent)
    finally:
        if not published and os.path.lexists(staged):
            shutil.rmtree(staged)


def publish_immutable_files(destination: Path, payloads: Mapping[str, bytes]) -> Path:
    """Publish an exact mapping of relative regular files as one directory."""

    expected = tuple(sorted(payloads))
    with staged_immutable_directory(destination) as staged:
        for name in expected:
            relative = _safe_relative_file(name)
            _write_new_file(staged.joinpath(*relative.parts), payloads[name])
    published = Path(os.path.abspath(destination))
    actual = _verify_and_fsync_tree(published)
    if actual != expected:
        raise RuntimeError("published immutable directory inventory mismatch")
    return published


__all__ = [
    "atomic_no_replace_rename",
    "publish_immutable_files",
    "staged_immutable_directory",
]
