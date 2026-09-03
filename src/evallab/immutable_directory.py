"""No-replace, same-parent publication for immutable artifact directories."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_or_create_parent_nofollow(parent: Path) -> int:
    if not parent.is_absolute():
        raise ValueError("immutable destination parent must be absolute")
    current_fd = os.open("/", _directory_flags())
    try:
        for part in parent.parts[1:]:
            try:
                next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(part, 0o777, dir_fd=current_fd)
                try:
                    next_fd = os.open(
                        part,
                        _directory_flags(),
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"refusing symlink destination chain: {parent}"
                    ) from exc
            except OSError as exc:
                raise ValueError(
                    f"refusing symlink destination chain: {parent}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _create_staged_directory(parent_fd: int, prefix: str) -> str:
    for _ in range(128):
        name = f"{prefix}{uuid.uuid4().hex}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name
    raise FileExistsError("unable to allocate a unique staging directory")


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


def _verify_and_fsync_directory_fd(
    directory_fd: int,
    *,
    prefix: str = "",
) -> tuple[str, ...]:
    inventory: list[str] = []
    for name in sorted(os.listdir(directory_fd)):
        relative = f"{prefix}/{name}" if prefix else name
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError(
                        f"staged directory changed during verification: {relative}"
                    )
                inventory.extend(
                    _verify_and_fsync_directory_fd(child_fd, prefix=relative)
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(before.st_mode):
            file_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)
                ):
                    raise ValueError(
                        f"staged file changed during verification: {relative}"
                    )
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            inventory.append(relative)
        else:
            raise ValueError(f"staged output contains a special entry: {relative}")
    os.fsync(directory_fd)
    return tuple(inventory)


def _entry_is_directory(
    parent_fd: int,
    name: str,
    *,
    identity: tuple[int, int],
) -> bool:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(current.st_mode) and (
        current.st_dev,
        current.st_ino,
    ) == identity


def _remove_tree_entry_at(parent_fd: int, name: str) -> None:
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    directory_fd = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            raise ValueError("cleanup target changed identity")
        for child in os.listdir(directory_fd):
            _remove_tree_entry_at(directory_fd, child)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _quarantine_and_remove(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    quarantine = f".{name}.rejected-{uuid.uuid4().hex}"
    os.rename(
        name,
        quarantine,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )
    _remove_tree_entry_at(parent_fd, quarantine)
    os.fsync(parent_fd)

def _atomic_no_replace_rename_at(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
    *,
    destination_display: Path,
) -> None:
    import ctypes
    import ctypes.util
    import errno
    import platform

    system = platform.system()
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
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
        result = rename(
            source_dir_fd,
            source_bytes,
            destination_dir_fd,
            destination_bytes,
            ctypes.c_uint(0x00000004),
        )
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
            result = renameat2(
                source_dir_fd,
                source_bytes,
                destination_dir_fd,
                destination_bytes,
                ctypes.c_uint(1),
            )
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
                source_dir_fd,
                source_bytes,
                destination_dir_fd,
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
                destination_display,
            )
        raise OSError(error, os.strerror(error), destination_display)


def atomic_no_replace_rename(source: Path, destination: Path) -> None:
    """Atomically rename a directory while refusing an existing destination."""

    source_parent = source.parent
    destination_parent = destination.parent
    directory_flags = _directory_flags()
    source_parent_fd = os.open(source_parent, directory_flags)
    destination_parent_fd = os.open(destination_parent, directory_flags)
    try:
        _atomic_no_replace_rename_at(
            source_parent_fd,
            source.name,
            destination_parent_fd,
            destination.name,
            destination_display=destination,
        )
    finally:
        os.close(destination_parent_fd)
        os.close(source_parent_fd)


@contextmanager
def staged_immutable_directory(
    destination: Path,
    *,
    expected_inventory: Collection[str] | None = None,
) -> Iterator[Path]:
    """Yield an unpublished directory and publish only its retained inode."""

    absolute = Path(os.path.abspath(destination))
    parent = absolute.parent
    parent_fd = _open_or_create_parent_nofollow(parent)
    staged_name = _create_staged_directory(
        parent_fd,
        f".{absolute.name}.staging-",
    )
    staged = parent / staged_name
    try:
        staged_fd = os.open(staged_name, _directory_flags(), dir_fd=parent_fd)
    except BaseException:
        _quarantine_and_remove(parent_fd, staged_name)
        os.close(parent_fd)
        raise
    staged_stat = os.fstat(staged_fd)
    staged_identity = (staged_stat.st_dev, staged_stat.st_ino)
    renamed = False
    published = False
    published_fd: int | None = None
    try:
        yield staged
        actual_inventory = _verify_and_fsync_directory_fd(staged_fd)
        if expected_inventory is not None:
            expected = tuple(sorted(expected_inventory))
            if len(expected) != len(set(expected)) or actual_inventory != expected:
                raise ValueError("staged immutable directory inventory mismatch")
        if not _entry_is_directory(
            parent_fd,
            staged_name,
            identity=staged_identity,
        ):
            raise ValueError("staged immutable directory identity changed")
        _atomic_no_replace_rename_at(
            parent_fd,
            staged_name,
            parent_fd,
            absolute.name,
            destination_display=absolute,
        )
        renamed = True
        try:
            published_fd = os.open(
                absolute.name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise ValueError(
                "published immutable directory is not a nofollow directory"
            ) from exc
        published_stat = os.fstat(published_fd)
        if (published_stat.st_dev, published_stat.st_ino) != staged_identity:
            raise ValueError("published immutable directory identity changed")
        if _verify_and_fsync_directory_fd(published_fd) != actual_inventory:
            raise ValueError("published immutable directory inventory changed")
        if not _entry_is_directory(
            parent_fd,
            absolute.name,
            identity=staged_identity,
        ):
            raise ValueError("published immutable directory identity changed")
        published = True
        os.fsync(parent_fd)
    finally:
        if published_fd is not None:
            os.close(published_fd)
        if not published:
            cleanup_name = absolute.name if renamed else staged_name
            _quarantine_and_remove(parent_fd, cleanup_name)
        os.close(staged_fd)
        os.close(parent_fd)


def publish_immutable_files(destination: Path, payloads: Mapping[str, bytes]) -> Path:
    """Publish an exact mapping of relative regular files as one directory."""

    expected = tuple(sorted(payloads))
    with staged_immutable_directory(
        destination,
        expected_inventory=expected,
    ) as staged:
        for name in expected:
            relative = _safe_relative_file(name)
            _write_new_file(staged.joinpath(*relative.parts), payloads[name])
    return Path(os.path.abspath(destination))


__all__ = [
    "atomic_no_replace_rename",
    "publish_immutable_files",
    "staged_immutable_directory",
]
