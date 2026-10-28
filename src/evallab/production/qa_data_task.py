"""Read-only task packages for the admitted DEV ``qa-data`` surface."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import Any

from evallab.production.corpus_queue import CorpusItem, CorpusRegistry
from evallab.production.ingest import _package_locators
from evallab.production.qa_data import QaContext, QaDataError, estimate_tokens
from evallab.registry import _should_ignore_file
from evallab.storage.paths import shared_checkout_root

# The verifier-aware bundle also withholds tests/{data,fixtures,hidden_data,golden-files}.
# Apply the same rule to explicitly requested files and to aliases of protected paths.
_WITHHELD_TOKENS = frozenset({
    "solution", "solutions", "golden", "oracle", "answer", "answers",
    "answerkey", "answerkeys", "answersheet", "answersheets", "answerfile", "answerfiles",
})
_WITHHELD_NAMES = _WITHHELD_TOKENS | {
    "cheat", "cheats", "gold", "goldendata", "goldenfiles", "groundtruth",
    "hiddendata", "reference", "references", "solve",
}
_VERIFIER_DIRS = frozenset({"tests", "test", "grader", "graders", "verifier", "verifiers"})
_TEST_COMMANDS = frozenset({
    "tests/test.sh", "tests/test_command.sh", "tests/run_tests.sh", "tests/command.sh",
})
_FILE_UNAVAILABLE = "file not available: expected an allowed relative task-package file"


def _name_tokens(name: str) -> tuple[str, ...]:
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    split = re.sub(r"([a-z])([A-Z])", r"\1 \2", split)
    tokens = [token.casefold() for token in re.findall(r"[A-Za-z]+|[0-9]+", split)]
    while tokens and (tokens[-1].isdigit() or tokens[-1] in {"v", "version", "final"}):
        tokens.pop()
    return tuple(tokens)


def _withheld(relative: Path) -> bool:
    parts = [_name_tokens(part) for part in relative.parts]
    if any(_WITHHELD_TOKENS.intersection(part) or "".join(part) in _WITHHELD_NAMES for part in parts):
        return True
    # Keep legacy basename guards (solve.sh, ground_truth.json), in addition to
    # the whole-component token check that catches foo.solution/foo.answer_key.
    if any("".join(_name_tokens(Path(part).stem)) in _WITHHELD_NAMES for part in relative.parts):
        return True
    joined = ["".join(part) for part in parts]
    if joined and joined[0] in {"tests", "test"} and {"data", "fixtures"}.intersection(joined):
        return True
    # DEV tests, including expected/expected_output files and their expected
    # values, are task content that QA reviews; those names are not answer keys.
    # MiMo's test.patch is the DEV verifier, not the reference solution patch.
    return relative.suffix.casefold() in {".patch", ".diff"} and relative.as_posix() != "tests/test.patch"


def _relative_file(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise QaDataError(_FILE_UNAVAILABLE)
    path = Path(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in path.parts or _withheld(path):
        raise QaDataError(_FILE_UNAVAILABLE)
    return path


def _requested_file(package: Path, relative: Path) -> Path:
    try:
        path = (package / relative).resolve(strict=True)
        if not path.is_relative_to(package) or _withheld(path.relative_to(package)) or not path.is_file():
            raise QaDataError(_FILE_UNAVAILABLE)
        return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise QaDataError(_FILE_UNAVAILABLE) from exc


def _streaming_digest(package: Path) -> str:
    """Match registry task-directory digests without whole-file allocations.

    Sorted depth-first paths have the registry's sorted Path ordering. Reuse its
    file exclusion policy and ingest's locators, but reject escaping symlinks
    before any target bytes are opened. Like Path.rglob, do not walk linked dirs.
    """
    def paths(directory: Path) -> Iterator[Path]:
        for path in sorted(directory.iterdir()):
            if path.is_dir():
                if not path.is_symlink():
                    yield from paths(path)
                continue
            # Mirror registry eligibility before strict resolution: dangling
            # links and ignored entries do not participate in the digest.
            if not path.is_file() or _should_ignore_file(path):
                continue
            if not path.resolve(strict=True).is_relative_to(package):
                raise ValueError("task package contains an escaping symlink")
            yield path

    aggregate = hashlib.sha256()
    count = 0
    for path in paths(package):
        digest = hashlib.sha256()
        with path.resolve(strict=True).open("rb") as stream:
            while block := stream.read(64 * 1024):
                digest.update(block)
        relative = path.relative_to(package).as_posix()
        aggregate.update(f"{digest.hexdigest()}  ./{relative}\n".encode())
        count += 1
    if not count:
        raise ValueError("task package is empty")
    return "sha256:" + aggregate.hexdigest()


def _candidate_locators(item: CorpusItem, root: Path) -> Iterator[Path]:
    """Reuse ingest's locator policy, isolating resolution failures per source."""
    metadata = item.metadata
    materialization = metadata.get("materialization") or {}
    package = metadata.get("package") or {}
    sources = (
        {"materialization": {"resolved_locator": materialization.get("resolved_locator")}},
        {"materialization": {"locator": materialization.get("locator")}},
        {"task_package": metadata.get("task_package")},
        {"package": {"path": package.get("path")}},
        {"package": {"root_hint": package.get("root_hint"), "name": package.get("name")}},
    )
    seen: set[Path] = set()
    for source in sources:
        try:
            locators = _package_locators(replace(item, metadata=source), root)
        except (OSError, RuntimeError, ValueError):
            continue
        for locator in locators:
            if locator not in seen:
                seen.add(locator)
                yield locator


def _package(ctx: QaContext, row: dict[str, Any]) -> Path | None:
    """Use ingest's registry locators with exact identities and bounded hashing."""
    registry = ctx.retriever.registry
    if not isinstance(registry, CorpusRegistry):
        return None
    dataset, task_id = row["dataset_id"], row["task_id"]
    metadata = row.get("metadata_json") or {}
    item_id = metadata.get("item_id") if isinstance(metadata, dict) else None
    candidates: list[CorpusItem] = []
    for identity in (item_id, task_id):
        if not isinstance(identity, str):
            continue
        try:
            item = registry.get(dataset, identity)
        except KeyError:
            continue
        if item.split == "dev" and item.task_id == task_id:
            candidates = [item]
            break
    if not candidates:
        candidates = [
            item for item in registry.items(dataset_id=dataset, split="dev")
            if item.task_id == task_id
        ]
        # Duplicate finding records can describe one package; different versions
        # cannot be selected by a mutable latest-version or bare-name heuristic.
        if len({item.task_digest for item in candidates}) != 1:
            return None
    else:
        selected = candidates[0]
        candidates.extend(
            item for item in registry.items(dataset_id=dataset, split="dev")
            if item.task_id == task_id and item.item_id != selected.item_id
            and item.task_digest == selected.task_digest
        )
    roots = dict.fromkeys((ctx.repo_root.resolve(), shared_checkout_root(ctx.repo_root)))
    for item in candidates:
        if item.task_id != task_id or item.task_digest is None:
            continue
        if registry.assigned_test_anywhere(item_id=item.item_id, task_id=item.task_id):
            continue
        for root in roots:
            for locator in _candidate_locators(item, root):
                try:
                    path = locator.resolve(strict=True)
                    if not path.is_dir() or not (path / "task.toml").is_file():
                        continue
                    if _streaming_digest(path) == item.task_digest:
                        return path
                except (OSError, RuntimeError, ValueError):
                    continue
    return None


def _inventory(package: Path) -> tuple[dict[str, int], list[str]]:
    files: dict[str, int] = {}
    withheld: list[str] = []
    for directory, dirs, names in os.walk(package, followlinks=False):
        base = Path(directory)
        kept = []
        for name in sorted(dirs):
            path = base / name
            relative = path.relative_to(package)
            if _withheld(relative) or path.is_symlink():
                withheld.append(relative.as_posix() + "/")
            elif name not in {".git", "__pycache__", ".pytest_cache"}:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(package)
            if _withheld(relative) or path.is_symlink():
                withheld.append(relative.as_posix())
                continue
            if (
                relative.parts[0] not in _VERIFIER_DIRS | {"environment"}
                and relative.as_posix() != "task.toml"
                and not name.casefold().startswith(("grader", "verifier", "verify", "test_"))
            ):
                continue
            try:
                if path.is_file():
                    files[relative.as_posix()] = path.stat().st_size
            except OSError:
                continue
    return files, sorted(withheld)


def _priority(relative: str) -> tuple[int, str]:
    path = Path(relative)
    if path.parts[0] == "environment" and path.name.casefold().startswith("dockerfile"):
        return 0, relative
    if relative in _TEST_COMMANDS:
        return 1, relative
    if path.parts[0] in _VERIFIER_DIRS or path.name.casefold().startswith(("grader", "verifier", "verify", "test_")):
        return 2, relative
    return (3 if relative == "task.toml" else 4), relative


def task(ctx: QaContext, task_id: str, *, files: Sequence[str] = ()) -> dict[str, Any]:
    """Instruction, verifier inventory and whole prioritized files of one DEV task."""
    row = ctx.require_task(task_id)  # Admission precedes registry/package/file reads.
    requested = [_relative_file(value) for value in files]
    package = _package(ctx, row)
    if requested and package is None:
        raise QaDataError(_FILE_UNAVAILABLE)
    inventory, withheld = _inventory(package) if package is not None else ({}, [])
    requested_paths: dict[str, Path] = {}
    if package is not None:
        for relative in requested:
            path = _requested_file(package, relative)
            requested_paths[relative.as_posix()] = path
            inventory.setdefault(relative.as_posix(), path.stat().st_size)
            withheld = [name for name in withheld if name != relative.as_posix()]
    contents = sorted((name for name in inventory if _priority(name)[0] < 4), key=_priority)
    contents.extend(name for name in requested_paths if name not in contents)
    omitted = {"files": len(inventory), "contents": len(contents), "withheld": len(withheld)}
    instruction = row["instruction_text"]
    bundle: dict[str, Any] | None = (
        {"files": [], "contents": [], "withheld": [], "omitted": omitted.copy()}
        if package is not None else None
    )
    data: dict[str, Any] = {
        "task_id": task_id, "dataset": row["dataset_id"], "instruction": "",
        "instruction_chars": len(instruction), "instruction_truncated": False,
        "package": bundle, "original_grader_digest": row["original_grader_digest"],
        "repaired_grader_digest": row["repaired_grader_digest"],
        "omitted": sum(omitted.values()) + 1, "truncated": False,
    }
    if package is None:
        data["reason"] = "no_local_package"
    # Reserve all JSON framing and maximum-width omission counters before text or
    # list items. A too-small allowance is a refusal, not an over-budget payload.
    if not ctx.budget.take(data):
        raise QaDataError("budget too small for task metadata")
    low, high = 0, len(instruction)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens({"instruction": instruction[:middle]}) <= ctx.budget.remaining:
            low = middle
        else:
            high = middle - 1
    data["instruction"] = instruction[:low]
    if low:
        ctx.budget.take({"instruction": data["instruction"]})
    cut = low < len(instruction)
    data["instruction_truncated"] = cut
    if cut:
        ctx.budget.omitted += 1

    def add(section: str, item: Any) -> None:
        assert bundle is not None
        if not ctx.budget.take(item):
            return
        bundle[section].append(item)
        # Budget.take estimates a record; the complete payload also has list
        # separators. Check exact compact JSON before accepting each whole item.
        if estimate_tokens(data) > ctx.budget.limit:
            bundle[section].pop()
            ctx.budget.used -= estimate_tokens(item)
            ctx.budget.omitted += 1
            return
        omitted[section] -= 1

    if bundle is not None and package is not None:
        for name in withheld:
            add("withheld", name)
        for name, size in sorted(inventory.items(), key=lambda pair: _priority(pair[0])):
            add("files", {"path": name, "bytes": size})
        for name in contents:
            try:
                path = requested_paths.get(name) or _requested_file(package, Path(name))
                # UTF-8 needs at most four bytes per character. A larger file
                # cannot fit even before JSON overhead; never allocate it all.
                with path.open("rb") as stream:
                    raw = stream.read(ctx.budget.remaining * 16 + 1)
                if len(raw) > ctx.budget.remaining * 16:
                    ctx.budget.omitted += 1
                    continue
                text = raw.decode("utf-8")
                if "\x00" in text:
                    ctx.budget.omitted += 1
                    continue
            except (OSError, UnicodeError, QaDataError):
                ctx.budget.omitted += 1
                continue
            add("contents", {"path": name, "text": text})
        bundle["omitted"] = omitted
    data["omitted"] = sum(omitted.values()) + int(cut)
    data["truncated"] = data["omitted"] > 0
    return data


__all__ = ["task"]
