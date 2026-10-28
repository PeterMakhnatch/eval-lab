"""Read-only task packages for the admitted DEV ``qa-data`` surface."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from evallab.production.cloud_trace_db import S3CompatibleCAS
from evallab.production.corpus_queue import CorpusItem, CorpusRegistry
from evallab.production.ingest import _package_locators
from evallab.production.qa_data import UNAVAILABLE, QaContext, QaDataError, estimate_tokens
from evallab.production.run_atlas import outcome_from_reward
from evallab.registry import _should_ignore_file
from evallab.storage.paths import shared_checkout_root

# The verifier-aware bundle also withholds tests/{data,fixtures,hidden_data,golden-files}.
# Apply the same rule to explicitly requested files and to aliases of protected paths.
_WITHHELD_TOKENS = frozenset({
    "solution", "solutions", "golden", "oracle", "answer", "answers",
    "answerkey", "answerkeys", "answersheet", "answersheets", "answerfile", "answerfiles",
    "exposure", "exposures", "label", "labels",
})
_WITHHELD_NAMES = _WITHHELD_TOKENS | {
    "cheat", "cheats", "gold", "goldendata", "goldenfiles", "groundtruth",
    "hiddendata", "reference", "references", "solve",
}
_SCORING_KEY_NAMES = frozenset({
    "expectedverdict", "expectedcategory", "scoreronlykey", "scoreronlykeys",
    "scoringkey", "scoringkeys", "scorerkey", "scorerkeys", "labelkey", "labelkeys",
    "goldkey", "goldkeys", "groundtruth",
})
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
    # Match scoring-key components through casing, separators and arbitrary
    # extensions, without confusing ordinary DEV expected-output fixtures.
    if any(marker in component for component in joined for marker in _SCORING_KEY_NAMES):
        return True
    if joined and joined[0] in {"tests", "test"} and {"data", "fixtures"}.intersection(joined):
        return True
    # DEV tests, including expected/expected_output files and their expected
    # values, are task content that QA reviews; those names are not answer keys.
    # MiMo's test.patch is the DEV verifier, not the reference solution patch.
    return relative.suffix.casefold() in {".patch", ".diff"} and relative.as_posix() != "tests/test.patch"


def _relative_file(value: str, *, allow_withheld: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise QaDataError(_FILE_UNAVAILABLE)
    path = Path(value)
    if (
        not path.parts or path.is_absolute() or PureWindowsPath(value).is_absolute()
        or ".." in path.parts or (not allow_withheld and _withheld(path))
    ):
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


@dataclass
class _Package:
    """One verified CAS manifest, or the digest-bound local registry fallback."""

    files: dict[str, dict[str, Any]]
    withheld: list[str]
    file_count: int | None
    omissions: int = 0
    path: Path | None = None
    available: bool = True


def _cas_packages(cas: S3CompatibleCAS, uri: str | None) -> dict[str, _Package]:
    if not uri:
        return {}
    try:
        bundle = cas.get_bundle(uri)
        variants = bundle["metadata"]["variants"]
        if not isinstance(variants, list):
            raise ValueError("invalid task variants")
    except (OSError, ValueError, KeyError, TypeError):
        return {"0:original_package": _Package({}, [], None, available=False)}
    packages = {}
    for number, variant in enumerate(variants):
        if not isinstance(variant, dict):
            continue
        for kind in ("original_package", "repaired_package"):
            reference = variant.get(kind)
            if not reference:
                continue
            key = f"{number}:{kind}"
            try:
                manifest = cas.get_bundle(reference["cas_uri"])
                records = manifest["files"]
                if not isinstance(records, list):
                    raise ValueError("invalid package files")
                inventory, withheld = {}, []
                for record in records:
                    relative = _relative_file(record["relative_path"], allow_withheld=True)
                    name = relative.as_posix()
                    if _withheld(relative):
                        withheld.append(name)
                    else:
                        inventory[name] = record
                packages[key] = _Package(
                    inventory, sorted(withheld), len(records), len(manifest.get("omissions", [])),
                )
            except (OSError, ValueError, KeyError, TypeError):
                packages[key] = _Package({}, [], None, available=False)
    return packages


def _task_rows(
    ctx: QaContext, task_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    canonical = ctx.require_task(task_id)["task_id"]
    # require_task owns its snapshot. Re-admit its canonical identity here so
    # every returned database value comes from this one non-nested snapshot.
    with ctx.database._transaction():
        admitted, _sealed = ctx.retriever._admitted_ids()
        rows = ctx.database._rows(
            "SELECT * FROM corpus_tasks WHERE task_id = ?", (canonical,),
        ) if canonical in admitted else []
        if not rows:
            raise QaDataError(UNAVAILABLE)
        issues = ctx.database._rows(
            "SELECT issue_id, defect_class, direction, status, trust_tier "
            "FROM task_issues WHERE task_id = ? ORDER BY issue_id", (canonical,),
        )
        runs = ctx.database._rows(
            "SELECT a.trace_id, a.reward, a.step_count, f.run_id AS faceted, f.tests, "
            "f.answer_material_accessed, f.grader_or_harness_touched, f.suspicion "
            "FROM agent_traces a LEFT JOIN run_facets f ON f.run_id = a.trace_id AND f.task_id = a.task_id "
            "WHERE a.task_id = ? ORDER BY a.trace_id", (canonical,),
        )
    return rows[0], issues, runs


def _text_prefix(ctx: QaContext, text: str, record: dict[str, Any], field: str, *, listed: bool) -> str | None:
    """Fit escaped UTF-8 text plus its complete record, not just its raw characters."""
    def cost(length: int) -> int:
        record[field] = text[:length]
        return estimate_tokens([record] if listed else record)

    if cost(0) > ctx.budget.remaining:
        return None
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if cost(middle) <= ctx.budget.remaining:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def task(
    ctx: QaContext, task_id: str, *, files: Sequence[str] = (), variant: int = 0, kind: str = "original_package",
) -> dict[str, Any]:
    """Instruction, local packages, retained issue metadata and every run of one admitted DEV task."""
    row, issues, runs = _task_rows(ctx, task_id)
    task_id = row["task_id"]
    requested = list(dict.fromkeys(_relative_file(value).as_posix() for value in files))
    if variant < 0 or kind not in {"original_package", "repaired_package"}:
        raise QaDataError("expected a nonnegative --variant and --kind original_package or repaired_package")
    cas = S3CompatibleCAS(local_directory=ctx.state_dir / "cas", bucket="")
    packages = _cas_packages(cas, row["cas_uri"])
    if not any(package.available for package in packages.values()):
        local = _package(ctx, row)
        if local is not None:
            inventory, withheld = _inventory(local)
            packages["0:original_package"] = _Package(
                {name: {"bytes": size} for name, size in inventory.items()},
                withheld, len(inventory), path=local,
            )
    if not packages:
        packages["0:original_package"] = _Package({}, [], None, available=False)
    selected_key = f"{variant}:{kind}"
    selected = packages.get(selected_key)
    if requested:
        if selected is None:
            raise QaDataError("not in that package")
        if not selected.available and not row["cas_uri"]:
            raise QaDataError(_FILE_UNAVAILABLE)
        if selected.available:
            for name in requested:
                if selected.path is not None:
                    try:
                        path = _requested_file(selected.path, Path(name))
                    except QaDataError:
                        if not (selected.path / name).exists() and not (selected.path / name).is_symlink():
                            raise QaDataError("not in that package") from None
                        raise
                    if name not in selected.files:
                        selected.files[name] = {"bytes": path.stat().st_size}
                        selected.file_count = len(selected.files)
                    selected.withheld = [value for value in selected.withheld if value != name]
                elif name not in selected.files:
                    raise QaDataError("not in that package")
    contents = {
        key: (
            list(requested) if key == selected_key and package.available else []
        ) for key, package in packages.items()
    }
    for key, package in packages.items():
        if package.path is not None:
            contents[key].extend(
                name for name in sorted(package.files, key=_priority)
                if _priority(name)[0] < 4 and name not in contents[key]
            )
    instruction = row["instruction_text"]
    summaries: dict[str, dict[str, Any]] = {
        key: {
            "source": "registry" if package.path is not None else "cas",
            "package": "unavailable", "file_count": package.file_count,
            "files": [], "contents": [], "withheld": [], "omissions": package.omissions,
            "omitted": {
                "files": len(package.files), "contents": len(contents[key]), "withheld": len(package.withheld),
            },
        } for key, package in packages.items()
    }
    maximum_omitted = 1 + len(issues) + len(runs) + sum(
        sum(summary["omitted"].values()) for summary in summaries.values()
    )
    data: dict[str, Any] = {
        "task_id": task_id, "dataset": row["dataset_id"], "instruction": "",
        "instruction_chars": len(instruction), "instruction_truncated": False,
        "original_grader_digest": row["original_grader_digest"],
        "repaired_grader_digest": row["repaired_grader_digest"],
        "packages": summaries, "issues": [], "issues_total": len(issues), "issues_omitted": len(issues),
        "runs": {
            "total": len(runs), "faceted": sum(run["faceted"] is not None for run in runs),
            "suspicion_ge_2": sum((run["suspicion"] or 0) >= 2 for run in runs),
            "list": [], "omitted": len(runs),
        },
        "how_to_read_a_file": f"evallab qa-data task {task_id} --file REL [--variant N --kind repaired_package]",
        "omitted": maximum_omitted, "truncated": False,
    }
    if not ctx.budget.take(data):
        raise QaDataError("budget too small for task metadata")
    initial_omitted = ctx.budget.omitted
    prefix = _text_prefix(ctx, instruction, {"instruction": ""}, "instruction", listed=False)
    if prefix is not None:
        ctx.budget.take({"instruction": prefix})
        data["instruction"] = prefix
    data["instruction_truncated"] = len(data["instruction"]) < len(instruction)

    def add(target: list[Any], item: Any) -> bool:
        # Charging the list wrapper also covers separators in the final JSON.
        if ctx.budget.take([item]):
            target.append(item)
            return True
        return False

    partial_contents = 0

    def read_content(key: str, name: str, *, explicit: bool) -> None:
        nonlocal partial_contents
        package, summary = packages[key], summaries[key]
        size = package.files[name].get("bytes", package.files[name].get("size"))
        item: dict[str, Any] = {"relative_path": name, "size": size, "text": "", "truncated": False}
        if estimate_tokens([item]) > ctx.budget.remaining:
            return
        try:
            if package.path is None:
                raw = cas.get_bytes(package.files[name]["cas_uri"])
            else:
                path = _requested_file(package.path, Path(name))
                with path.open("rb") as stream:
                    raw = stream.read() if explicit else stream.read(ctx.budget.remaining * 16 + 1)
                if not explicit and len(raw) > ctx.budget.remaining * 16:
                    return
            item["size"] = len(raw)
            try:
                text = raw.decode("utf-8")
                if "\x00" in text:
                    raise UnicodeError("binary data")
            except UnicodeError:
                item = {"relative_path": name, "size": len(raw), "binary": True}
            else:
                prefix = _text_prefix(ctx, text, item, "text", listed=True) if explicit else text
                if prefix is None:
                    return
                item["text"], item["truncated"] = prefix, len(prefix) < len(text)
        except (OSError, ValueError, KeyError):
            summary["package"] = "unavailable"
            item = {"relative_path": name, "size": size, "unavailable": True}
        if add(summary["contents"], item):
            summary["omitted"]["contents"] -= 1
            partial_contents += int(item.get("truncated", False))

    for key, package in packages.items():
        summaries[key]["package"] = "available" if package.available else "unavailable"
    if selected is not None and selected.available:
        for name in requested:
            read_content(selected_key, name, explicit=True)
    for issue in issues:
        if add(data["issues"], issue):
            data["issues_omitted"] -= 1
    for key, package in packages.items():
        summary = summaries[key]
        for name in package.withheld:
            if add(summary["withheld"], name):
                summary["omitted"]["withheld"] -= 1
        for name in sorted(package.files, key=_priority):
            record = package.files[name]
            if add(summary["files"], {
                "relative_path": name, "size": record.get("bytes", record.get("size")),
            }):
                summary["omitted"]["files"] -= 1
    for run in runs:
        item = {field: run[field] for field in (
            "trace_id", "step_count", "tests", "answer_material_accessed", "grader_or_harness_touched", "suspicion",
        )}
        item["outcome"] = outcome_from_reward(run["reward"])
        for field in ("answer_material_accessed", "grader_or_harness_touched"):
            if item[field] is not None:
                item[field] = bool(item[field])
        if add(data["runs"]["list"], item):
            data["runs"]["omitted"] -= 1
    for key in packages:
        for name in contents[key]:
            if key != selected_key or name not in requested:
                read_content(key, name, explicit=False)
        if packages[key].path is not None:
            summaries[key]["contents"].sort(key=lambda item: _priority(item["relative_path"]))
    data["omitted"] = (
        int(data["instruction_truncated"]) + partial_contents + data["issues_omitted"] + data["runs"]["omitted"]
        + sum(sum(summary["omitted"].values()) for summary in summaries.values())
    )
    ctx.budget.omitted = initial_omitted + data["omitted"]
    data["truncated"] = data["omitted"] > 0
    return data


__all__ = ["task"]
