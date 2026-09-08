"""Archive-backed runtime identity for retained quality-audit evidence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from evallab.task_workbench import _audit_runtime_identity

# A uid no test host uses for the invoking user, so retained host ownership
# provably differs from the container ownership recorded in the archives.
CONTAINER_UID = getattr(os, "geteuid", lambda: 1000)() + 4242
CONTAINER_GID = CONTAINER_UID + 7

DEFAULT_FILES: dict[str, bytes] = {
    "input/data.json": b'{"value": 2}',
    "inputs/extra.json": b'{"value": 5}',
    "config/credentials.conf": b"secret",
    "result.json": b'{"value": 3}',
}
DEFAULT_MODES = {"config/credentials.conf": 0o600}


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tar_bytes(
    files: dict[str, bytes],
    *,
    uid: int,
    gid: int,
    modes: dict[str, int] | None = None,
    link_paths: dict[str, str] | None = None,
    link_type: bytes = tarfile.SYMTYPE,
    extra_members: list[tuple[str, bytes, str, bytes]] | None = None,
    duplicate: str | None = None,
) -> tuple[bytes, dict[str, dict[str, Any]]]:
    """Build a docker-cp-shaped tar plus the manifest the runner would record."""
    modes = {**DEFAULT_MODES, **(modes or {})}
    link_paths = link_paths or {}
    entries: dict[str, dict[str, Any]] = {}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:

        def add_dir(name: str) -> None:
            info = tarfile.TarInfo("." if name == "." else f"./{name}")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid, info.gid = uid, gid
            archive.addfile(info)

        def add_file(rel: str, payload: bytes) -> None:
            info = tarfile.TarInfo(f"./{rel}")
            info.size = len(payload)
            info.mode = modes.get(rel, 0o644)
            info.uid, info.gid = uid, gid
            archive.addfile(info, io.BytesIO(payload))

        dirs = {"."}
        for rel in files:
            parent = PurePosixPath(rel).parent
            while str(parent) != ".":
                dirs.add(str(parent))
                parent = parent.parent
        for name in sorted(dirs):
            add_dir(name)
        for rel in sorted(files):
            payload = files[rel]
            # A link replacing a member keeps the runner-recorded entry, the
            # way post-hoc tampering of an already-exported archive would.
            entries[rel] = {
                "sha256": _digest(payload),
                "size": len(payload),
                "uid": uid,
                "gid": gid,
                "mode": oct(modes.get(rel, 0o644)),
            }
            if rel in link_paths:
                info = tarfile.TarInfo(f"./{rel}")
                info.type = link_type
                info.linkname = link_paths[rel]
                info.mode = 0o644
                info.uid, info.gid = uid, gid
                archive.addfile(info)
            else:
                add_file(rel, payload)
        for name, mtype, linkname, payload in extra_members or ():
            info = tarfile.TarInfo(name)
            info.type = mtype
            info.linkname = linkname
            info.size = len(payload)
            info.mode = 0o644
            info.uid, info.gid = uid, gid
            archive.addfile(info, io.BytesIO(payload) if payload else None)
        if duplicate is not None:
            add_file(duplicate, files[duplicate])
    return buffer.getvalue(), entries


def _write_trio(
    arm: Path,
    kind: str,
    tar: bytes,
    entries: dict[str, dict[str, Any]],
    *,
    exit_code: int = 0,
    digest: str | None = None,
) -> None:
    (arm / f"{kind}-task-file.tar").write_bytes(tar)
    (arm / f"{kind}-task-file.manifest.json").write_text(json.dumps(entries))
    receipt = {
        "args": ["docker", "cp", f"cid:/task_file/.", "-"],
        "exit": exit_code,
        "archive_sha256": digest if digest is not None else _digest(tar),
    }
    (arm / f"{kind}-task-file.receipt.json").write_text(json.dumps(receipt))


def _build_arm(
    tmp_path: Path,
    *,
    files: dict[str, bytes] | None = None,
    host_modes: dict[str, int] | None = None,
    action_kwargs: dict[str, Any] | None = None,
    verifier_kwargs: dict[str, Any] | None = None,
    archives: bool = True,
    skip_verifier_archive: bool = False,
    rich_manifest: bool = True,
) -> Path:
    files = dict(files or DEFAULT_FILES)
    host_modes = {**DEFAULT_MODES, **(host_modes or {})}
    arm = tmp_path / "probe"
    for rel, payload in files.items():
        target = arm / "task_file" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        os.chmod(target, host_modes.get(rel, 0o644))
    manifest: dict[str, Any] = {}
    for path in sorted((arm / "task_file").rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(arm / "task_file").as_posix()
        retained = path.stat()
        manifest[rel] = (
            {
                "sha256": _digest(path.read_bytes()),
                "size": retained.st_size,
                "copied_mode": oct(stat.S_IMODE(retained.st_mode)),
            }
            if rich_manifest
            else _digest(path.read_bytes())
        )
    (arm / "task-file-manifest.json").write_text(json.dumps(manifest))
    if archives:
        kinds = (("action", action_kwargs),)
        if not skip_verifier_archive:
            kinds = (("action", action_kwargs), ("verifier", verifier_kwargs))
        for kind, kwargs in kinds:
            tar, entries = _tar_bytes(
                files,
                **{"uid": CONTAINER_UID, "gid": CONTAINER_GID, **(kwargs or {})},
            )
            _write_trio(arm, kind, tar, entries)
    return arm


def _identity(arm: Path, **fields: Any) -> dict[str, Any]:
    return _audit_runtime_identity({"evidence_path": str(arm), **fields})


def test_matched_archive_ownership_survives_host_uid_difference(tmp_path: Path) -> None:
    arm = _build_arm(tmp_path)
    identity = _identity(arm)
    assert identity["status"] == "verified"
    assert identity["ownership"]["status"] == "verified"
    assert identity["ownership"]["files"]["config/credentials.conf"] == {
        "sha256": _digest(b"secret"),
        "size": 6,
        "uid": CONTAINER_UID,
        "gid": CONTAINER_GID,
        "mode": "0o600",
    }
    # The retained host copy carries the invoking user's uid, not the
    # container's, and ownership still verifies because host uid/gid are
    # never runtime metadata.
    retained = arm / "task_file/config/credentials.conf"
    assert retained.stat().st_uid != CONTAINER_UID
    assert identity["transport"]["status"] == "verified"


@pytest.mark.parametrize("drift", ["uid", "mode"])
def test_transport_uid_and_mode_drift_is_mismatched(
    tmp_path: Path, drift: str
) -> None:
    verifier_kwargs = (
        {"uid": CONTAINER_UID + 1}
        if drift == "uid"
        else {"modes": {"config/credentials.conf": 0o600, "input/data.json": 0o444}}
    )
    identity = _identity(_build_arm(tmp_path, verifier_kwargs=verifier_kwargs))
    assert identity["status"] == "verified"
    assert identity["ownership"]["status"] == "verified"
    transport = identity["transport"]
    assert transport["status"] == "mismatched"
    assert transport["issues"] == (
        [f"transport_changed:{rel}" for rel in sorted(DEFAULT_FILES)]
        if drift == "uid"
        else ["transport_changed:input/data.json"]
    )


def test_bytes_only_manifest_cannot_invent_ownership(tmp_path: Path) -> None:
    identity = _identity(_build_arm(tmp_path, rich_manifest=False, archives=False))
    assert identity["status"] == "verified"
    assert identity["ownership"] == {"status": "unbound", "files": {}, "issues": []}
    assert identity["transport"] == {"status": "unbound", "issues": []}


def test_missing_verifier_archive_is_explicit_not_verified(tmp_path: Path) -> None:
    identity = _identity(_build_arm(tmp_path, skip_verifier_archive=True))
    assert identity["ownership"]["status"] == "verified"
    assert identity["transport"]["status"] == "mismatched"
    assert "transport_verifier_archive_unbound" in identity["transport"]["issues"]


@pytest.mark.parametrize("variant", ["receipt", "trailing_bytes"])
def test_archive_digest_mismatch_cannot_verify(tmp_path: Path, variant: str) -> None:
    arm = _build_arm(tmp_path)
    if variant == "receipt":
        receipt_path = arm / "action-task-file.receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["archive_sha256"] = _digest(b"not the archive")
        receipt_path.write_text(json.dumps(receipt))
    else:  # trailing bytes change the digest while the tar still parses
        with (arm / "action-task-file.tar").open("ab") as stream:
            stream.write(b"trailing junk")
    identity = _identity(arm)
    assert identity["status"] == "verified"  # retained bytes still manifest-clean
    assert identity["ownership"]["status"] == "mismatched"
    assert "action_archive_digest_mismatch" in identity["ownership"]["issues"]
    assert identity["transport"]["status"] == "mismatched"


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_link_member_at_manifest_path_cannot_verify(
    tmp_path: Path, link_type: bytes
) -> None:
    identity = _identity(
        _build_arm(
            tmp_path,
            action_kwargs={
                "link_paths": {"input/data.json": "../../../etc/passwd"},
                "link_type": link_type,
            },
        )
    )
    ownership = identity["ownership"]
    assert ownership["status"] == "mismatched"
    assert ownership["files"] == {}
    assert "action_archive_member_unsupported:input/data.json" in ownership["issues"]
    assert identity["transport"]["status"] == "mismatched"


@pytest.mark.parametrize("variant", ["escape", "duplicate"])
def test_escape_and_duplicate_members_are_rejected(tmp_path: Path, variant: str) -> None:
    action_kwargs = (
        {"extra_members": [("../outside.txt", tarfile.REGTYPE, "", b"escaped")]}
        if variant == "escape"
        else {"duplicate": "result.json"}
    )
    ownership = _identity(_build_arm(tmp_path, action_kwargs=action_kwargs))["ownership"]
    assert ownership["status"] == "mismatched"
    assert (
        "action_archive_member_escape:../outside.txt"
        if variant == "escape"
        else "action_archive_member_duplicate:result.json"
    ) in ownership["issues"]


@pytest.mark.parametrize("drift", ["mode", "size"])
def test_rich_manifest_validates_size_and_copied_mode(tmp_path: Path, drift: str) -> None:
    arm = _build_arm(tmp_path)
    manifest_path = arm / "task-file-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if drift == "mode":
        os.chmod(arm / "task_file/config/credentials.conf", 0o755)
    else:
        manifest["input/data.json"]["size"] += 1
        manifest_path.write_text(json.dumps(manifest))
    identity = _identity(arm)
    assert identity["status"] == "mismatched"
    assert identity["issues"] == [
        "config/credentials.conf" if drift == "mode" else "input/data.json"
    ]
    # Archive ownership is unaffected: chmod and manifest size edits do not
    # touch the retained bytes the archive is bound to.
    assert identity["ownership"]["status"] == "verified"
    assert identity["transport"]["status"] == "verified"


def test_runtime_input_paths_classify_exact_values(tmp_path: Path) -> None:
    identity = _identity(
        _build_arm(tmp_path), runtime_input_paths=["config/credentials.conf"]
    )
    assert sorted(identity["inputs"]) == [
        "config/credentials.conf",
        "input/data.json",
        "inputs/extra.json",
    ]
    assert sorted(identity["outputs"]) == ["result.json"]


def test_action_archive_disagreement_with_retained_copy_is_mismatched(
    tmp_path: Path,
) -> None:
    arm = _build_arm(tmp_path)
    target = arm / "task_file/input/data.json"
    target.write_bytes(b'{"value": 99}')
    manifest_path = arm / "task-file-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["input/data.json"]["sha256"] = _digest(b'{"value": 99}')
    manifest["input/data.json"]["size"] = len(b'{"value": 99}')
    manifest_path.write_text(json.dumps(manifest))
    identity = _identity(arm)
    assert identity["status"] == "verified"  # the manifest covers its own bytes
    ownership = identity["ownership"]
    assert ownership["status"] == "mismatched"
    assert "ownership_retained_mismatch:input/data.json" in ownership["issues"]
    assert identity["transport"]["status"] == "verified"
