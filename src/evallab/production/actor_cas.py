from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _path(value: Path) -> Path:
    path = Path(value)
    if ".." in path.parts or path.anchor == "//":
        raise ValueError("path aliases are not allowed")
    return path.absolute()


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _remove_tree_at(directory: int) -> None:
    for entry in os.listdir(directory):
        try:
            child = os.open(entry, _DIRECTORY_FLAGS, dir_fd=directory)
        except OSError:
            os.unlink(entry, dir_fd=directory)
        else:
            try:
                _remove_tree_at(child)
            finally:
                os.close(child)
            os.rmdir(entry, dir_fd=directory)


def directory_status(path: Path) -> os.stat_result:
    target = _path(path)
    with _directory(target.parent) as parent:
        descriptor = os.open(target.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)


def discard_owned_directory(directory: Path, expected: os.stat_result) -> bool:
    target = _path(directory)
    with _directory(target.parent) as parent:
        try:
            descriptor = os.open(target.name, _DIRECTORY_FLAGS, dir_fd=parent)
        except (FileNotFoundError, OSError):
            return False
        try:
            status = os.fstat(descriptor)
            if (status.st_dev, status.st_ino) != (expected.st_dev, expected.st_ino):
                return False
            _remove_tree_at(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        os.rmdir(target.name, dir_fd=parent)
        return True


def _publish_no_replace(parent: int, staging_name: str, destination_name: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        rename = getattr(library, "renameat2", None)
        flags = 1
    elif sys.platform == "darwin":
        rename = getattr(library, "renameatx_np", None)
        flags = 4
    else:
        raise RuntimeError("atomic no-clobber CAS publication is unsupported on this platform")
    if rename is None:
        raise RuntimeError("atomic no-clobber CAS publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(staging_name), parent, os.fsencode(destination_name), flags):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_name)


class PublicationUncertainError(RuntimeError):
    pass


def _verify_published(parent: int, destination_name: str, staging: int) -> None:
    published = os.stat(destination_name, dir_fd=parent, follow_symlinks=False)
    staged = os.fstat(staging)
    if (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino):
        raise ValueError("published directory is not the staged directory")


def publish_staged_directory(
    staging: Path, destination: Path, *, expected: os.stat_result | None = None,
) -> None:
    staging_path, destination_path = _path(staging), _path(destination)
    if staging_path.parent != destination_path.parent or staging_path == destination_path:
        raise ValueError("staging and destination must be distinct siblings without aliases")
    with _directory(staging_path.parent) as parent:
        descriptor = os.open(staging_path.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            status = os.fstat(descriptor)
            if expected is not None and (status.st_dev, status.st_ino) != (
                expected.st_dev, expected.st_ino,
            ):
                raise ValueError("staging directory is no longer the one this run created")
            os.fsync(descriptor)
            _publish_no_replace(parent, staging_path.name, destination_path.name)
            try:
                _verify_published(parent, destination_path.name, descriptor)
                os.fsync(parent)
            except OSError as error:
                raise PublicationUncertainError(
                    f"published {destination_path} but could not confirm it: {error}"
                ) from error
            except ValueError as error:
                raise PublicationUncertainError(
                    f"published {destination_path} but it is not the staged tree: {error}"
                ) from error
        finally:
            os.close(descriptor)
