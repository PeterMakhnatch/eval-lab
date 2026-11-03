"""Structural DEV admission of one native rollout, as an allow-list.

A rollout is admitted only when everything about it is what a Harbor trial looks like, so no
list of known-bad tricks is needed:

* the path is absolute, canonical text made of printable ASCII, and exactly what the caller
  listed: no ``..``, no trailing separator, no Unicode look-alikes, and every component equals
  the name on disk letter for letter (a case-insensitive filesystem would otherwise accept
  ``Agent`` for ``agent``);
* every component of the path, the top one included, is a real directory or file by ``lstat``:
  a symlink on the path or inside the trial is refused, so a link cannot redirect to, or hide,
  forbidden content. ``allowed_roots`` names the folders the operator trusts: operating-system
  links *above* a root (macOS ``/tmp`` is a link) are fine, links at or below it never are;
* every file inside is regular and is not a hard link to sealed material: published trials are
  themselves hard-linked copies (``st_nlink`` 2), so a link count cannot decide, but the inode
  of every file under the caller's sealed folders is known, and a match is refused;
* a directory is a whole trial (never ``agent`` or ``verifier``); a single file is accepted only
  when no ancestor is ``agent``/``verifier`` and no ancestor folder holds a sidecar or an
  ``agent`` folder, so a row cannot point inside a trial to skip its sidecars. Files are indexed
  by case-folded relative path, so ``Result.json`` is a sidecar too;
* every trajectory and sidecar is read once, into memory, together with the hash of every file;
  the digest is checked on those same bytes and the parsed documents are what the caller serves,
  so nothing is read again after it is verified;
* a ``split`` key (any case, any depth, lists included except the agent-written
  ``steps``/``nodes``) must be exactly ``dev``, a document nested past ``MAX_DEPTH`` is refused,
  and malformed JSON is refused;
* every task, trial and run name found anywhere (documents, every path segment, every file and
  folder name in the trial, the run id) is compared in canonical form (NFKC, case-folded, each
  path segment and ``__`` stem) with the corpus registry's TEST assignments and sealed
  identities.

``AdmissionRefused.detail`` may hold paths and ids: log it, never show it to a judge.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab.line.io import canonical_hash

SIDECARS = ("result.json", "config.json", "run.json", "grade.json")
TRAJECTORIES = ("agent/trajectory.json", "trajectory.json")
DIFFS = ("final.diff", "verifier/agent.diff")
TRIAL_FOLDERS = frozenset({"agent", "verifier"})
IDENTITY_KEYS = frozenset(
    {"task_id", "task_name", "task", "trial_name", "trial_id", "id", "item_id", "record_id"}
)
DIGEST_KEYS = frozenset({"task_checksum", "task_digest", "package_digest"})
OPAQUE_KEYS = frozenset({"steps", "nodes"})  # agent-written content, not declarations
MAX_DEPTH = 8
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")
_SEPARATORS = re.compile(r"[\\/]+")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}$")


class AdmissionRefused(ValueError):
    """The rollout is not admitted. ``unreadable`` marks a missing or malformed file and
    ``integrity`` a digest mismatch, as opposed to a readable, forbidden rollout."""

    def __init__(self, detail: str, *, unreadable: bool = False, integrity: bool = False) -> None:
        super().__init__(detail)
        self.detail, self.unreadable, self.integrity = detail, unreadable, integrity


@dataclass(frozen=True)
class Admitted:
    root: Path
    kind: str  # "trial" | "file"
    trajectory: Path | None  # the file the served document came from
    document: Any  # that file, parsed from the bytes the digest covered (None: no trajectory)
    diff: str | None
    names: tuple[str, ...]
    sha256: str  # the line's digest of what was read


def canonical(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _ascii(text: str, what: str) -> None:
    if not _PRINTABLE_ASCII.fullmatch(text):
        raise AdmissionRefused(f"{what} is not printable ASCII: {text!r}")


def _gate(path: Path | str) -> None:
    try:
        hd.check_dev_path_allowed(path)
    except hd.RefusedRun as error:
        raise AdmissionRefused(str(error)) from error


def _lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as error:
        raise AdmissionRefused(f"cannot stat {path}: {error}", unreadable=True) from error


def _trusted_parts(root: Path, allowed_roots: Sequence[str | Path]) -> int:
    """How many leading components of ``root`` are trusted (0 without ``allowed_roots``)."""
    if not allowed_roots:
        return 0
    for allowed in allowed_roots:
        base = Path(allowed)
        if base.is_absolute() and root.parts[: len(base.parts)] == base.parts:
            return len(base.parts)
    raise AdmissionRefused(f"rollout path is not under an allowed root: {root}")


def _walk_path(listed: str, allowed_roots: Sequence[str | Path]) -> Path:
    """The listed path, validated component by component without following any link."""
    _ascii(listed, "rollout path")
    root = Path(listed)
    if not root.is_absolute() or str(root) != listed or ".." in root.parts or "." in root.parts:
        raise AdmissionRefused(f"rollout path is not canonical and absolute: {listed}")
    trusted = _trusted_parts(root, allowed_roots)
    prefix = Path(root.anchor)
    for index, part in enumerate(root.parts[1:], start=2):
        parent, prefix = prefix, prefix / part
        mode = _lstat(prefix).st_mode
        if stat.S_ISLNK(mode) and index > trusted:
            raise AdmissionRefused(f"symlink on the rollout path: {prefix}")
        if part not in os.listdir(parent):
            raise AdmissionRefused(f"path case does not match the filesystem: {prefix}")
        _gate(prefix)  # a link-free prefix resolves to itself, so the gate sees what is written
    return root


def sealed_inodes(*roots: Path) -> frozenset[tuple[int, int]]:
    """``(device, inode)`` of every file under the sealed folders (stat only, links not followed)."""
    found: set[tuple[int, int]] = set()
    for root in roots:
        for directory, _, names in os.walk(root, followlinks=False):
            for name in names:
                info = os.lstat(os.path.join(directory, name))
                found.add((info.st_dev, info.st_ino))
    return frozenset(found)


def _needed(relative: str) -> bool:
    return relative in {*SIDECARS, *TRAJECTORIES, *DIFFS}


@dataclass
class _Tree:
    digest: str
    held: dict[str, tuple[Path, bytes]]  # casefolded relative path -> (path, bytes read)
    names: list[str]


def _read_tree(root: Path, sealed: frozenset[tuple[int, int]]) -> _Tree:
    """Walk the trial once: refuse links, sealed hard links and special files; hash every file;
    keep the bytes of the files the caller needs."""
    files: list[tuple[Path, str]] = []
    held: dict[str, tuple[Path, bytes]] = {}
    seen: dict[str, Path] = {}
    names: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                path = Path(entry.path)
                _ascii(entry.name, "file name")
                names.append(entry.name)
                mode = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(mode.st_mode):
                    raise AdmissionRefused(f"symlink inside the trial: {path}")
                _gate(path)
                if stat.S_ISDIR(mode.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(mode.st_mode):
                    if (mode.st_dev, mode.st_ino) in sealed:
                        raise AdmissionRefused(
                            f"hard link to sealed material inside the trial: {path}"
                        )
                    relative = path.relative_to(root).as_posix().casefold()
                    if relative in seen:
                        raise AdmissionRefused(f"two files differ only by case: {path}")
                    seen[relative] = path
                    if _needed(relative):
                        data = path.read_bytes()
                        held[relative] = (path, data)
                        files.append((path, hashlib.sha256(data).hexdigest()))
                    else:
                        with path.open("rb") as handle:
                            files.append((path, hashlib.file_digest(handle, "sha256").hexdigest()))
                else:
                    raise AdmissionRefused(f"special file inside the trial: {path}")
    # the line's own digest of a folder, computed from the hashes of the bytes read here
    ordered = sorted(files)
    digest = canonical_hash([(p.relative_to(root).as_posix(), d) for p, d in ordered])
    return _Tree(digest, held, names)


def _parse(path: Path, data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise AdmissionRefused(f"{path}: not JSON ({error})", unreadable=True) from error


def declares_non_dev(value: Any, depth: int = 0) -> bool:
    """True when any ``split`` key, in any case, is not exactly ``dev``; fails closed on a
    document nested deeper than ``MAX_DEPTH``."""
    if isinstance(value, Mapping):
        if depth > MAX_DEPTH:
            return True
        for key, child in value.items():
            if isinstance(key, str) and canonical(key) == "split":
                if child is None:
                    continue
                if not isinstance(child, str) or canonical(child) != "dev":
                    return True
            elif key not in OPAQUE_KEYS and declares_non_dev(child, depth + 1):
                return True
    elif isinstance(value, list):
        if depth > MAX_DEPTH:
            return True
        return any(declares_non_dev(child, depth + 1) for child in value)
    return False


def identities(value: Any, depth: int = 0) -> tuple[list[str], list[str]]:
    """``(names, digests)`` a document declares, including Harbor's ``{"path": ...}`` and
    ``{"name": ...}`` mappings for ``task`` and ``task_id``."""
    names: list[str] = []
    digests: list[str] = []
    if depth > MAX_DEPTH:
        return names, digests
    children: Iterable[tuple[Any, Any]] = ()
    if isinstance(value, Mapping):
        children = value.items()
    elif isinstance(value, list):
        children = (("", child) for child in value)
    for key, child in children:
        if isinstance(key, str) and key in IDENTITY_KEYS:
            if isinstance(child, str):
                names.append(child)
            elif isinstance(child, Mapping):
                names += [v for k in ("path", "name") if isinstance(v := child.get(k), str)]
        elif isinstance(key, str) and key in DIGEST_KEYS and isinstance(child, str):
            digests.append(child)
        if key not in OPAQUE_KEYS and isinstance(child, Mapping | list):
            sub_names, sub_digests = identities(child, depth + 1)
            names += sub_names
            digests += sub_digests
    return names, digests


def name_forms(raw: str) -> set[str]:
    """Every form of a name worth comparing: whole, each path segment, each ``__`` stem, and
    each of those without a file extension (``.json``, ``.txt``, ``.tar.gz`` one at a time)."""
    text = canonical(raw)
    forms = {text.strip("/\\")}
    for segment in _SEPARATORS.split(text):
        if segment:
            for form in (
                segment,
                _EXTENSION.sub("", segment),
                _EXTENSION.sub("", _EXTENSION.sub("", segment)),
            ):
                forms.add(form)
                forms.add(form.split("__")[0])
    return {form for form in forms if form}


def _refuse_test_identity(registry: Any, names: Iterable[str], digests: Iterable[str]) -> None:
    sealed = getattr(registry, "sealed_identity", None)
    for raw in names:
        for form in name_forms(raw):
            if registry.assigned_test_anywhere(item_id=form) or registry.assigned_test_anywhere(
                task_id=form
            ):
                raise AdmissionRefused(f"assigned TEST in registry: {form}")
            if sealed is not None and sealed(item_id=form, task_id=form) is not None:
                raise AdmissionRefused(f"sealed identity in registry: {form}")
    for digest in digests:
        if sealed is not None and sealed(task_digest=digest) is not None:
            raise AdmissionRefused("sealed task digest in registry")


def _inside_a_trial(file: Path) -> bool:
    """Whether any ancestor is a trial folder or holds a trial's sidecars (case-insensitively)."""
    for folder in file.parents:
        if canonical(folder.name) in TRIAL_FOLDERS:
            return True
        try:
            listing = {canonical(name) for name in os.listdir(folder)}
        except OSError:
            continue
        if listing & {"agent", *SIDECARS}:
            return True
    return False


def admit_rollout(
    listed: str,
    *,
    registry: Any,
    row: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    expected_sha256: str | None = None,
    sealed: frozenset[tuple[int, int]] = frozenset(),
    allowed_roots: Sequence[str | Path] = (),
    require_trajectory: bool = True,
) -> Admitted:
    """Admit the rollout at exactly ``listed`` or raise :class:`AdmissionRefused`.

    ``expected_sha256`` is the line's digest of the rollout; it is checked on the bytes read
    here, before anything is parsed. With ``require_trajectory=False`` a rollout without an ATIF
    trajectory (a patch or code bundle) is admitted with ``document=None``. Paths must be
    absolute; ``allowed_roots`` trusts links above (never at or below) the named folders.
    """
    root = _walk_path(listed, allowed_roots)
    top = _lstat(root).st_mode
    documents: list[Any] = [dict(row)] if row is not None else []
    names: list[str] = [name for name in (run_id, str(root)) if name]
    candidates: list[tuple[Path, bytes]] = []
    sidecars: list[tuple[Path, bytes]] = []
    diff_bytes: bytes | None = None
    if stat.S_ISDIR(top):
        if canonical(root.name) in TRIAL_FOLDERS:
            raise AdmissionRefused(f"a folder inside a trial is not a trial: {root}")
        tree = _read_tree(root, sealed)
        kind, digest, names = "trial", tree.digest, names + tree.names
        candidates = [tree.held[name] for name in TRAJECTORIES if name in tree.held]
        sidecars = [tree.held[name] for name in SIDECARS if name in tree.held]
        for name in DIFFS:
            if name in tree.held:
                diff_bytes = tree.held[name][1]
                break
    elif stat.S_ISREG(top):
        info = _lstat(root)
        if (info.st_dev, info.st_ino) in sealed:
            raise AdmissionRefused(f"hard link to sealed material: {root}")
        if _inside_a_trial(root):
            raise AdmissionRefused(f"a file inside a trial is not a rollout: {root}")
        data = root.read_bytes()
        kind, digest = "file", hashlib.sha256(data).hexdigest()
        if root.suffix.casefold() == ".json" or require_trajectory:
            candidates = [(root, data)]
    else:
        raise AdmissionRefused(f"rollout is neither a folder nor a regular file: {root}")
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise AdmissionRefused(
            f"sha256 expected {expected_sha256.lower()} got {digest}", integrity=True
        )
    documents += [_parse(path, data) for path, data in sidecars]
    served: tuple[Path, Any] | None = None
    for path, data in candidates:
        document = _parse(path, data)
        documents.append(document)
        if isinstance(document, Mapping) and isinstance(document.get("traces"), list):
            documents += [trace for trace in document["traces"] if isinstance(trace, Mapping)]
            served = served or (path, document)
        elif isinstance(document, Mapping) and isinstance(document.get("steps"), list):
            served = served or (path, document)
    if served is None and require_trajectory:
        raise AdmissionRefused("no ATIF trajectory", unreadable=True)
    if any(declares_non_dev(document) for document in documents):
        raise AdmissionRefused("a split declaration is not dev")
    digests: list[str] = []
    for document in documents:
        found_names, found_digests = identities(document)
        for identity in found_names:
            if identity.strip():
                _ascii(identity, "task or trial name")
                _gate("/" + identity.strip().lstrip("/\\"))  # the path gate, without the cwd
                names.append(identity)
        digests += found_digests
    _refuse_test_identity(registry, names, digests)
    diff = diff_bytes.decode("utf-8", errors="replace") if diff_bytes is not None else None
    path, document = served if served else (None, None)
    return Admitted(root, kind, path, document, diff, tuple(names), digest)
