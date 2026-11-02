"""Deterministic receipt checks that never echo arbitrary receipt values."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from evallab.schemas import ContractModel

MAX_BYTES = 64 * 1024 * 1024
_MARKER_VALUE = re.compile(r"(?:test|held[-_]?out|answer[-_]keys?|answers)", re.I)
_SPLIT_VALUE = re.compile(r"(?:test|held[-_]?out)", re.I)
_STEM_MARKER = re.compile(
    r"(?:^|(?<=[-_.]))(?:test|held[-_]?out|answer[-_]keys?|answers)(?=$|[-_.])", re.I,
)
_DATA_EXTENSIONS = {".json", ".jsonl", ".csv", ".tsv", ".parquet"}
_SPLIT_KEYS = {"split", "partition", "subset", "fold", "dataset_split", "eval_split"}
_MARKER_KEYS = {
    "answer_key": "answer_key", "answer_keys": "answer_keys", "answers": "answers",
    "heldout": "heldout", "test_labels": "test",
}
# Access failures are read from harness metadata only, never from agent transcript text.
_METADATA_KEY = re.compile(r"error|stderr|exception|failure|status|refusal|finish_reason")
_STATUS_KEY = re.compile(r"status|code|error")
_CONTENT_KEYS = frozenset({
    "steps", "messages", "content", "transcript", "output", "outputs", "stdout", "diff",
    "reasoning", "trajectory", "observation", "observations", "thinking", "tool_calls",
})
_CONTENT, _METADATA = 1, 2
_ACCESS_PATTERNS = {
    "permission": r"\b(?:permission denied|PermissionError|EACCES|not permitted)\b",
    "refusal": r"\b(?:refused to|I can(?:'|no)t (?:help|assist)|cannot comply)\b",
    "403": r"\b403\b",
    "forbidden": r"\bforbidden\b",
}
_STRONG_ACCESS = {kind: re.compile(pattern, re.I) for kind, pattern in _ACCESS_PATTERNS.items()}
_REFUSAL_FINISH = frozenset({"refusal", "content_filter"})
_LABEL_KEYS = {"label_source", "labels_path", "label_loader", "label_sha256"}
_LABEL_FIELDS = frozenset({
    "is_cheat", "label", "labels", "ground_truth", "positive", "enacted", "verdict_label",
})
_POSITIVE_FLAGS = ("is_cheat", "positive", "enacted", "cheat")
_POSITIVE_TEXT_KEYS = ("label", "verdict_label", "behavior", "behaviour", "ground_truth")
_POSITIVE_TEXT = frozenset({
    "hack", "cheat", "cheating", "positive", "confirmed_cheat", "cheat_confirmed", "exploit",
    "exploited", "true", "yes",
})
_BEHAVIOUR_FIELDS = frozenset((*_POSITIVE_FLAGS, *_POSITIVE_TEXT_KEYS))
_ID_KEYS = ("id", "case_id", "run_id", "rollout_id", "trial_id")
_SCRIPTED_VARIANTS = frozenset({"cheat", "honest"})
_MODEL_KEYS = {"model", "served_model", "requested_model", "route", "requested_route"}
_SIZE_KEYS = {"n", "total", "denominator", "sample_size", "n_cases", "num_cases"}
_SPEND_KEYS = {"cost_usd", "usd", "spend"}
_IGNORED_DIRS = frozenset({".git", ".state", ".venv", "node_modules", "__pycache__"})
_LABEL_SUFFIXES = frozenset({".json", ".jsonl", ".csv", ".tsv"})
_WRITE_ATTRS = frozenset({
    "write_text", "write_bytes", "write", "writelines", "dump", "writerow", "writerows",
    "to_csv", "to_json", "to_parquet",
})
_LOADER = "load_labels"
_PATH_CALLS = frozenset({
    "Path", "PurePath", "PurePosixPath", "str", "join", "joinpath", "with_name", "with_suffix",
    "resolve", "expanduser", "fspath", "format",
})
_MODULE_SCOPE = "<module>"
_PROVENANCE_PATH_LIMIT = 10


class ValidityFinding(ContractModel):
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    finding_id: str = ""
    paths: list[str] = Field(default_factory=list)
    pointers: list[str] = Field(default_factory=list)
    count: int = 0


class ReceiptMetadata(ContractModel):
    path: str
    rows: int = 0
    sample_sizes: dict[str, float] = Field(default_factory=dict)
    spend_usd: float = 0
    scripted_positives: int = 0
    real_positives: int = 0


class MetadataIdentity(ContractModel):
    """A bounded configuration value, never task or transcript text."""

    field: str
    sha256: str
    occurrences: int = 0
    locations: list[str] = Field(default_factory=list)
    value: str | None = None


class LabelProvenance(ContractModel):
    """Where one label file's rows come from; never contains label values."""

    file: str
    present: bool
    rows: int | None = None
    rows_source: Literal["file", "metrics_summary", "unknown"] = "unknown"
    producer: str | None = None
    loader_use: Literal["direct", "helper", "none", "mixed", "aggregate", "unknown"] = "unknown"
    via_shared_loader: bool | None = None


class ValidityReport(ContractModel):
    files_checked: int = 0
    files_skipped: int = 0
    rows_checked: int = 0
    label_rows: int = 0
    rows_without_label_source: int = 0
    marker_counts: dict[str, int] = Field(default_factory=dict)
    label_sources: list[MetadataIdentity] = Field(default_factory=list)
    label_provenance: list[LabelProvenance] = Field(default_factory=list)
    models_routes: list[MetadataIdentity] = Field(default_factory=list)
    access_failures: dict[str, int] = Field(default_factory=dict)
    split_counts: dict[str, int] = Field(default_factory=dict)
    receipts: list[ReceiptMetadata] = Field(default_factory=list)
    scripted_positives: int = 0
    real_positives: int = 0
    spend_usd: float = 0
    findings: list[ValidityFinding] = Field(default_factory=list)


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _walk(
    value: object, pointer: str = "", flags: int = 0,
) -> Iterator[tuple[str, str, object, int]]:
    """Yield (pointer, lowercase key, value, flags) where flags say whether any key on the
    way down (including the value's own) names agent content or harness metadata."""
    if isinstance(value, dict):
        for key, child in sorted(value.items()):
            field = str(key)
            lowered = field.lower()
            inner = flags
            if lowered in _CONTENT_KEYS:
                inner |= _CONTENT
            if _METADATA_KEY.search(lowered):
                inner |= _METADATA
            location = f"{pointer}/{_pointer(field)}"
            yield location, lowered, child, inner
            if isinstance(child, (dict, list)):
                yield from _walk(child, location, inner)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            location = f"{pointer}/{index}"
            if isinstance(child, (dict, list)):
                yield from _walk(child, location, flags)
            else:
                yield location, "", child, flags
    else:
        yield pointer, "", value, flags


def _own_source(row: dict[str, object]) -> bool:
    return any(row.get(key) not in (None, "") for key in _LABEL_KEYS)


def _rows(
    value: object, inherited: bool = False,
) -> Iterator[tuple[dict[str, object], bool]]:
    """Yield (row, has label provenance) for every dict inside a list of the document."""
    if isinstance(value, dict):
        sourced = inherited or _own_source(value)
        for child in value.values():
            if isinstance(child, list):
                for row in child:
                    if isinstance(row, dict):
                        yield row, sourced or _own_source(row)
                        yield from _rows(row, sourced)
            elif isinstance(child, dict):
                yield from _rows(child, sourced)
    elif isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                yield row, inherited or _own_source(row)
                yield from _rows(row, inherited)


def _label_row(row: dict[str, object]) -> bool:
    return any(row.get(key) not in (None, "") for key in _LABEL_FIELDS)


def _positive(row: dict[str, object]) -> bool:
    for key in _POSITIVE_FLAGS:
        value = row.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() in {"true", "1"}):
            return True
        if isinstance(value, int) and not isinstance(value, bool) and value == 1:
            return True
    for key in _POSITIVE_TEXT_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip().lower().replace("-", "_") in _POSITIVE_TEXT:
            return True
    return False


def _scripted(row: dict[str, object]) -> bool | None:
    """True/False when the row is a scripted control / real; None when it has no such marker."""
    kind, variant = row.get("kind"), row.get("variant")
    scripted_id = any(
        isinstance(row.get(key), str) and "scripted" in str(row[key]).lower() for key in _ID_KEYS
    )
    if not isinstance(kind, str) and not isinstance(variant, str) and not scripted_id:
        return None
    if scripted_id:
        return True
    if isinstance(kind, str) and kind.strip().lower() == "scripted":
        return True
    return isinstance(variant, str) and variant.strip().lower() in _SCRIPTED_VARIANTS


def _documents(path: Path) -> Iterator[tuple[str, object, bool]]:
    with path.open(encoding="utf-8", newline="") as handle:
        if path.suffix.lower() == ".jsonl":
            for number, line in enumerate(handle, 1):
                if line.strip():
                    yield f"/line/{number}", json.loads(line), True
        elif path.suffix.lower() in {".csv", ".tsv"}:
            delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
            for number, row in enumerate(csv.DictReader(handle, delimiter=delimiter, strict=True), 2):
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("inconsistent CSV columns")
                yield f"/line/{number}", row, True
        else:
            yield "", json.load(handle), False


def _numeric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int, float)):
        try:
            number = float(value)
        except (ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None
    return None


def _code_path(path: Path) -> bool:
    return any(part.lower() == "tests" for part in path.parts[:-1])


def _path_markers(value: str) -> Iterator[str]:
    path = Path(value)
    if path.suffix.lower() not in _DATA_EXTENSIONS or _code_path(path):
        return
    for component in path.parts[:-1]:
        if _MARKER_VALUE.fullmatch(component):
            yield component.lower().replace("-", "_")
    for match in _STEM_MARKER.finditer(path.stem):
        yield match.group().lower().replace("-", "_")


def _field_markers(key: str, value: object) -> Iterator[str]:
    """A key marks held-out data only when the WHOLE key is a split or answer-key name."""
    if key in _MARKER_KEYS:
        yield _MARKER_KEYS[key]
    elif key in _SPLIT_KEYS and isinstance(value, str) and _SPLIT_VALUE.fullmatch(value):
        yield value.lower().replace("-", "_")


def _access_counts(key: str, value: object, flags: int) -> Iterator[tuple[str, int]]:
    """Strong access-failure patterns in harness metadata fields; never agent content."""
    if flags & _CONTENT or not flags & _METADATA:
        return
    if isinstance(value, str):
        text = value
        if key == "finish_reason" and value.strip().lower() in _REFUSAL_FINISH:
            yield "refusal", 1
    elif isinstance(value, int) and not isinstance(value, bool) and _STATUS_KEY.search(key):
        text = str(value)
    else:
        return
    for kind, pattern in _STRONG_ACCESS.items():
        count = sum(1 for _ in pattern.finditer(text))
        if count:
            yield kind, count


def _add_finding(
    findings: dict[str, ValidityFinding], *, code: str,
    severity: Literal["info", "warning", "error"], message: str, count: int = 1,
    paths: Iterable[str] = (), pointers: Iterable[str] = (),
) -> None:
    finding = findings.get(code)
    if finding is None:
        finding = ValidityFinding(code=code, severity=severity, message=message)
        findings[code] = finding
    finding.count += count
    for target, examples in ((finding.paths, paths), (finding.pointers, pointers)):
        for example in examples:
            if len(target) >= 10:
                break
            if example not in target:
                target.append(example)


# --- label provenance: label files and the code that writes them --------------------------


@dataclass(frozen=True)
class _Writer:
    """A function (or module body) that writes a label file name."""

    module: str
    scope: str
    shown: str
    pattern: re.Pattern[str]
    loader: Literal["direct", "helper", "none", "aggregate"]

    @property
    def producer(self) -> str:
        return f"{self.module}:{self.scope}"


def _label_name(node: ast.expr) -> tuple[str, re.Pattern[str]] | None:
    """The file-name part of a string constant or f-string that names a label file."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else "\0" for part in node.values
        )
    else:
        return None
    base = text.replace("\\", "/").rsplit("/", 1)[-1]
    literal = base.replace("\0", "").lower()
    if "labels" not in literal or Path(literal).suffix not in _LABEL_SUFFIXES:
        return None
    regex = ".+".join(re.escape(piece) for piece in base.split("\0"))
    return base.replace("\0", "{}"), re.compile(regex, re.I)


def _is_write(call: ast.Call) -> bool:
    function = call.func
    name = function.attr if isinstance(function, ast.Attribute) else (
        function.id if isinstance(function, ast.Name) else ""
    )
    if name in _WRITE_ATTRS:
        return True
    if name != "open":
        return False
    mode: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    if isinstance(function, ast.Attribute) and call.args:
        mode = call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    return (
        isinstance(mode, ast.Constant) and isinstance(mode.value, str)
        and any(flag in mode.value for flag in "wax+")
    )


@dataclass
class _Scope:
    names: dict[str, re.Pattern[str]]
    writes: bool
    loader: bool
    calls: set[str]


def _path_expression(expression: ast.AST) -> bool:
    """True when every call inside is path building, so the value is still just a path."""
    for part in ast.walk(expression):
        if isinstance(part, ast.Call):
            function = part.func
            name = function.attr if isinstance(function, ast.Attribute) else (
                function.id if isinstance(function, ast.Name) else ""
            )
            if name not in _PATH_CALLS:
                return False
    return True


def _scope_facts(nodes: list[ast.stmt]) -> _Scope:
    """Label file names a scope mentions, and whether it WRITES one (a write call whose
    receiver or argument is the name or a variable assigned from it), calls the shared
    loader, and which bare names it calls."""
    walked = [item for node in nodes for item in ast.walk(node)]
    inner_constants = {
        id(part) for item in walked if isinstance(item, ast.JoinedStr) for part in item.values
    }
    scope = _Scope(names={}, writes=False, loader=False, calls=set())
    name_nodes: set[int] = set()
    for item in walked:
        if id(item) in inner_constants or not isinstance(item, (ast.Constant, ast.JoinedStr)):
            continue
        found = _label_name(item)
        if found is not None:
            scope.names.setdefault(*found)
            name_nodes.add(id(item))

    def bearing(expression: ast.AST, variables: set[str]) -> bool:
        return any(
            id(part) in name_nodes or (isinstance(part, ast.Name) and part.id in variables)
            for part in ast.walk(expression)
        )

    variables: set[str] = set()
    for item in walked:
        if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            if _path_expression(item.value) and bearing(item.value, variables):
                variables.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
    for item in walked:
        if not isinstance(item, ast.Call):
            continue
        function = item.func
        if isinstance(function, ast.Name):
            scope.calls.add(function.id)
            scope.loader |= function.id == _LOADER
        elif isinstance(function, ast.Attribute):
            scope.loader |= function.attr == _LOADER
            if isinstance(function.value, ast.Name) and function.value.id in {"self", "cls"}:
                scope.calls.add(function.attr)
        if _is_write(item):
            operands: list[ast.AST] = [*item.args, *(kw.value for kw in item.keywords)]
            if isinstance(function, ast.Attribute):
                operands.append(function.value)
            scope.writes |= any(bearing(operand, variables) for operand in operands)
    return scope


def _module_writers(source: bytes, relative: str) -> list[_Writer]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    bodies: dict[str, list[ast.stmt]] = {}
    aliases: dict[str, str | None] = {}
    module_body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, defs):
            bodies[node.name] = [node]
            aliases[node.name] = node.name
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, defs):
                    qualified = f"{node.name}.{child.name}"
                    bodies[qualified] = [child]
                    # A bare method name resolves only when no other callable shares it.
                    aliases[child.name] = None if child.name in aliases else qualified
        else:
            module_body.append(node)
    if module_body:
        bodies[_MODULE_SCOPE] = module_body
    facts = {name: _scope_facts(nodes) for name, nodes in bodies.items()}
    producers = {name for name, fact in facts.items() if fact.names and fact.writes}
    if not producers:
        return []

    def callees(name: str) -> set[str]:
        resolved = {aliases.get(call) for call in facts[name].calls}
        return {callee for callee in resolved if callee is not None and callee != name}

    def uses_loader(name: str) -> Literal["direct", "helper", "none"]:
        if facts[name].loader:
            return "direct"
        seen = {name}
        pending = [name]
        while pending:
            for callee in callees(pending.pop()):
                if callee in seen:
                    continue
                if facts[callee].loader:
                    return "helper"
                seen.add(callee)
                pending.append(callee)
        return "none"

    writers: list[_Writer] = []
    for name in sorted(producers):
        loader: Literal["direct", "helper", "none", "aggregate"] = (
            "aggregate" if producers & callees(name) else uses_loader(name)
        )
        for shown, pattern in facts[name].names.items():
            writers.append(_Writer(relative, name, shown, pattern, loader))
    return writers


def _count_label_rows(path: Path) -> int | None:
    """Row count of a label file; reads structure only and keeps no row contents."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        with path.open(encoding="utf-8", newline="") as handle:
            suffix = path.suffix.lower()
            if suffix == ".jsonl":
                return sum(1 for line in handle if line.strip())
            if suffix in {".csv", ".tsv"}:
                delimiter = "\t" if suffix == ".tsv" else ","
                rows = sum(1 for row in csv.reader(handle, delimiter=delimiter) if row)
                return max(rows - 1, 0)
            document = json.load(handle)
    except (OSError, UnicodeError, ValueError, csv.Error):
        return None
    if isinstance(document, list):
        return len(document)
    if isinstance(document, dict):
        lists = [child for child in document.values() if isinstance(child, list)]
        return len(lists[0]) if len(document) == 1 and lists else len(document)
    return None


def _metrics_totals(paths: list[Path]) -> dict[str, int]:
    """`total_rollouts` per top-level corpus key of metrics_summary files; ambiguous keys
    (different totals in different files) are dropped."""
    seen: dict[str, set[int]] = {}
    for path in paths:
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        for key, value in document.items():
            total = value.get("total_rollouts") if isinstance(value, dict) else None
            if isinstance(total, int) and not isinstance(total, bool):
                seen.setdefault(str(key), set()).add(total)
    return {key: next(iter(totals)) for key, totals in seen.items() if len(totals) == 1}


def _loader_summary(
    writers: list[_Writer],
) -> tuple[Literal["direct", "helper", "none", "mixed", "aggregate", "unknown"], bool | None]:
    if not writers:
        return "unknown", None
    uses = {writer.loader for writer in writers}
    if uses == {"aggregate"}:
        return "aggregate", None
    uses.discard("aggregate")
    if len(uses) > 1:
        shared = {use != "none" for use in uses}
        return "mixed", (next(iter(shared)) if len(shared) == 1 else None)
    use = next(iter(uses))
    return use, use != "none"


def _label_provenance(root: Path, excluded: tuple[Path, ...]) -> list[LabelProvenance]:
    label_files: list[Path] = []
    metrics_files: list[Path] = []
    writers: list[_Writer] = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(
            name for name in dirs
            if name not in _IGNORED_DIRS and not (parent / name).is_symlink()
            and not any((parent / name).resolve().is_relative_to(path) for path in excluded)
        )
        for filename in sorted(files):
            path = parent / filename
            if path.is_symlink() or not path.is_file():
                continue
            lowered, suffix = filename.lower(), path.suffix.lower()
            relative = path.relative_to(root).as_posix()
            if suffix in _LABEL_SUFFIXES and "labels" in lowered:
                label_files.append(path)
            if suffix == ".json" and "metrics_summary" in lowered:
                metrics_files.append(path)
            if suffix == ".py" and path.stat().st_size <= MAX_BYTES:
                try:
                    writers.extend(_module_writers(path.read_bytes(), relative))
                except OSError:
                    continue
    entries: list[LabelProvenance] = []
    used: set[tuple[str, str, str]] = set()
    for path in label_files:
        relative = path.relative_to(root).as_posix()
        matches = [writer for writer in writers if writer.pattern.fullmatch(path.name)]
        top = relative.split("/", 1)[0]
        matches = [w for w in matches if w.module.split("/", 1)[0] == top] or matches
        used.update((w.module, w.scope, w.shown) for w in matches)
        use, shared = _loader_summary(matches)
        producers = sorted({w.producer for w in matches if w.loader != "aggregate"}
                           or {w.producer for w in matches})
        rows = _count_label_rows(path)
        entries.append(LabelProvenance(
            file=relative, present=True, rows=rows,
            rows_source="file" if rows is not None else "unknown",
            producer=", ".join(producers) or None, loader_use=use, via_shared_loader=shared,
        ))
    pending = sorted(
        {(w.module, w.scope, w.shown): w for w in writers
         if (w.module, w.scope, w.shown) not in used}.values(),
        key=lambda w: (w.module, w.scope, w.shown),
    )
    totals = _metrics_totals(metrics_files) if pending else {}
    for writer in pending:
        use, shared = _loader_summary([writer])
        corpus = writer.scope.removeprefix("prepare_") if writer.scope.startswith("prepare_") else ""
        derived = totals.get(corpus) if corpus and use != "aggregate" else None
        entries.append(LabelProvenance(
            file=writer.shown, present=False, rows=derived,
            rows_source="metrics_summary" if derived is not None else "unknown",
            producer=writer.producer, loader_use=use, via_shared_loader=shared,
        ))
    return entries


def _rows_text(entries: list[LabelProvenance]) -> str:
    known = sum(entry.rows or 0 for entry in entries)
    unknown = sum(entry.rows is None for entry in entries)
    if unknown == len(entries):
        return "row count unknown"
    if unknown:
        return f"at least {known} rows, count unknown for {unknown}"
    return f"{known} rows"


def _provenance_finding(
    findings: dict[str, ValidityFinding], entries: list[LabelProvenance],
) -> None:
    if not entries:
        return
    counted = [entry for entry in entries if entry.loader_use != "aggregate"]
    shared = [entry for entry in counted if entry.via_shared_loader is True]
    rules = [entry for entry in counted if entry.via_shared_loader is False]
    unknown = [entry for entry in counted if entry.via_shared_loader is None]
    rule_paths = {entry.producer for entry in rules}
    labelling_paths = len(rule_paths) + bool(shared)
    parts: list[str] = []
    if shared:
        parts.append(
            f"{_rows_text(shared)} in {len(shared)} file(s) via the shared {_LOADER} loader"
        )
    if rules:
        names = ", ".join(sorted(
            (entry.producer or "").rpartition(":")[2] for entry in rules
        )[:6])
        parts.append(
            f"{_rows_text(rules)} in {len(rules)} file(s) via per-corpus rules ({names})"
        )
    if unknown:
        parts.append(
            f"{_rows_text(unknown)} in {len(unknown)} file(s) with no identifiable producing code"
        )
    message = f"Label provenance: {'; '.join(parts) or 'only aggregate label files'}."
    if aggregates := [entry for entry in entries if entry.loader_use == "aggregate"]:
        message += (
            f" {len(aggregates)} pooled file(s) written by code that merges other label files "
            "are not counted again."
        )
    if derived := [entry for entry in entries if entry.rows_source == "metrics_summary"]:
        message += (
            f" {len(derived)} count(s) come from metrics_summary total_rollouts "
            "because the label files are absent."
        )
    absent = [entry for entry in counted if not entry.present and entry.rows is None]
    if absent:
        message += f" {len(absent)} label file(s) are absent, so their counts are unknown."
    _add_finding(
        findings, code="label_provenance",
        severity="warning" if labelling_paths > 1 else "info",
        message=message, count=sum(entry.rows or 0 for entry in counted),
        paths=[entry.file for entry in entries if entry.present][:_PROVENANCE_PATH_LIMIT]
        or [entry.producer.partition(":")[0] for entry in entries if entry.producer],
    )


def check_folder_validity(
    folder: Path, *, exclude: tuple[Path, ...] = (),
) -> ValidityReport:
    """Inspect receipts without echoing task text, code or transcript contents."""
    root = folder.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    report = ValidityReport()
    findings: dict[str, ValidityFinding] = {}
    identities: dict[tuple[str, str], MetadataIdentity] = {}
    markers: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    splits: Counter[str] = Counter()
    excluded = tuple(path.resolve() for path in exclude)
    distinct: Counter[str] = Counter()
    overflow: Counter[str] = Counter()
    unsourced: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in _DATA_EXTENSIONS:
            continue
        if any(path.resolve().is_relative_to(ignored) for ignored in excluded):
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in {".git", ".state"} for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            report.files_skipped += 1
            _add_finding(
                findings, code="path_escape", severity="error",
                message="A linked receipt was not read.", paths=[relative],
            )
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_BYTES:
            report.files_skipped += 1
            _add_finding(
                findings, code="oversize", severity="warning",
                message="A receipt exceeds the size limit.", paths=[relative],
            )
            continue
        receipt = ReceiptMetadata(path=relative)
        code_receipt = _code_path(Path(relative))
        for marker in _path_markers(relative):
            markers[marker] += 1
            _add_finding(
                findings, code="heldout_marker", severity="warning",
                message="Test, answer-key or held-out markers appear in receipt data metadata.",
                paths=[relative],
            )
        if path.suffix.lower() == ".parquet":
            report.files_skipped += 1
            report.receipts.append(receipt)
            _add_finding(
                findings, code="content_not_inspected", severity="info",
                message=(
                    "Parquet receipt paths and size limits were inspected, "
                    "but their contents and rows were not read."
                ),
                paths=[relative],
            )
            continue
        try:
            for prefix, document, is_row in _documents(path):
                row_records = list(_rows(document))
                if is_row and isinstance(document, dict):
                    row_records.insert(0, (document, _own_source(document)))
                receipt.rows += len(row_records)
                for row, sourced in row_records:
                    if _label_row(row):
                        report.label_rows += 1
                        if not sourced:
                            report.rows_without_label_source += 1
                            if relative not in unsourced:
                                unsourced.append(relative)
                    if not any(row.get(key) not in (None, "") for key in _BEHAVIOUR_FIELDS):
                        continue
                    scripted = _scripted(row)
                    if scripted is not None and _positive(row):
                        if scripted:
                            receipt.scripted_positives += 1
                        else:
                            receipt.real_positives += 1
                for pointer, key, value, flags in _walk(document, prefix):
                    location = f"{relative}#{pointer}"
                    is_model = (
                        key in _MODEL_KEYS or key.endswith("_route") or key.startswith("route_")
                    )
                    if (key in _LABEL_KEYS or is_model) and isinstance(value, (str, int, float)):
                        if isinstance(value, bool) or value == "":
                            continue
                        literal = str(value)
                        digest = hashlib.sha256(literal.encode()).hexdigest()
                        identity_key = key, digest
                        if identity_key not in identities:
                            if distinct[key] >= 50:
                                overflow[key] += 1
                            else:
                                distinct[key] += 1
                                identities[identity_key] = MetadataIdentity(
                                    field=key, sha256=digest,
                                    value=literal if len(literal) <= 200 else None,
                                )
                        identity = identities.get(identity_key)
                        if identity is not None:
                            identity.occurrences += 1
                            if len(identity.locations) < 50:
                                identity.locations.append(location)
                    number = _numeric(value)
                    size_key = (
                        key in _SIZE_KEYS or "denominator" in key or key == "denom"
                        or key.startswith(("n_", "total_")) or key.endswith("_n")
                    )
                    if number is not None and size_key:
                        receipt.sample_sizes[pointer] = number
                    if key in _SPEND_KEYS and number is not None:
                        receipt.spend_usd += number
                    if key == "in_sample":
                        if value in (True, "true", "True", 1):
                            splits["in_sample"] += 1
                        elif value in (False, "false", "False", 0):
                            splits["out_of_sample"] += 1
                    if key in {"tune_split", "report_split"}:
                        splits[key.removesuffix("_split")] += 1
                    if not code_receipt:
                        for marker in _field_markers(key, value):
                            markers[marker] += 1
                            _add_finding(
                                findings, code="heldout_marker", severity="warning",
                                message=(
                                    "Test, answer-key or held-out markers appear "
                                    "in receipt data metadata."
                                ),
                                paths=[relative], pointers=[pointer],
                            )
                    for kind, count in _access_counts(key, value, flags):
                        failures[kind] += count
                        _add_finding(
                            findings, code="access_failure", severity="warning", count=count,
                            message=(
                                "Permission, refusal or forbidden-response markers "
                                "appear in receipts."
                            ),
                            paths=[relative], pointers=[pointer],
                        )
                    if isinstance(value, str):
                        lower = value.lower()
                        if key in {"split", "tune_split", "report_split", "partition"}:
                            for name in ("test", "train", "dev", "tune", "report", "heldout"):
                                normalized = lower.replace("_", "").replace("-", "")
                                if re.search(rf"(?<![a-z]){name}(?![a-z])", normalized):
                                    splits[name] += 1
            report.files_checked += 1
        except (OSError, UnicodeError, ValueError, csv.Error):
            report.files_skipped += 1
            _add_finding(
                findings, code="unreadable", severity="warning",
                message="A receipt could not be read completely; its counts may be partial.",
                paths=[relative],
            )
        report.receipts.append(receipt)
    report.rows_checked = sum(receipt.rows for receipt in report.receipts)
    report.spend_usd = sum(receipt.spend_usd for receipt in report.receipts)
    report.scripted_positives = sum(receipt.scripted_positives for receipt in report.receipts)
    report.real_positives = sum(receipt.real_positives for receipt in report.receipts)
    report.marker_counts = dict(sorted(markers.items()))
    report.access_failures = {key: value for key, value in sorted(failures.items()) if value}
    if access_finding := findings.get("access_failure"):
        counts = ", ".join(f"{kind}={count}" for kind, count in report.access_failures.items())
        access_finding.message = f"Access-failure markers appear in receipts: {counts}."
    report.split_counts = dict(sorted(splits.items()))
    report.label_sources = [value for (key, _), value in sorted(identities.items())
                            if key in _LABEL_KEYS]
    report.models_routes = [value for (key, _), value in sorted(identities.items())
                            if key not in _LABEL_KEYS]
    loaders = [value for value in report.label_sources if value.field == "label_loader"]
    if len(loaders) > 1:
        _add_finding(
            findings, code="multiple_label_loaders", severity="error", count=len(loaders),
            message="More than one label loader is recorded; check that labels are comparable.",
            pointers=(location for loader in loaders for location in loader.locations),
        )
    if report.rows_without_label_source:
        _add_finding(
            findings, code="missing_label_source", severity="warning",
            message=(
                f"{report.rows_without_label_source} of {report.label_rows} label rows "
                "carry no label_source, label_loader, labels_path or label_sha256."
            ),
            count=report.rows_without_label_source,
            paths=unsourced,
        )
    report.label_provenance = _label_provenance(root, excluded)
    _provenance_finding(findings, report.label_provenance)
    if report.scripted_positives:
        only_scripted = [
            receipt for receipt in report.receipts
            if receipt.scripted_positives and not receipt.real_positives
        ]
        shares = ", ".join(
            f"{receipt.path}: {receipt.scripted_positives} of "
            f"{receipt.scripted_positives + receipt.real_positives}"
            for receipt in report.receipts if receipt.scripted_positives
        )
        total = report.scripted_positives + report.real_positives
        _add_finding(
            findings, code="scripted_positives",
            severity="warning" if only_scripted else "info",
            message=(
                f"{report.scripted_positives} of {total} positives are scripted controls "
                f"(kind scripted, variant cheat/honest, or 'scripted' in the id); by receipt "
                f"file: {shares}."
            ),
            count=report.scripted_positives,
            paths=[receipt.path for receipt in report.receipts if receipt.scripted_positives],
        )
    if splits.get("in_sample") or splits.get("tune"):
        _add_finding(
            findings, code="in_sample", severity="warning",
            message="Some receipts describe in-sample results or a tuning split.",
            count=splits["in_sample"] + splits["tune"],
        )
    if receipt_paths := [r.path for r in report.receipts if r.sample_sizes]:
        _add_finding(
            findings, code="sample_sizes", severity="info",
            message="Receipt sample sizes were inventoried.",
            paths=receipt_paths, count=len(receipt_paths),
        )
    for key, count in sorted(overflow.items()):
        _add_finding(
            findings, code="metadata_limit", severity="warning",
            message="A metadata field has more distinct values than the display limit.",
            pointers=[f"/{_pointer(key)}"], count=count,
        )
    _add_finding(
        findings, code="scan_summary", severity="info",
        message=f"Checked {report.files_checked} receipt files and {report.rows_checked} rows.",
        count=report.files_checked,
    )
    report.findings = list(findings.values())
    for index, finding in enumerate(report.findings, 1):
        finding.finding_id = f"V-{index:02d}"
    return report
