"""Stored witness route validation, split guards, and evidence custody."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

_SOLVE_WRITE_RE = re.compile(
    r"(?:cat\s*>\s*|tee\s+|cp\s+\S+\s+|mv\s+\S+\s+|write_text\(|open\()['\"]?([A-Za-z0-9_./\-]+)"
)


class SeekerRefusalError(ValueError):
    """Raised when a seeker job violates split, measurability, or differential gates."""


MECHANISM_CLASSES = frozenset(
    {"weak_check", "grader_tamperable", "answer_reachable", "exploit_grader_gap"}
)


class SeekerEvidenceError(RuntimeError):
    """Declared witness evidence is missing or cannot be pinned immutably."""


def _omp_stream_messages(stream_text: str) -> list[Mapping[str, Any]]:
    """Messages carried by the ``agent_end`` events of an OMP JSON event stream."""
    messages: list[Mapping[str, Any]] = []
    for line in stream_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, Mapping) and event.get("type") == "agent_end":
            messages.extend(m for m in event.get("messages") or () if isinstance(m, Mapping))
    return messages


def _route_matches(requested: str, provider: str, model: str) -> bool:
    """Whether ``provider``/``model`` is the requested ``provider/model`` route.

    A ``:thinking`` selector on either side is not part of the route, and a
    vendor-prefixed route served through ``openrouter`` (e.g.
    ``meta/muse-spark-open`` served as ``provider='openrouter'``,
    ``model='meta/muse-spark-open'``) matches that exact vendor/model pair.
    """
    expected_provider, _, expected_model = requested.partition("/")
    want_provider = expected_provider.strip()
    want_model = expected_model.split(":", 1)[0].strip()
    got_provider = provider.strip()
    got_model = model.split(":", 1)[0].strip()
    if got_provider == want_provider and got_model == want_model:
        return True
    return got_provider == "openrouter" and got_model == f"{want_provider}/{want_model}"


def _stream_served_route(
    messages: Sequence[Mapping[str, Any]], requested: str | None
) -> tuple[str | None, str | None]:
    """Provider/model that answered an OMP session, counting every assistant message.

    A session answered by more than one route reports the first route that is not
    the requested one, so a mid-session fallback reads as a mismatch instead of
    inheriting the requested route's name.
    """
    routes = [
        (str(message["provider"]), str(message["model"]))
        for message in messages
        if message.get("role") == "assistant" and message.get("provider") and message.get("model")
    ]
    if requested and "/" in requested:
        for provider, model in routes:
            if not _route_matches(requested, provider, model):
                return provider, model
    return routes[0] if routes else (None, None)


def compute_candidate_digest(files: Mapping[str, str | bytes]) -> str:
    """Deterministic digest of the executable submission payload (excluding notes.json)."""
    hasher = hashlib.sha256()
    executable_keys = [k for k in sorted(files) if k != "notes.json"]
    keys_to_hash = executable_keys or sorted(files)
    for key in keys_to_hash:
        raw = files[key]
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if key == "solve.sh" and isinstance(raw, str):
            stripped_lines = [
                line.strip()
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            data = "\n".join(stripped_lines).encode("utf-8")
        hasher.update(key.encode("utf-8") + b"\0" + data + b"\0")
    return "sha256:" + hasher.hexdigest()


def _evidence_content_digest(path: Path) -> str:
    """Full sha256 over a file's bytes or a tree's canonical manifest.

    Every member's relative path and file digest participates, so changed
    receipt bytes always change the digest. Symlinks and non-file members are
    refused outright: evidence custody must name real immutable bytes, never
    an indirection that can drift after the pin.
    """
    if path.is_symlink():
        raise SeekerEvidenceError(f"evidence path is a symlink: {path}")
    if path.is_file():
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        raise SeekerEvidenceError(f"declared evidence source is missing: {path}")
    entries: list[tuple[str, str]] = []
    for member in sorted(path.rglob("*")):
        relative = member.relative_to(path).as_posix()
        if member.is_symlink():
            raise SeekerEvidenceError(f"evidence tree contains a symlink: {member}")
        if member.is_dir():
            entries.append((relative + "/", "-"))
        elif member.is_file():
            entries.append((relative, hashlib.sha256(member.read_bytes()).hexdigest()))
        else:
            raise SeekerEvidenceError(f"evidence tree holds a non-file member: {member}")
    aggregate = hashlib.sha256()
    for relative, digest in entries:
        aggregate.update(f"{digest}  ./{relative}\n".encode())
    return "sha256:" + aggregate.hexdigest()


def _evidence_custody_key(
    *,
    job_event_id: str,
    run_id: str,
    task_id: str,
    candidate_digest: str,
    original_task: Path,
    repaired_task: str,
    sources: Mapping[str, str | None],
) -> str:
    """Witness-custody key binding job, package, candidate, and source bytes.

    The pinned witness directory is keyed on the full source content digests
    plus the job and package identities — never on the truncated candidate
    digest alone — so the same candidate replayed against changed original or
    repaired packages, or under another job, can never silently reuse an
    earlier pin's receipts. A missing or undigestable declared source fails
    closed: execution proof stays unavailable rather than a confirmed asset
    carrying invalid references.
    """
    from evallab.registry import task_directory_digest

    material: dict[str, Any] = {
        "job": job_event_id,
        "run_id": run_id,
        "task_id": task_id,
        "candidate_digest": candidate_digest,
        "original_task": str(original_task),
        "repaired_task": repaired_task,
        "package_digests": {},
        "sources": {},
    }
    for name, package in (("original_task", original_task), ("repaired_task", Path(repaired_task))):
        package_path = Path(package)
        try:
            if package_path.is_dir():
                material["package_digests"][name] = task_directory_digest(package_path)
            else:
                material["package_digests"][name] = _evidence_content_digest(package_path)
        except (OSError, ValueError, SeekerEvidenceError) as exc:
            raise SeekerEvidenceError(f"{name} package is undigestable: {exc}") from exc
    for slot, source in sources.items():
        if not isinstance(source, str) or not source:
            raise SeekerEvidenceError(f"{slot} evidence is not declared")
        material["sources"][slot] = _evidence_content_digest(Path(source))
    return "sha256:" + hashlib.sha256(
        json.dumps(material, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _copy_evidence_tree(source: Path, stage: Path) -> None:
    """Copy a tree into ``stage``, refusing symlinked or non-file members."""
    stage.mkdir(parents=True)
    for member in sorted(source.rglob("*")):
        target = stage / member.relative_to(source)
        if member.is_symlink():
            raise SeekerEvidenceError(f"evidence tree contains a symlink: {member}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(member, target)
        else:
            raise SeekerEvidenceError(f"evidence tree holds a non-file member: {member}")


def _publish_evidence_tree(source: Path, destination: Path) -> None:
    """Publish a tree at ``destination`` atomically under content custody.

    The copy is staged on a sibling temporary path and renamed, so an
    interrupted publish leaves no partial destination for a later retry to
    accept. An existing destination is kept only when it holds byte-identical
    content; a symlink, a non-directory, or different bytes are refused —
    first-writer wins only when both writers carried the same evidence.
    """
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise SeekerEvidenceError(f"refusing to pin over non-directory: {destination}")
    if destination.exists():
        if _evidence_content_digest(destination) == _evidence_content_digest(source):
            return
        raise SeekerEvidenceError(
            f"pinned evidence at {destination} conflicts with new source bytes"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _copy_evidence_tree(source, stage)
        os.replace(stage, destination)
    except OSError as exc:
        # A concurrent pinner may have published between the check and the
        # rename; identical bytes are a no-op, anything else is refused.
        if destination.is_dir() and not destination.is_symlink():
            if _evidence_content_digest(destination) == _evidence_content_digest(source):
                return
            raise SeekerEvidenceError(
                f"pinned evidence at {destination} conflicts with new source bytes"
            ) from exc
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _publish_evidence_file(source: Path, target: Path) -> None:
    """Publish one file atomically; identical bytes are a no-op, others refuse."""
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SeekerEvidenceError(f"refusing to pin over non-file: {target}")
    data = source.read_bytes()
    if target.is_file() and target.read_bytes() == data:
        return
    if target.exists():
        raise SeekerEvidenceError(
            f"pinned evidence at {target} conflicts with new source bytes"
        )
    stage = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with stage.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hardlink publication can never overwrite an existing entry.
            os.link(stage, target)
        except FileExistsError as exc:
            if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
                return
            raise SeekerEvidenceError(
                f"pinned evidence at {target} conflicts with new source bytes"
            ) from exc
        except OSError as exc:
            # Filesystems without hardlink support fall back to rename after a
            # final existence check.
            if target.exists():
                if (
                    target.is_file()
                    and not target.is_symlink()
                    and target.read_bytes() == data
                ):
                    return
                raise SeekerEvidenceError(
                    f"pinned evidence at {target} conflicts with new source bytes"
                ) from exc
            os.replace(stage, target)
    finally:
        stage.unlink(missing_ok=True)


def _pin_evidence(source: str | None, destination: Path) -> str:
    """Pin witness evidence outside the mutable attempt workspace.

    Confirmed ``seeker.exploit_confirmed``/``asset.verified`` payloads
    reference run receipts and the solution bundle from the durable
    journal, but ``attempt-<slot>`` workspaces are wiped and reused by
    later jobs for the same task — confirmed evidence is published under a
    custody-keyed ``witness-*/`` sibling instead. Publication is staged
    then renamed atomically, an existing destination must hold identical
    bytes, and symlinks or conflicting bytes are refused. A declared
    source that is missing or unreadable raises ``SeekerEvidenceError``:
    execution proof stays unavailable rather than the confirmation
    falling back to a mutable workspace path with invalid references.
    """
    if not isinstance(source, str) or not source:
        raise SeekerEvidenceError("declared evidence source is empty")
    src = Path(source)
    if src.is_symlink() or not src.exists():
        raise SeekerEvidenceError(f"declared evidence source is missing: {source}")
    if src.is_dir():
        _publish_evidence_tree(src, destination)
        return str(destination)
    if src.is_file():
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise SeekerEvidenceError(
                f"refusing to pin under non-directory: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / src.name
        _publish_evidence_file(src, target)
        return str(target)
    raise SeekerEvidenceError(f"declared evidence source is unusable: {source}")


@lru_cache(maxsize=8)
def _load_assignments_csv_index(csv_path: str) -> dict[str, tuple[tuple[str, str], ...]]:
    import csv as _csv

    by_bare: dict[str, set[tuple[str, str]]] = {}
    path = Path(csv_path)
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = _csv.DictReader(handle)
        for row in reader:
            key_id = str(row.get("key_id") or "").strip()
            split = str(row.get("split") or "").strip().casefold()
            if not key_id or not split:
                continue
            for col in ("item_id", "task", "unit"):
                val = str(row.get(col) or "").strip()
                if not val:
                    continue
                bare = val.split("@", 1)[0].rsplit("/", 1)[-1]
                by_bare.setdefault(bare, set()).add((key_id, split))
                if "__" in bare:
                    by_bare.setdefault(bare.split("__")[-1], set()).add((key_id, split))
    return {k: tuple(sorted(v)) for k, v in by_bare.items()}


def find_cross_key_non_dev_assignments(
    task_id: str,
    *,
    assignments_csv: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return ``(key_id, split)`` pairs where ``task_id`` is non-DEV in ``assignments.csv``."""
    csv_file = (
        Path(assignments_csv)
        if assignments_csv is not None
        else Path(__file__).resolve().parents[3] / "research/answer-keys/splits/assignments.csv"
    )
    index = _load_assignments_csv_index(str(csv_file.resolve()))
    raw = str(task_id or "").strip()
    bare = raw.split("@", 1)[0].rsplit("/", 1)[-1]
    candidates = {bare}
    if "__" in bare:
        candidates.add(bare.split("__")[-1])
    non_dev: set[tuple[str, str]] = set()
    for cand in candidates:
        for key_id, split in index.get(cand, ()):
            if split not in {"dev", "train"}:
                non_dev.add((key_id, split))
    return tuple(sorted(non_dev))


def find_cross_key_dev_assignments(
    task_id: str,
    *,
    assignments_csv: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return ``(key_id, split)`` pairs where ``task_id`` is affirmatively DEV/train in ``assignments.csv``."""
    csv_file = (
        Path(assignments_csv)
        if assignments_csv is not None
        else Path(__file__).resolve().parents[3] / "research/answer-keys/splits/assignments.csv"
    )
    index = _load_assignments_csv_index(str(csv_file.resolve()))
    raw = str(task_id or "").strip()
    bare = raw.split("@", 1)[0].rsplit("/", 1)[-1]
    candidates = {bare}
    if "__" in bare:
        candidates.add(bare.split("__")[-1])
    dev_rows: set[tuple[str, str]] = set()
    for cand in candidates:
        for key_id, split in index.get(cand, ()):
            if split in {"dev", "train"}:
                dev_rows.add((key_id, split))
    return tuple(sorted(dev_rows))


def require_cross_key_dev_only(
    task_id: str,
    *,
    assignments_csv: Path | None = None,
    require_affirmative_dev: bool = False,
) -> None:
    """Raise ``SeekerRefusalError`` if ``task_id`` has any test/heldout row in ``assignments.csv``."""
    non_dev = find_cross_key_non_dev_assignments(task_id, assignments_csv=assignments_csv)
    if non_dev:
        raise SeekerRefusalError(
            f"task {task_id!r} has non-DEV split in secondary assignments.csv keys: {list(non_dev)}"
        )
    if require_affirmative_dev and not find_cross_key_dev_assignments(
        task_id, assignments_csv=assignments_csv
    ):
        raise SeekerRefusalError(
            f"task {task_id!r} has no affirmative DEV assignment in assignments.csv"
        )


def _parse_omp_json_stdout(raw_text: str) -> tuple[str, str | None, str | None]:
    """Return final text and actual provider/model from OMP's JSON event stream."""
    text = ""
    served_provider: str | None = None
    served_model: str | None = None
    for message in _omp_stream_messages(raw_text):
        if message.get("role") != "assistant":
            continue
        parts = [
            part.get("text", "")
            for part in message.get("content") or []
            if part.get("type") == "text"
        ]
        if any(parts):
            text = "".join(parts)
            served_provider = str(message["provider"]) if message.get("provider") else None
            served_model = str(message["model"]) if message.get("model") else None
    return text, served_provider, served_model
