"""Deterministic receipt checks that never echo arbitrary receipt values."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal

from pydantic import Field

from evallab.schemas import ContractModel

MAX_BYTES = 64 * 1024 * 1024
_MARKER_VALUE = re.compile(r"(?:test|held[-_]?out|answer[-_]keys?|answers)", re.I)
_STEM_MARKER = re.compile(
    r"(?:^|(?<=[-_.]))(?:test|held[-_]?out|answer[-_]keys?|answers)(?=$|[-_.])", re.I,
)
_DATA_EXTENSIONS = {".json", ".jsonl", ".csv", ".tsv", ".parquet"}
_SPLIT_KEYS = {"split", "partition", "subset", "fold", "dataset_split", "eval_split"}
_MARKER_KEYS = {
    "answer_key": "answer_key", "answer_keys": "answer_keys", "answers": "answers",
    "heldout": "heldout", "test_labels": "test",
}
_ERROR_KEY = re.compile(r"error|stderr|exception|failure|refusal|status")
_ACCESS_PATTERNS = {
    "permission": r"\b(?:permission denied|PermissionError|EACCES|not permitted)\b",
    "refusal": r"\b(?:refused to|I can(?:'|no)t (?:help|assist)|cannot comply)\b",
    "403": r"\b403\b",
    "forbidden": r"\bforbidden\b",
}
_STRONG_ACCESS = {kind: re.compile(pattern, re.I) for kind, pattern in _ACCESS_PATTERNS.items()}
_ERROR_ACCESS = {
    kind: re.compile(
        pattern + (
            r"|\bpermission\b" if kind == "permission"
            else r"|\b(?:refusal|refused)\b" if kind == "refusal" else ""
        ), re.I,
    )
    for kind, pattern in _ACCESS_PATTERNS.items()
}
_LABEL_KEYS = {"label_source", "labels_path", "label_loader", "label_sha256"}
_MODEL_KEYS = {"model", "served_model", "requested_model", "route", "requested_route"}
_SIZE_KEYS = {"n", "total", "denominator", "sample_size", "n_cases", "num_cases"}
_SPEND_KEYS = {"cost_usd", "usd", "spend"}


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


class MetadataIdentity(ContractModel):
    """A bounded configuration value, never task or transcript text."""

    field: str
    sha256: str
    occurrences: int = 0
    locations: list[str] = Field(default_factory=list)
    value: str | None = None


class ValidityReport(ContractModel):
    files_checked: int = 0
    files_skipped: int = 0
    rows_checked: int = 0
    rows_without_label_source: int = 0
    marker_counts: dict[str, int] = Field(default_factory=dict)
    label_sources: list[MetadataIdentity] = Field(default_factory=list)
    models_routes: list[MetadataIdentity] = Field(default_factory=list)
    access_failures: dict[str, int] = Field(default_factory=dict)
    split_counts: dict[str, int] = Field(default_factory=dict)
    receipts: list[ReceiptMetadata] = Field(default_factory=list)
    spend_usd: float = 0
    findings: list[ValidityFinding] = Field(default_factory=list)


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _walk(value: object, pointer: str = "") -> Iterator[tuple[str, str, object]]:
    if isinstance(value, dict):
        for key, child in sorted(value.items()):
            field = str(key)
            location = f"{pointer}/{_pointer(field)}"
            yield location, field.lower(), child
            if isinstance(child, (dict, list)):
                yield from _walk(child, location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            location = f"{pointer}/{index}"
            if isinstance(child, (dict, list)):
                yield from _walk(child, location)
            else:
                yield location, "", child
    else:
        yield pointer, "", value


def _rows(value: object, inherited: bool = False) -> Iterator[bool]:
    if isinstance(value, dict):
        sourced = inherited or any(value.get(key) not in (None, "") for key in _LABEL_KEYS)
        for child in value.values():
            if isinstance(child, list):
                for row in child:
                    if isinstance(row, dict):
                        own = any(row.get(key) not in (None, "") for key in _LABEL_KEYS)
                        yield sourced or own
                        yield from _rows(row, sourced)
            elif isinstance(child, dict):
                yield from _rows(child, sourced)
    elif isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                sourced = any(row.get(key) not in (None, "") for key in _LABEL_KEYS)
                yield inherited or sourced
                yield from _rows(row, inherited)


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
    if key in _MARKER_KEYS:
        yield _MARKER_KEYS[key]
    if not isinstance(value, str):
        return
    if key in _SPLIT_KEYS and _MARKER_VALUE.fullmatch(value):
        yield value.lower().replace("-", "_")
    elif "/" in value and not any(character.isspace() for character in value):
        yield from _path_markers(value)


def _access_counts(key: str, value: object) -> Iterator[tuple[str, int]]:
    patterns = _ERROR_ACCESS if _ERROR_KEY.search(key) else _STRONG_ACCESS
    if isinstance(value, str):
        text = value
    elif _ERROR_KEY.search(key) and isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        return
    for kind, pattern in patterns.items():
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
                row_sources = list(_rows(document))
                if is_row and isinstance(document, dict):
                    row_sources.insert(0, any(
                        document.get(key) not in (None, "") for key in _LABEL_KEYS
                    ))
                receipt.rows += len(row_sources)
                report.rows_without_label_source += sum(not sourced for sourced in row_sources)
                for pointer, key, value in _walk(document, prefix):
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
                    for kind, count in _access_counts(key, value):
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
            message="Some rows do not identify their label source.",
            count=report.rows_without_label_source,
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
