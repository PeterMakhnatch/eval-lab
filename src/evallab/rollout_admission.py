"""Structural DEV admission of one native rollout, as an allow-list.

A rollout is admitted only when everything about it is what a Harbor trial looks like, so no
list of known-bad tricks is needed:

* the path is absolute, canonical text made of printable ASCII, and exactly what the caller
  listed: no ``..``, no trailing separator, no Unicode look-alikes, and every component equals
  the name on disk letter for letter (a case-insensitive filesystem would otherwise accept
  ``Agent`` for ``agent``);
* every component of the path, the top one included, is a real directory or file by ``lstat``:
  a symlink on the path or inside the trial is refused, so a link cannot redirect to, or hide,
  forbidden content. The caller passes the canonical path (``os.path.realpath(p) == p``; macOS
  ``/tmp`` must be given as ``/private/tmp``) and every check runs on that one path, so a link
  above it can hide nothing because nothing else is ever examined;
* every file inside is regular and is not a hard link to sealed material: published trials are
  themselves hard-linked copies (``st_nlink`` 2), so a link count cannot decide, but the inode
  of every file under the caller's sealed folders is known, and a match is refused;
* a rollout is never inside a trial. Trials are told from job folders by shape (``TRIAL_SHAPE``,
  measured on the real Harbor results): an ancestor is a trial when it is named ``agent`` or
  ``verifier``, holds ``agent``, ``verifier``, ``trajectory.json``, ``final.diff`` or
  ``trial.log``, or holds a sidecar with a trial's keys (or one empty, not a mapping or
  unreadable; keys compared case-insensitively). A job folder's own sidecars carry job keys, so
  the trials below it still admit. A single file is stricter: any ancestor holding ``agent``,
  ``verifier`` or any sidecar refuses. A directory is a whole trial. Files are indexed by
  case-folded relative path, so ``Result.json`` is a sidecar too;
* shape is not what keeps a TEST trial out: the sidecars (``result.json``, ``config.json``,
  ``run.json``, ``grade.json``, ``provenance.json``, any letter case) of EVERY ancestor folder
  are read and held to the same checks as the rollout's own documents (split, task and trial
  names, digests, ``tasks`` lists), so an honest job above a trial passes and a TEST declaration
  anywhere above refuses;
* every trajectory and sidecar is read once, into memory, together with the hash of every file;
  the digest is checked on those same bytes and the parsed documents are what the caller serves,
  so nothing is read again after it is verified;
* a ``split`` key (any case, any depth, lists included, the agent-written ``steps`` too) must be
  exactly ``dev``, in EVERY ``.json`` file of the rollout (trajectory, sidecars, metadata,
  verifier output, bundle files), and none of those files may hold an evaluator/label key
  (``label_key`` documents the two exact exclusions). A document nested past ``DOCUMENT_DEPTH``
  is refused, and malformed JSON is refused;
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab.line.io import LABEL_KEYS, MAX_READ_BYTES, canonical_hash

SIDECARS = ("result.json", "config.json", "run.json", "grade.json", "provenance.json")
TRAJECTORIES = ("agent/trajectory.json", "trajectory.json")
DIFFS = ("final.diff", "verifier/agent.diff")
TRIAL_FOLDERS = frozenset({"agent", "verifier"})
IDENTITY_KEYS = frozenset(
    {"task_id", "task_name", "task", "trial_name", "trial_id", "id", "item_id", "record_id"}
)
DIGEST_KEYS = frozenset({"task_checksum", "task_digest", "package_digest"})
OPAQUE_KEYS = frozenset({"steps", "nodes"})  # agent-written content, not declarations
MAX_DEPTH = 8
DOCUMENT_DEPTH = 64
"""How deep a JSON file of a rollout may nest before it is refused. Real tool schemas reach 10
levels (``trajs.main.tools[].function.parameters.properties.x.items``); a document nested past
this is not searched and is refused, never skipped."""
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
    # What the line builds a rollout from, all taken from the one read:
    held: Mapping[str, bytes] = field(default_factory=dict)  # relative path -> bytes
    trajectories: tuple[tuple[str, Any], ...] = ()  # every trajectory file, parsed, in order
    sidecars: tuple[tuple[str, Any], ...] = ()  # every sidecar, parsed, in SIDECARS order
    inodes: frozenset[tuple[int, int]] = frozenset()  # (device, inode) of each file read


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
    """The listed path, which must be canonical: absolute, ASCII, spelled as on disk, and with
    no link anywhere (``os.path.realpath(listed) == listed``, so macOS ``/tmp`` is refused and
    ``/private/tmp`` is what a caller passes). Every check below runs on this one path."""
    _ascii(listed, "rollout path")
    root = Path(listed)
    if not root.is_absolute() or str(root) != listed or ".." in root.parts or "." in root.parts:
        raise AdmissionRefused(f"rollout path is not canonical and absolute: {listed}")
    prefix = Path(root.anchor)
    for part in root.parts[1:]:
        parent, prefix = prefix, prefix / part
        if stat.S_ISLNK(_lstat(prefix).st_mode):
            raise AdmissionRefused(f"symlink on the rollout path: {prefix}")
        if part not in os.listdir(parent):
            raise AdmissionRefused(f"path case does not match the filesystem: {prefix}")
        _gate(prefix)
    if os.path.realpath(listed) != listed:
        raise AdmissionRefused(f"rollout path is not its own real path: {listed}")
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
    texts: dict[str, bytes]  # relative path -> bytes, every file a caller may serve
    inodes: frozenset[tuple[int, int]]  # (device, inode) of every file in the trial
    documents: list[Any]  # every .json file that is not a sidecar or a trajectory, parsed


def _open(path: Path, expected: os.stat_result) -> Any:
    """Open ``path`` without following a link, and only if it is still the file that was seen.

    A path swapped for a link or another file after the walk's ``lstat`` is refused, not read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:  # a link swapped in after the walk (ELOOP), or the file vanished
        raise AdmissionRefused(f"file changed while it was being admitted: {path}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise AdmissionRefused(f"file changed while it was being admitted: {path}")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _read_whole(path: Path, expected: os.stat_result) -> bytes:
    if expected.st_size > MAX_READ_BYTES:
        raise AdmissionRefused(f"file is larger than {MAX_READ_BYTES} bytes: {path}")
    with _open(path, expected) as handle:
        data = handle.read(MAX_READ_BYTES + 1)  # a file that grew in place is still bounded
    if len(data) > MAX_READ_BYTES:
        raise AdmissionRefused(f"file is larger than {MAX_READ_BYTES} bytes: {path}")
    return data


def _read_tree(root: Path, sealed: frozenset[tuple[int, int]]) -> _Tree:
    """Walk the trial once: refuse links, sealed hard links and special files; hash every file;
    keep the bytes of the files the caller needs."""
    files: list[tuple[Path, str]] = []
    held: dict[str, tuple[Path, bytes]] = {}
    texts: dict[str, bytes] = {}
    inodes: set[tuple[int, int]] = set()
    others: list[Any] = []
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
                    inodes.add((mode.st_dev, mode.st_ino))
                    original = path.relative_to(root).as_posix()
                    relative = original.casefold()
                    if relative in seen:
                        raise AdmissionRefused(f"two files differ only by case: {path}")
                    seen[relative] = path
                    needed = _needed(relative)
                    is_json = relative.endswith(".json")
                    keep = needed or hd.bundle_file_eligible(original)
                    if keep or is_json:
                        data = _read_whole(path, mode)
                        if keep:
                            texts[original] = data
                        if needed:
                            held[relative] = (path, data)
                        if is_json:
                            document = _parse(path, data)
                            _refuse_document(
                                original, document, trajectory=relative in TRAJECTORIES
                            )
                            if not needed:  # sidecars and trajectories are read by the caller
                                others.append(document)
                        files.append((path, hashlib.sha256(data).hexdigest()))
                    else:
                        with _open(path, mode) as handle:
                            files.append((path, hashlib.file_digest(handle, "sha256").hexdigest()))
                else:
                    raise AdmissionRefused(f"special file inside the trial: {path}")
    # the line's own digest of a folder, computed from the hashes of the bytes read here
    ordered = sorted(files)
    digest = canonical_hash([(p.relative_to(root).as_posix(), d) for p, d in ordered])
    return _Tree(digest, held, names, texts, frozenset(inodes), others)


def _parse(path: Path, data: bytes) -> Any:
    try:
        return hd.strict_json_loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise AdmissionRefused(f"{path}: not JSON ({error})", unreadable=True) from error


def declares_non_dev(value: Any, depth: int = 0) -> bool:
    """True when any ``split`` key, in any case, is not exactly ``dev``; fails closed on a
    document nested deeper than ``DOCUMENT_DEPTH``. Nothing is skipped: a ``split`` key inside the
    agent-written ``steps`` counts too."""
    if isinstance(value, Mapping):
        if depth > DOCUMENT_DEPTH:
            return True
        for key, child in value.items():
            if isinstance(key, str) and canonical(key) == "split":
                if child is None:
                    continue
                if not isinstance(child, str) or canonical(child) != "dev":
                    return True
            elif declares_non_dev(child, depth + 1):
                return True
    elif isinstance(value, list):
        if depth > DOCUMENT_DEPTH:
            return True
        return any(declares_non_dev(child, depth + 1) for child in value)
    return False


def label_key(
    value: Any,
    depth: int = 0,
    tools: bool = False,
    names: bool = False,
    *,
    trajectory: bool = True,
) -> str | None:
    """The first evaluator/label key (``LABEL_KEYS``, any case) in a document, or ``None``.

    Exactly two parts of a document are not searched, both provably not a declaration:

    * the value of a top-level ``steps`` list of a trajectory file: what the agent wrote and what
      its tools returned (any other file of a rollout is searched whole);
    * the parameter names under a ``properties`` mapping inside a top-level ``tools`` list whose
      value is a parameter schema (a mapping): a tool schema may name a parameter ``label``.
      Their values (the parameter schemas) and every other key of a tool entry are searched.

    Everything else is searched at every depth, including a trajectory's ``nodes`` and the
    remaining ``tools`` keys. A document nested past ``DOCUMENT_DEPTH`` is not searched here:
    :func:`declares_non_dev` refuses it.
    """
    if depth > DOCUMENT_DEPTH:
        return None
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                (not names or not isinstance(child, Mapping))
                and isinstance(key, str)
                and key.lower() in LABEL_KEYS
            ):
                return key
            if trajectory and depth == 0 and key == "steps" and isinstance(child, list):
                continue
            found = label_key(
                child,
                depth + 1,
                tools or (depth == 0 and key == "tools" and isinstance(child, list)),
                tools and key == "properties" and isinstance(child, Mapping),
            )
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = label_key(child, depth + 1, tools, False)
            if found is not None:
                return found
    return None


def _refuse_document(relative: str, document: Any, *, trajectory: bool) -> None:
    """Refuse a parsed JSON file of the rollout that declares a split other than dev or holds an
    evaluator/label field. Every ``.json`` file of a rollout goes through this; only a
    trajectory file may keep its ``steps`` unsearched."""
    if declares_non_dev(document):
        raise AdmissionRefused(f"{relative}: a split declaration is not dev")
    key = label_key(document, trajectory=trajectory)
    if key is not None:
        raise AdmissionRefused(f"{relative}: evaluator/label field {key!r}")


def _get_folded(mapping: Mapping[Any, Any], wanted: str) -> Any:
    for key, value in mapping.items():
        if isinstance(key, str) and canonical(key) == wanted:
            return value
    return None


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
        folded = canonical(key) if isinstance(key, str) else ""
        if folded in IDENTITY_KEYS:
            if isinstance(child, str):
                names.append(child)
            elif isinstance(child, Mapping):
                names += [
                    v for k in ("path", "name") if isinstance(v := _get_folded(child, k), str)
                ]
        elif folded == "tasks" and isinstance(child, list):  # a job config lists its tasks
            for task in child:
                if isinstance(task, str):
                    names.append(task)
                elif isinstance(task, Mapping):
                    names += [
                        v for k in ("path", "name") if isinstance(v := _get_folded(task, k), str)
                    ]
        elif folded in DIGEST_KEYS and isinstance(child, str):
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


# Real Harbor shapes (8,386 jobs and 8,395 trials under eval-lab-results): a trial's result.json
# holds task_name/trial_name/trial_uri/agent_result/verifier_result and its config.json holds
# trial_name/task; a job's result.json holds n_total_trials/stats and its config.json holds
# job_name/tasks. A c1c5 run.json/grade.json holds task/case_id; a job-level run.json does not.
_IDENTITY_SHAPE = frozenset({"task_id", "task_checksum", "task_digest", "package_digest"})
TRIAL_SHAPE = {
    "result.json": frozenset(
        {"task_name", "trial_name", "trial_uri", "agent_result", "verifier_result"}
    )
    | _IDENTITY_SHAPE,
    "config.json": frozenset({"trial_name", "task"}) | _IDENTITY_SHAPE,
    "run.json": frozenset({"task", "case_id", "trial", "trial_id", "trial_name"}) | _IDENTITY_SHAPE,
    "grade.json": frozenset({"task", "case_id", "trial_id", "trial_name"}) | _IDENTITY_SHAPE,
}
TRIAL_ONLY = frozenset({"agent", "verifier", "trajectory.json", "final.diff", "trial.log"})


def _trial_shaped(folder: Path, listing: Mapping[str, str]) -> bool:
    """Whether ``folder`` is a trial by shape: it holds a trial-only file or folder, or a sidecar
    with a trial's keys (compared case-insensitively). A sidecar that cannot be read, is not a
    mapping, or is empty counts as trial-shaped (fail closed)."""
    if TRIAL_ONLY & listing.keys():
        return True
    for sidecar, keys in TRIAL_SHAPE.items():
        if sidecar not in listing:
            continue
        try:
            document = json.loads((folder / listing[sidecar]).read_bytes().decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return True
        if not isinstance(document, Mapping) or not document:
            return True
        if keys & {canonical(key) for key in document if isinstance(key, str)}:
            return True
    return False


def _inside_a_trial(path: Path, *, folder_rollout: bool) -> bool:
    """Whether any ancestor of ``path`` is a trial, compared case-insensitively.

    An ancestor named ``agent``/``verifier`` always is. A folder rollout is also inside a trial
    when an ancestor is trial-shaped (a job folder above many trials is not: its own sidecars
    have job keys). A single file is inside a trial when an ancestor holds ``agent``/``verifier``
    or any sidecar, which is stricter because a file's own folder holds its sidecars."""
    for folder in path.parents:
        if canonical(folder.name) in TRIAL_FOLDERS:
            return True
        try:
            listing = {canonical(name): name for name in os.listdir(folder)}
        except OSError:
            continue
        if folder_rollout:
            if _trial_shaped(folder, listing):
                return True
        elif listing.keys() & ({"agent", "verifier"} | set(SIDECARS)):
            return True
    return False


def _ancestor_documents(
    root: Path, sealed: frozenset[tuple[int, int]]
) -> tuple[list[Any], frozenset[tuple[int, int]]]:
    """The sidecars of every ancestor folder, parsed, so a folder above the rollout (a trial
    above a folder rollout, or a job above a trial) is held to the same checks as the rollout's
    own documents: read without following a link, bounded, never a hard link to sealed
    material, and refused for a non-dev split or an evaluator/label field. An ancestor sidecar
    that cannot be read refuses (fail closed). Also returns the (device, inode) of each."""
    found: list[Any] = []
    inodes: set[tuple[int, int]] = set()
    for folder in root.parents:
        try:
            listing = {canonical(name): name for name in os.listdir(folder)}
        except OSError:
            continue
        for sidecar in SIDECARS:
            if sidecar in listing:
                path = folder / listing[sidecar]
                try:
                    info = os.lstat(path)
                    if not stat.S_ISREG(info.st_mode):
                        raise AdmissionRefused(f"ancestor sidecar is not a regular file: {path}")
                    if (info.st_dev, info.st_ino) in sealed:
                        raise AdmissionRefused(f"hard link to sealed material: {path}")
                    data = _read_whole(path, info)
                except OSError as error:
                    raise AdmissionRefused(
                        f"cannot read {path}: {error}", unreadable=True
                    ) from error
                document = _parse(path, data)
                _refuse_document(str(path), document, trajectory=False)
                inodes.add((info.st_dev, info.st_ino))
                found.append(document)
    return found, frozenset(inodes)


def admit_rollout(
    listed: str,
    *,
    registry: Any,
    row: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    expected_sha256: str | None = None,
    sealed: frozenset[tuple[int, int]] = frozenset(),
    require_trajectory: bool = True,
) -> Admitted:
    """Admit the rollout at exactly ``listed`` or raise :class:`AdmissionRefused`.

    ``expected_sha256`` is the line's digest of the rollout; it is checked on the bytes read
    here, before anything is parsed. With ``require_trajectory=False`` a rollout without an ATIF
    trajectory (a patch or code bundle) is admitted with ``document=None``. ``listed`` must be
    the canonical path (``os.path.realpath(listed) == listed``); nothing else is examined.

    Everything the caller needs is returned from the one read (``held``, ``trajectories``,
    ``sidecars``, ``diff``), so a caller that builds its rollout from the result never opens a
    file of the rollout again.
    """
    root = _walk_path(listed)
    top = _lstat(root).st_mode
    documents: list[Any] = [dict(row)] if row is not None else []
    names: list[str] = [name for name in (run_id, str(root)) if name]
    candidates: list[tuple[str, Path, bytes]] = []
    sidecars: list[tuple[str, Path, bytes]] = []
    diff_bytes: bytes | None = None
    held: dict[str, bytes] = {}
    inodes: frozenset[tuple[int, int]] = frozenset()
    if stat.S_ISDIR(top):
        if canonical(root.name) in TRIAL_FOLDERS or _inside_a_trial(root, folder_rollout=True):
            raise AdmissionRefused(f"a folder inside a trial is not a trial: {root}")
        tree = _read_tree(root, sealed)
        kind, digest, names = "trial", tree.digest, names + tree.names
        candidates = [(name, *tree.held[name]) for name in TRAJECTORIES if name in tree.held]
        sidecars = [(name, *tree.held[name]) for name in SIDECARS if name in tree.held]
        for name in DIFFS:
            if name in tree.held:
                diff_bytes = tree.held[name][1]
                break
        held, inodes = tree.texts, tree.inodes
        documents += tree.documents  # every other .json file names its task and trial too
    elif stat.S_ISREG(top):
        info = _lstat(root)
        if (info.st_dev, info.st_ino) in sealed:
            raise AdmissionRefused(f"hard link to sealed material: {root}")
        if _inside_a_trial(root, folder_rollout=False):
            raise AdmissionRefused(f"a file inside a trial is not a rollout: {root}")
        data = _read_whole(root, info)
        kind, digest = "file", hashlib.sha256(data).hexdigest()
        if root.suffix.casefold() == ".json" or require_trajectory:
            candidates = [(root.name, root, data)]
            _refuse_document(root.name, _parse(root, data), trajectory=True)
        held, inodes = {root.name: data}, frozenset({(info.st_dev, info.st_ino)})
    else:
        raise AdmissionRefused(f"rollout is neither a folder nor a regular file: {root}")
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise AdmissionRefused(
            f"sha256 expected {expected_sha256.lower()} got {digest}", integrity=True
        )
    parsed_sidecars = tuple((name, _parse(path, data)) for name, path, data in sidecars)
    documents += [document for _, document in parsed_sidecars]
    ancestors, ancestor_inodes = _ancestor_documents(root, sealed)
    documents += ancestors
    inodes = inodes | ancestor_inodes
    served: tuple[Path, Any] | None = None
    trajectories: list[tuple[str, Any]] = []
    for name, path, data in candidates:
        document = _parse(path, data)
        trajectories.append((name, document))
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
    return Admitted(
        root,
        kind,
        path,
        document,
        diff,
        tuple(names),
        digest,
        held=held,
        trajectories=tuple(trajectories),
        sidecars=parsed_sidecars,
        inodes=inodes,
    )
