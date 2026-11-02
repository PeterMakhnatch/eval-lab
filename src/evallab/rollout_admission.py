"""Structural DEV admission of one native rollout, as an allow-list.

A rollout is admitted only when everything about it is what a Harbor trial looks like, so no
list of known-bad tricks is needed:

* the path is absolute, canonical text made of printable ASCII, and exactly what the caller
  listed (no ``..``, no trailing separator, no Unicode look-alikes, no case folding of the
  listed form);
* every component of the path, the top one included, is a real directory or file by ``lstat``:
  a symlink anywhere on the path or inside the trial is refused, so a link cannot redirect to,
  or hide, forbidden content;
* every file inside is regular and is not a hard link to sealed material: published trials are
  themselves hard-linked copies (``st_nlink`` 2), so a link count cannot decide, but the inode
  of every file under the caller's sealed folders is known, and a match is refused;
* a directory is a whole trial; a single file is accepted only when it is not inside one (its
  folder holds no ``result.json``/``config.json``/``run.json`` and no ``agent`` folder), so a
  row cannot point at ``<trial>/agent/trajectory.json`` to skip the trial's sidecars;
* every trajectory and sidecar is read; a ``split`` key (any case, any depth, lists included
  except the agent-written ``steps``/``nodes``) must be exactly ``dev``, a document nested past
  ``MAX_DEPTH`` is refused, and malformed JSON is refused;
* every task, trial and run name found anywhere (documents, file and folder names, the run id)
  is compared in canonical form (NFKC, case-folded, each path segment and ``__`` stem) with the
  corpus registry's TEST assignments and sealed identities.

``AdmissionRefused.detail`` may hold paths and ids: log it, never show it to a judge.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd

SIDECARS = ("result.json", "config.json", "run.json")
TRAJECTORIES = ("agent/trajectory.json", "trajectory.json")
DIFFS = ("final.diff", "verifier/agent.diff")
IDENTITY_KEYS = frozenset(
    {"task_id", "task_name", "task", "trial_name", "trial_id", "id", "item_id", "record_id"}
)
DIGEST_KEYS = frozenset({"task_checksum", "task_digest", "package_digest"})
OPAQUE_KEYS = frozenset({"steps", "nodes"})  # agent-written content, not declarations
MAX_DEPTH = 8
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")
_SEPARATORS = re.compile(r"[\\/]+")


class AdmissionRefused(ValueError):
    """The rollout is not admitted. ``unreadable`` marks a missing or malformed file, as opposed
    to one that is readable and forbidden."""

    def __init__(self, detail: str, *, unreadable: bool = False) -> None:
        super().__init__(detail)
        self.detail, self.unreadable = detail, unreadable


@dataclass(frozen=True)
class Admitted:
    root: Path
    kind: str  # "trial" | "file"
    trajectory: Path  # the one trajectory file to serve
    diff: str | None
    names: tuple[str, ...]


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


def _walk_path(listed: str) -> Path:
    """The listed path, validated component by component without following any link."""
    _ascii(listed, "rollout path")
    root = Path(listed)
    if not root.is_absolute() or str(root) != listed or ".." in root.parts or "." in root.parts:
        raise AdmissionRefused(f"rollout path is not canonical and absolute: {listed}")
    prefix = Path(root.anchor)
    for part in root.parts[1:]:
        prefix = prefix / part
        mode = _lstat(prefix).st_mode
        if stat.S_ISLNK(mode):
            raise AdmissionRefused(f"symlink on the rollout path: {prefix}")
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


def _walk_tree(root: Path, sealed: frozenset[tuple[int, int]]) -> list[Path]:
    """Every file below ``root``; refuses links, sealed hard links and special files."""
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                path = Path(entry.path)
                _ascii(entry.name, "file name")
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
                    files.append(path)
                else:
                    raise AdmissionRefused(f"special file inside the trial: {path}")
    return files


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
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
    each without a ``.json``/``.jsonl`` suffix."""
    text = canonical(raw)
    forms = {text.strip("/\\")}
    for segment in _SEPARATORS.split(text):
        if segment:
            forms.add(segment)
            forms.add(segment.split("__")[0])
            forms.add(re.sub(r"\.jsonl?$", "", segment))
    return {form for form in forms if form}


def _refuse_test_identity(registry: Any, names: Iterable[str], digests: Iterable[str]) -> None:
    sealed = getattr(registry, "sealed_identity", None)
    for raw in names:
        _ascii(raw, "task or trial name")
        _gate("/" + raw.strip().lstrip("/\\"))  # the path gate, without the cwd
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
    folder = file.parent
    return (
        folder.name == "agent"
        or (folder / "agent").exists()
        or any((folder / sidecar).exists() for sidecar in SIDECARS)
    )


def admit_rollout(
    listed: str,
    *,
    registry: Any,
    row: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    verify: Callable[[Path], None] | None = None,
    sealed: frozenset[tuple[int, int]] = frozenset(),
) -> Admitted:
    """Admit the rollout at exactly ``listed`` or raise :class:`AdmissionRefused`.

    ``verify(root)`` runs after the structure is accepted and before any content is read (the
    caller's integrity check); anything it raises propagates.
    """
    root = _walk_path(listed)
    top = _lstat(root).st_mode
    documents: list[Any] = [dict(row)] if row is not None else []
    names: list[str] = [name for name in (run_id, root.name, root.stem, root.parent.name) if name]
    diff_files: list[Path] = []
    if stat.S_ISDIR(top):
        if root.name == "agent":
            raise AdmissionRefused(f"a folder inside a trial is not a trial: {root}")
        files = _walk_tree(root, sealed)
        present = set(files)
        kind = "trial"
        candidates = [root / name for name in TRAJECTORIES if root / name in present]
        sidecars = [root / name for name in SIDECARS if root / name in present]
        diff_files = [root / name for name in DIFFS if root / name in present]
    elif stat.S_ISREG(top):
        info = _lstat(root)
        if (info.st_dev, info.st_ino) in sealed:
            raise AdmissionRefused(f"hard link to sealed material: {root}")
        if _inside_a_trial(root):
            raise AdmissionRefused(f"a file inside a trial is not a rollout: {root}")
        kind, candidates, sidecars = "file", [root], []
    else:
        raise AdmissionRefused(f"rollout is neither a folder nor a regular file: {root}")
    if verify is not None:
        verify(root)
    documents += [_read_json(path) for path in sidecars]
    served: Path | None = None
    for candidate in candidates:
        document = _read_json(candidate)
        documents.append(document)
        if isinstance(document, Mapping) and isinstance(document.get("traces"), list):
            documents += [trace for trace in document["traces"] if isinstance(trace, Mapping)]
            served = served or candidate
        elif isinstance(document, Mapping) and isinstance(document.get("steps"), list):
            served = served or candidate
    if served is None:
        raise AdmissionRefused("no ATIF trajectory", unreadable=True)
    if any(declares_non_dev(document) for document in documents):
        raise AdmissionRefused("a split declaration is not dev")
    digests: list[str] = []
    for document in documents:
        found_names, found_digests = identities(document)
        names += found_names
        digests += found_digests
    _refuse_test_identity(registry, names, digests)
    diff = None
    if diff_files:
        diff = diff_files[0].read_text(encoding="utf-8", errors="replace")
    return Admitted(root, kind, served, diff, tuple(names))
