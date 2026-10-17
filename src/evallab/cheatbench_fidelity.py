"""Consumer-fidelity and blinding audit for external CheatBench traces.

The CheatBench converter in :mod:`evallab.cheatbench` intentionally produces
lossy detector ATIF and a separate viewer annotation.  This module answers a
different question: when a downstream consumer (a QA-trace investigator, a
detector, or a critic-training exporter) reads a derived artifact, is the
externally labelled evidence still present, and is the answer itself absent?

The audit is deterministic and model-free.  It never turns missing evidence
into a pass: latent localizations, missing artifacts and unresolved snippets
stay ``unknown``/``not_run``.  Snippet text itself is evaluator-only; audit
rows carry hashes and coordinates, not the snippet.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "evallab.trace_consumer_audit/v1"
ANNOTATION_MARKER = "[CheatBench annotation"
HELDOUT_BENCHMARKS = frozenset({"swe-bench-verified", "swe-rebench", "cybench"})
MEASURABLE_LOC_KINDS = frozenset({"localized_snippet", "line_span"})
POSITIVE_LABELS = frozenset({"cheating", "attempt"})
SNIPPET_SEARCH_CHARS = 200

_GENERIC_EVALUATOR_KEYS = frozenset(
    {
        "annotator_id",
        "cheating_mechanism",
        "cheating_type",
        "label_source",
        "rationale",
    }
)


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return int(as_float) if as_float.is_integer() else None


def _counter_dict(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): int(counter[key]) for key in sorted(counter, key=lambda k: str(k))}


def family_split(benchmark: str | None) -> str:
    """Existing CheatBench/Kev family split: three benchmarks stay held out."""
    return "heldout" if benchmark in HELDOUT_BENCHMARKS else "development"


@dataclass(frozen=True)
class AnchorSource:
    """Evaluator-only localization for one CheatBench source trace."""

    trace_id: str
    label: str | None
    benchmark: str | None
    split_raw: str | None
    loc_kind: str | None
    field_path: str | None
    char_start: int | None
    char_end: int | None
    line_start: int | None
    line_end: int | None
    snippet: str = ""

    @property
    def snippet_sha256(self) -> str:
        return _sha256_text(self.snippet)

    @property
    def search_snippet(self) -> str:
        return (self.snippet or "")[:SNIPPET_SEARCH_CHARS]

    @property
    def measurable(self) -> bool:
        return (
            self.loc_kind in MEASURABLE_LOC_KINDS
            and len(self.search_snippet.strip()) >= 4
        )

    @property
    def family_split(self) -> str:  # noqa: A003 - mirrors the module helper name
        return family_split(self.benchmark)


@dataclass(frozen=True)
class AnchorMatch:
    status: str
    variant: str | None = None
    offset: int | None = None
    search_sha256: str | None = None
    reason: str | None = None


@dataclass
class KevAuditResult:
    per_trace: dict[str, dict[str, Any]] = field(default_factory=dict)
    seen_trace_ids: set[str] = field(default_factory=set)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)


def anchor_from_parquet_row(row: Mapping[str, Any]) -> AnchorSource:
    snippet = row.get("loc_snippet") or ""
    if not isinstance(snippet, str):
        snippet = str(snippet)
    return AnchorSource(
        trace_id=str(row.get("trace_id")),
        label=row.get("label"),
        benchmark=row.get("benchmark"),
        split_raw=row.get("split"),
        loc_kind=row.get("loc_kind") or None,
        field_path=row.get("loc_field_path") or None,
        char_start=_opt_int(row.get("loc_char_start")),
        char_end=_opt_int(row.get("loc_char_end")),
        line_start=_opt_int(row.get("loc_line_start")),
        line_end=_opt_int(row.get("loc_line_end")),
        snippet=snippet,
    )


def load_cb_subset(path: str | Path) -> list[dict[str, Any]]:
    """Load committed ``cb_subset.json`` rows (metadata only, no trace bodies)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"cb_subset rows must be a list: {path}")
    return [dict(row) for row in rows]


def load_parquet_anchors(
    parquet_path: str | Path,
    *,
    wanted_ids: Iterable[str] | None = None,
) -> dict[str, AnchorSource]:
    """Load evaluator-only anchors from the pinned Parquet metadata columns.

    The large ``trace`` column is never read.  With ``wanted_ids=None`` only
    cheating/attempt rows are returned; otherwise exactly the wanted rows
    that exist in the Parquet are returned.
    """
    import pyarrow.parquet as pq

    wanted = None if wanted_ids is None else {str(item) for item in wanted_ids}
    parquet_file = pq.ParquetFile(Path(parquet_path))
    available = set(parquet_file.schema.names)
    columns = [
        name
        for name in (
            "trace_id",
            "label",
            "benchmark",
            "split",
            "loc_kind",
            "loc_field_path",
            "loc_char_start",
            "loc_char_end",
            "loc_line_start",
            "loc_line_end",
            "loc_snippet",
        )
        if name in available
    ]
    anchors: dict[str, AnchorSource] = {}
    for batch in parquet_file.iter_batches(batch_size=1024, columns=columns):
        for row in batch.to_pylist():
            trace_id = str(row.get("trace_id"))
            if wanted is not None and trace_id not in wanted:
                continue
            if wanted is None and row.get("label") not in POSITIVE_LABELS:
                continue
            anchors[trace_id] = anchor_from_parquet_row(row)
        if wanted is not None and len(anchors) >= len(wanted):
            break
    return anchors

def _resolve_source_content(trace: Mapping[str, Any], field_path: str | None) -> str | None:
    from evallab.cheatbench import resolve_field_path

    try:
        content = resolve_field_path(trace, field_path or "")
    except LookupError:
        content = None
    if isinstance(content, str):
        return content
    path = field_path or ""
    if path.startswith("events[1]"):
        try:
            fallback = resolve_field_path(trace, "events[0]" + path[len("events[1]") :])
        except LookupError:
            fallback = None
        if isinstance(fallback, str):
            return fallback
    return None


def source_anchor_match(trace: Mapping[str, Any], anchor: AnchorSource) -> AnchorMatch:
    """Verify that an anchor snippet is verbatim in its source trace field."""
    search_sha = _sha256_text(anchor.search_snippet) if anchor.search_snippet else None
    if anchor.loc_kind not in MEASURABLE_LOC_KINDS:
        return AnchorMatch(
            "unknown",
            search_sha256=search_sha,
            reason=f"loc_kind_not_measurable:{anchor.loc_kind or 'missing'}",
        )
    if len(anchor.search_snippet.strip()) < 4:
        return AnchorMatch(
            "unknown", search_sha256=search_sha, reason="snippet_missing_or_short"
        )
    content = _resolve_source_content(trace, anchor.field_path)
    if content is None:
        return AnchorMatch(
            "unknown", search_sha256=search_sha, reason="source_field_not_string"
        )
    match = find_anchor(content, anchor.search_snippet, loc_kind=anchor.loc_kind)
    if match.status == "lost":
        return AnchorMatch(
            "unknown",
            search_sha256=match.search_sha256,
            reason="snippet_not_in_source",
        )
    return match


def load_source_anchor_matches(
    parquet_path: str | Path,
    anchors: Mapping[str, AnchorSource],
) -> dict[str, AnchorMatch]:
    """Source-verify measurable anchors against the Parquet ``trace`` column."""
    import pyarrow.parquet as pq

    from evallab.cheatbench import parse_raw_trace

    matches: dict[str, AnchorMatch] = {}
    measurable: dict[str, AnchorSource] = {}
    for trace_id, anchor in anchors.items():
        if anchor.measurable:
            measurable[trace_id] = anchor
            matches[trace_id] = AnchorMatch(
                "unknown",
                search_sha256=_sha256_text(anchor.search_snippet),
                reason="source_row_missing",
            )
        else:
            matches[trace_id] = find_anchor(
                None, anchor.search_snippet, loc_kind=anchor.loc_kind
            )
    if not measurable:
        return matches

    parquet_file = pq.ParquetFile(Path(parquet_path))
    seen: set[str] = set()
    for batch in parquet_file.iter_batches(batch_size=64, columns=["trace_id", "trace"]):
        for row in batch.to_pylist():
            trace_id = str(row.get("trace_id"))
            anchor = measurable.get(trace_id)
            if anchor is None:
                continue
            seen.add(trace_id)
            try:
                trace = parse_raw_trace(row.get("trace") or "")
            except Exception as exc:  # source bytes are evaluator evidence
                matches[trace_id] = AnchorMatch(
                    "unknown",
                    search_sha256=_sha256_text(anchor.search_snippet),
                    reason=f"source_unresolved:{type(exc).__name__}",
                )
                continue
            matches[trace_id] = source_anchor_match(trace, anchor)
        if len(seen) >= len(measurable):
            break
    return matches


def _unescape_literals(text: str) -> str:
    out = text.replace("\\r\\n", "\n")
    out = out.replace("\\n", "\n").replace("\\r", "\n").replace("\\t", "\t")
    out = out.replace('\\"', '"').replace("\\'", "'")
    return out.replace("\\\\", "\\")


def _escape_newlines(text: str) -> str:
    out = text.replace("\\", "\\\\")
    out = out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")
    return out.replace("\t", "\\t")


def snippet_variants(
    snippet: str | None, *, limit: int = SNIPPET_SEARCH_CHARS
) -> list[tuple[str, str]]:
    """Exact-substring variants for known snippet/consumer normalizations."""
    base = (snippet or "")[:limit]
    if not base.strip():
        return []
    unescaped = _unescape_literals(base)
    candidates = [
        ("exact", base),
        ("unescape_literals", unescaped),
        ("pipe_newlines", base.replace("|", "\n")),
        ("unescape_and_pipe", unescaped.replace("|", "\n")),
        ("escape_newlines", _escape_newlines(base)),
        ("json_escape_quotes", base.replace('"', '\\"')),
    ]
    seen: set[str] = set()
    variants: list[tuple[str, str]] = []
    for name, text in candidates:
        if text and text not in seen:
            seen.add(text)
            variants.append((name, text))
    return variants


def find_anchor(
    text: str | None,
    snippet: str | None,
    *,
    loc_kind: str | None,
) -> AnchorMatch:
    """Search one consumer text for an evaluator-only anchor snippet."""
    search = (snippet or "")[:SNIPPET_SEARCH_CHARS]
    search_sha = _sha256_text(search) if search else None
    if loc_kind not in MEASURABLE_LOC_KINDS:
        return AnchorMatch(
            "unknown",
            search_sha256=search_sha,
            reason=f"loc_kind_not_measurable:{loc_kind or 'missing'}",
        )
    if len(search.strip()) < 4:
        return AnchorMatch(
            "unknown", search_sha256=search_sha, reason="snippet_missing_or_short"
        )
    if text is None:
        return AnchorMatch(
            "unknown", search_sha256=search_sha, reason="consumer_text_missing"
        )
    for name, variant in snippet_variants(search):
        offset = text.find(variant)
        if offset >= 0:
            return AnchorMatch(
                "preserved", variant=name, offset=offset, search_sha256=search_sha
            )
    return AnchorMatch("lost", search_sha256=search_sha, reason="snippet_not_found")


def _collect_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _collect_strings(item)
    elif isinstance(node, dict):
        for key in sorted(node, key=lambda item: str(item)):
            if key == "extra":
                continue
            yield from _collect_strings(node[key])


def flatten_atif_text(doc: Mapping[str, Any]) -> str:
    """Evidence text a structured ATIF consumer can read, excluding metadata."""
    parts: list[str] = []
    for step in doc.get("steps") or []:
        if isinstance(step, dict):
            parts.extend(_collect_strings(step))
    return "\n".join(parts)


def find_forbidden_json(obj: Any) -> list[dict[str, str]]:
    """Find evaluator-only fields/text that must not reach an actor input."""
    hits: list[dict[str, str]] = []

    def walk(node: Any, path: str, parent_key: str | None) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key)
                lowered = key_text.lower()
                child = f"{path}.{key_text}"
                forbidden = (
                    lowered.startswith("cheatbench_")
                    or lowered.startswith("loc_")
                    or (lowered == "cheatbench" and parent_key == "extra")
                    or (
                        lowered in _GENERIC_EVALUATOR_KEYS
                        and path in {"$", "$.metadata"}
                    )
                )
                if forbidden:
                    hits.append(
                        {"path": child, "kind": "forbidden_key", "detail": key_text}
                    )
                walk(value, child, key_text)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", parent_key)
        elif isinstance(node, str) and ANNOTATION_MARKER in node:
            hits.append(
                {"path": path, "kind": "annotation_text", "detail": ANNOTATION_MARKER}
            )

    walk(obj, "$", None)
    return hits


def _is_forbidden_top_key(key: Any) -> bool:
    lowered = str(key).lower()
    return (
        lowered.startswith("cheatbench_")
        or lowered.startswith("loc_")
        or lowered in _GENERIC_EVALUATOR_KEYS
    )


def _kept_steps(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Actor-visible steps of ``doc``: every step except CheatBench annotation steps."""
    kept: list[Mapping[str, Any]] = []
    for step in doc.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        message = step.get("message")
        if isinstance(message, str) and message.lstrip().startswith(ANNOTATION_MARKER):
            continue
        kept.append(step)
    return kept


def blind_atif_step_origins(doc: Mapping[str, Any]) -> list[Any]:
    """Original ``step_id`` of each :func:`blind_atif` step, indexed by its new id."""
    return [step.get("step_id") for step in _kept_steps(doc)]


def blind_atif(doc: Mapping[str, Any], *, item_id: str) -> dict[str, Any]:
    """Return an actor-visible ATIF copy with evaluator fields removed.

    Evidence text, tool calls and observations are preserved byte-for-byte at
    the string level.  Top-level metadata is replaced, annotation steps are
    dropped, per-step ``extra`` span side-channels and a root ``extra.cheatbench``
    block are removed, and steps are renumbered from zero.  Mutating the original
    label/mechanism/rationale metadata therefore cannot change the projected
    document.
    """
    steps: list[dict[str, Any]] = []
    for step in _kept_steps(doc):
        projected = copy.deepcopy(dict(step))
        projected.pop("extra", None)
        steps.append(projected)
    for index, step in enumerate(steps):
        step["step_id"] = index
    out: dict[str, Any] = {
        str(key): copy.deepcopy(value)
        for key, value in doc.items()
        if key not in {"metadata", "steps"} and not _is_forbidden_top_key(key)
    }
    extra = out.get("extra")
    if isinstance(extra, dict):
        extra.pop("cheatbench", None)
    out["steps"] = steps
    out["metadata"] = {"blind_projection": SCHEMA, "item_id": item_id}
    return out


def _anchor_payload(
    *,
    anchor: AnchorSource | None,
    match: AnchorMatch,
    loc: Mapping[str, Any] | None = None,
    source_match: AnchorMatch | None = None,
) -> dict[str, Any]:
    loc = loc or {}
    return {
        "loc_kind": anchor.loc_kind if anchor else loc.get("kind"),
        "field_path": anchor.field_path if anchor else loc.get("field_path"),
        "char_start": anchor.char_start if anchor else _opt_int(loc.get("char_start")),
        "char_end": anchor.char_end if anchor else _opt_int(loc.get("char_end")),
        "line_start": anchor.line_start if anchor else _opt_int(loc.get("line_start")),
        "line_end": anchor.line_end if anchor else _opt_int(loc.get("line_end")),
        "snippet_sha256": anchor.snippet_sha256 if anchor else None,
        "status": match.status,
        "variant": match.variant,
        "offset": match.offset,
        "search_sha256": match.search_sha256,
        "reason": match.reason,
        "source_status": source_match.status if source_match else None,
        "source_variant": source_match.variant if source_match else None,
        "source_offset": source_match.offset if source_match else None,
        "source_reason": source_match.reason if source_match else None,
    }


def audit_atif_document(
    doc: Mapping[str, Any],
    *,
    anchor: AnchorSource | None,
    consumer: str,
    artifact_ref: str | None = None,
    source_row: Mapping[str, Any] | None = None,
    source_match: AnchorMatch | None = None,
) -> dict[str, Any]:
    """Audit one already-loaded ATIF document against an optional anchor."""
    source_row = source_row or {}
    label = anchor.label if anchor else source_row.get("label")
    benchmark = anchor.benchmark if anchor else source_row.get("benchmark")
    trace_id = anchor.trace_id if anchor else source_row.get("trace_id") or source_row.get("id")
    text = flatten_atif_text(doc)
    hits = find_forbidden_json(doc)
    failure_reasons: list[str] = []
    if hits:
        failure_reasons.append("blinding_forbidden_fields")

    anchors: list[dict[str, Any]] = []
    if label in POSITIVE_LABELS:
        if anchor is None:
            match = AnchorMatch("unknown", reason="anchor_missing")
            anchors.append(
                _anchor_payload(
                    anchor=None,
                    match=match,
                    loc=source_row.get("loc") or {},
                    source_match=source_match,
                )
            )
            failure_reasons.append("anchor_unknown")
        else:
            if source_match is not None and source_match.status != "preserved":
                match = AnchorMatch(
                    "unknown",
                    search_sha256=_sha256_text(anchor.search_snippet),
                    reason=source_match.reason or "snippet_not_in_source",
                )
            else:
                match = find_anchor(text, anchor.search_snippet, loc_kind=anchor.loc_kind)
            anchors.append(
                _anchor_payload(anchor=anchor, match=match, source_match=source_match)
            )
            if match.status == "lost":
                failure_reasons.append("anchor_lost")
            elif match.status == "unknown":
                failure_reasons.append("anchor_unknown")
    elif anchor is not None:
        match = AnchorMatch("not_applicable", reason="non_positive_label")
        anchors.append(
            _anchor_payload(anchor=anchor, match=match, source_match=source_match)
        )

    steps = [step for step in doc.get("steps") or [] if isinstance(step, Mapping)]
    return {
        "schema": SCHEMA,
        "consumer": consumer,
        "artifact_ref": artifact_ref,
        "source": {
            "trace_id": trace_id,
            "label": label,
            "benchmark": benchmark,
            "family_split": family_split(benchmark),
        },
        "coverage": {
            "artifact_present": True,
            "steps": len(steps),
            "has_tool_calls": any(step.get("tool_calls") for step in steps),
            "flattened_text_chars": len(text),
        },
        "blinding": {"status": "fail" if hits else "pass", "forbidden_hits": hits},
        "anchors": anchors,
        "measurement": "measured",
        "failure_reasons": failure_reasons,
    }


def _base_audit_row(
    *,
    consumer: str,
    source_row: Mapping[str, Any],
    anchor: AnchorSource | None,
    artifact_ref: str | None,
    measurement: str,
    reason: str,
    source_match: AnchorMatch | None = None,
) -> dict[str, Any]:
    label = source_row.get("label")
    benchmark = source_row.get("benchmark")
    if label in POSITIVE_LABELS:
        match = AnchorMatch("unknown", reason=reason)
        anchors = [
            _anchor_payload(
                anchor=anchor,
                match=match,
                loc=source_row.get("loc") or {},
                source_match=source_match,
            )
        ]
    elif anchor is not None:
        anchors = [
            _anchor_payload(
                anchor=anchor,
                match=AnchorMatch("not_applicable", reason="non_positive_label"),
                source_match=source_match,
            )
        ]
    else:
        anchors = []
    return {
        "schema": SCHEMA,
        "consumer": consumer,
        "artifact_ref": artifact_ref,
        "source": {
            "trace_id": source_row.get("trace_id") or source_row.get("id"),
            "label": label,
            "benchmark": benchmark,
            "family_split": family_split(benchmark),
        },
        "coverage": {"artifact_present": False, "exclusion_reason": reason},
        "blinding": {"status": "unknown", "forbidden_hits": []},
        "anchors": anchors,
        "measurement": measurement,
        "failure_reasons": [reason],
    }


def audit_materialized_atif(
    cb_root: str | Path,
    subset_rows: Iterable[Mapping[str, Any]],
    anchors: Mapping[str, AnchorSource],
    *,
    consumer: str = "materialized_atif",
    source_matches: Mapping[str, AnchorMatch] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one audit row per subset row over materialized trial dirs."""
    root = Path(cb_root)
    trial_root = root / "cb" if (root / "cb").is_dir() else root
    exclusions: dict[str, str] = {}
    exclusions_path = root / "exclusions.jsonl"
    if exclusions_path.is_file():
        for line in exclusions_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("trace_id"):
                exclusions[str(entry["trace_id"])] = str(entry.get("reason") or "excluded")

    source_matches = source_matches or {}
    for source_row in subset_rows:
        trace_id = str(source_row.get("trace_id") or source_row.get("id") or "")
        label = source_row.get("label")
        anchor = anchors.get(trace_id) if label in POSITIVE_LABELS else None
        source_match = source_matches.get(trace_id)
        artifact_ref = f"cb/{trace_id}/agent/trajectory.json"
        artifact = trial_root / trace_id / "agent" / "trajectory.json"
        if not artifact.is_file():
            yield _base_audit_row(
                consumer=consumer,
                source_row=source_row,
                anchor=anchor,
                artifact_ref=artifact_ref,
                measurement="not_run",
                reason=exclusions.get(trace_id, "artifact_missing"),
                source_match=source_match,
            )
            continue
        try:
            doc = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            row = _base_audit_row(
                consumer=consumer,
                source_row=source_row,
                anchor=anchor,
                artifact_ref=artifact_ref,
                measurement="ran_without_usable_result",
                reason="invalid_json",
                source_match=source_match,
            )
            row["coverage"]["artifact_present"] = True
            yield row
            continue

        row = audit_atif_document(
            doc,
            anchor=anchor,
            consumer=consumer,
            artifact_ref=artifact_ref,
            source_row=source_row,
            source_match=source_match,
        )
        subset_loc = source_row.get("loc") or {}
        if anchor is not None and row["anchors"]:
            subset_kind = subset_loc.get("kind")
            subset_field = subset_loc.get("field_path")
            mismatch = (
                subset_kind and anchor.loc_kind and subset_kind != anchor.loc_kind
            ) or (
                subset_field and anchor.field_path and subset_field != anchor.field_path
            )
            if mismatch:
                payload = row["anchors"][0]
                payload.update(
                    {
                        "status": "unknown",
                        "variant": None,
                        "offset": None,
                        "reason": "subset_parquet_loc_mismatch",
                    }
                )
                row["failure_reasons"] = [
                    reason
                    for reason in row["failure_reasons"]
                    if reason not in {"anchor_lost", "anchor_unknown"}
                ] + ["subset_parquet_loc_mismatch"]
        yield row


def _chunk_label_kind(value: Any) -> str:
    if value is True or value == "True" or value == 1:
        return "positive"
    if value is False or value == "False" or value == 0:
        return "negative"
    return "unknown"


def audit_kev_chunks(
    kev_root: str | Path,
    anchors: Mapping[str, AnchorSource],
    *,
    wanted_ids: Iterable[str],
    split_files: Iterable[str] = ("train", "dev", "test", "external"),
    source_matches: Mapping[str, AnchorMatch] | None = None,
) -> KevAuditResult:
    """Stream Kev JSONL windows and audit anchor survival in ``state`` text."""
    root = Path(kev_root)
    wanted = {str(item) for item in wanted_ids}
    result = KevAuditResult()
    source_matches = source_matches or {}
    for trace_id, anchor in anchors.items():
        source_match = source_matches.get(trace_id)
        source_ok = source_match is None or source_match.status == "preserved"
        initial_anchor = (
            find_anchor("", anchor.search_snippet, loc_kind=anchor.loc_kind)
            if source_ok
            else AnchorMatch(
                "unknown",
                search_sha256=_sha256_text(anchor.search_snippet),
                reason=source_match.reason if source_match else "snippet_not_in_source",
            )
        )
        result.per_trace[trace_id] = {
            "trace_id": trace_id,
            "benchmark": anchor.benchmark,
            "label": anchor.label,
            "family_split": anchor.family_split,
            "loc_kind": anchor.loc_kind,
            "source_status": source_match.status if source_match else None,
            "source_variant": source_match.variant if source_match else None,
            "source_offset": source_match.offset if source_match else None,
            "source_reason": source_match.reason if source_match else None,
            "n_chunks": 0,
            "splits": [],
            "chunk_label_counts": {"positive": 0, "negative": 0, "unknown": 0},
            "found_in_positive_chunk": False,
            "blinding": {"status": "unknown", "annotation_chunks": 0},
            "anchor": asdict(initial_anchor),
            "_splits": set(),
            "_searched": False,
            "_found": False,
            "_variant": None,
            "_offset": None,
            "_source_ok": source_ok,
        }

    for split_name in split_files:
        path = root / f"{split_name}.jsonl"
        file_info: dict[str, Any] = {
            "path": str(path),
            "present": path.is_file(),
            "rows": 0,
            "wanted_rows": 0,
            "invalid_rows": 0,
        }
        result.files[str(split_name)] = file_info
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                file_info["rows"] += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    file_info["invalid_rows"] += 1
                    continue
                trace_id = str(row.get("trace_id") or "")
                if trace_id in wanted:
                    result.seen_trace_ids.add(trace_id)
                    file_info["wanted_rows"] += 1
                anchor = anchors.get(trace_id)
                if anchor is None:
                    continue
                entry = result.per_trace[trace_id]
                state = row.get("state") if isinstance(row.get("state"), str) else ""
                entry["n_chunks"] += 1
                entry["_splits"].add(str(split_name))
                kind = _chunk_label_kind(row.get("chunk_label"))
                entry["chunk_label_counts"][kind] += 1
                if ANNOTATION_MARKER in state:
                    entry["blinding"]["annotation_chunks"] += 1
                if entry["_source_ok"] and not entry["_found"]:
                    match = find_anchor(
                        state, anchor.search_snippet, loc_kind=anchor.loc_kind
                    )
                    if match.status in {"preserved", "lost"}:
                        entry["_searched"] = True
                    if match.status == "preserved":
                        entry["_found"] = True
                        entry["_variant"] = match.variant
                        entry["_offset"] = match.offset
                        entry["anchor"] = asdict(match)
                        if kind == "positive":
                            entry["found_in_positive_chunk"] = True

    for trace_id, entry in result.per_trace.items():
        anchor = anchors[trace_id]
        if not entry["_found"]:
            if entry["_searched"]:
                entry["anchor"] = asdict(
                    AnchorMatch(
                        "lost",
                        search_sha256=_sha256_text(anchor.search_snippet),
                        reason="snippet_not_found",
                    )
                )
            elif entry["n_chunks"] == 0 and anchor.measurable:
                entry["anchor"] = asdict(
                    AnchorMatch(
                        "unknown",
                        search_sha256=_sha256_text(anchor.search_snippet),
                        reason="no_kev_chunks",
                    )
                )
        entry["splits"] = sorted(entry.pop("_splits"))
        entry.pop("_searched")
        entry.pop("_found")
        entry.pop("_variant")
        entry.pop("_offset")
        entry.pop("_source_ok")
        if entry["blinding"]["annotation_chunks"]:
            entry["blinding"]["status"] = "fail"
        elif entry["n_chunks"]:
            entry["blinding"]["status"] = "pass"
    return result


def _grouped_counts(
    rows: list[dict[str, Any]], key_fn: Any
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(key_fn(row))
        bucket = grouped.setdefault(
            key,
            {
                "rows": 0,
                "measurements": Counter(),
                "blinding": Counter(),
                "anchor_status": Counter(),
            },
        )
        bucket["rows"] += 1
        bucket["measurements"][row.get("measurement")] += 1
        bucket["blinding"][(row.get("blinding") or {}).get("status")] += 1
        anchors = row.get("anchors") or []
        bucket["anchor_status"][anchors[0].get("status") if anchors else "none"] += 1
    return {
        key: {
            "rows": bucket["rows"],
            "measurements": _counter_dict(bucket["measurements"]),
            "blinding": _counter_dict(bucket["blinding"]),
            "anchor_status": _counter_dict(bucket["anchor_status"]),
        }
        for key, bucket in sorted(grouped.items())
    }


def summarize_atif_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    anchor_status = Counter()
    source_status = Counter()
    by_loc_kind: dict[str, Counter[str]] = {}
    forbidden_kinds = Counter()
    for row in materialized:
        for hit in (row.get("blinding") or {}).get("forbidden_hits") or []:
            forbidden_kinds[hit.get("kind")] += 1
        for payload in row.get("anchors") or []:
            anchor_status[payload.get("status")] += 1
            source_status[payload.get("source_status")] += 1
            by_loc_kind.setdefault(str(payload.get("loc_kind")), Counter())[
                payload.get("status")
            ] += 1
    return {
        "consumer": "materialized_atif",
        "rows": len(materialized),
        "by_measurement": _counter_dict(
            Counter(row.get("measurement") for row in materialized)
        ),
        "by_blinding_status": _counter_dict(
            Counter((row.get("blinding") or {}).get("status") for row in materialized)
        ),
        "forbidden_hit_kinds": _counter_dict(forbidden_kinds),
        "anchors": {
            "status": _counter_dict(anchor_status),
            "source_status": _counter_dict(source_status),
            "by_loc_kind": {
                kind: _counter_dict(counts) for kind, counts in sorted(by_loc_kind.items())
            },
        },
        "by_family_split": _grouped_counts(
            materialized, lambda row: (row.get("source") or {}).get("family_split")
        ),
        "by_benchmark": _grouped_counts(
            materialized, lambda row: (row.get("source") or {}).get("benchmark")
        ),
    }


def summarize_kev(
    result: KevAuditResult, subset_rows: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    subset_by_id = {
        str(row.get("trace_id") or row.get("id")): row for row in subset_rows
    }
    anchor_status = Counter()
    source_status = Counter()
    by_loc_kind: dict[str, Counter[Any]] = {}
    grouped: dict[str, dict[str, Any]] = {}
    chunk_labels = Counter()
    for trace_id, entry in result.per_trace.items():
        status = (entry.get("anchor") or {}).get("status")
        anchor_status[status] += 1
        source_status[entry.get("source_status")] += 1
        by_loc_kind.setdefault(str(entry.get("loc_kind")), Counter())[status] += 1
        for kind, count in (entry.get("chunk_label_counts") or {}).items():
            chunk_labels[kind] += int(count)
        subset_row = subset_by_id.get(trace_id, {})
        benchmark = entry.get("benchmark") or subset_row.get("benchmark")
        for group_name, key in (
            ("by_family_split", entry.get("family_split")),
            ("by_benchmark", benchmark),
        ):
            bucket = grouped.setdefault(group_name, {}).setdefault(
                str(key), {"traces": 0, "anchor_status": Counter(), "chunks": 0}
            )
            bucket["traces"] += 1
            bucket["anchor_status"][status] += 1
            bucket["chunks"] += int(entry.get("n_chunks") or 0)

    def render_group(name: str) -> dict[str, Any]:
        return {
            key: {
                "traces": bucket["traces"],
                "chunks": bucket["chunks"],
                "anchor_status": _counter_dict(bucket["anchor_status"]),
            }
            for key, bucket in sorted(grouped.get(name, {}).items())
        }

    return {
        "consumer": "kev_windows",
        "files": result.files,
        "seen_wanted_traces": len(result.seen_trace_ids),
        "anchor_traces": len(result.per_trace),
        "traces_with_chunks": sum(
            1 for entry in result.per_trace.values() if entry.get("n_chunks")
        ),
        "chunk_label_counts": _counter_dict(chunk_labels),
        "found_in_positive_chunk": sum(
            1
            for entry in result.per_trace.values()
            if entry.get("found_in_positive_chunk")
        ),
        "blinding_failures": sum(
            1
            for entry in result.per_trace.values()
            if (entry.get("blinding") or {}).get("status") == "fail"
        ),
        "anchors": {
            "status": _counter_dict(anchor_status),
            "source_status": _counter_dict(source_status),
            "by_loc_kind": {
                kind: _counter_dict(counts) for kind, counts in sorted(by_loc_kind.items())
            },
        },
        "by_family_split": render_group("by_family_split"),
        "by_benchmark": render_group("by_benchmark"),
    }
