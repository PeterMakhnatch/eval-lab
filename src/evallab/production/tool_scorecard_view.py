"""Read-only trace-tool scorecard view: whole-run tools, cascade and facets side by side.

Everything here is a projection of files other lanes already retain under the
dashboard's ``<data_root>``:

* ``trace-tools/tt1-scorecard.json`` -- ``evallab.trace_tool_scorecard/v1``: per tool and
  detector counts on labelled cohorts. Abstentions are counted apart from negatives.
* ``run-atlas/scorecards/*.json`` -- ``run-atlas-scorecard/v1``: facet detectors and the
  frozen cascade on the same traces. The newest valid file is used.
* ``trace-tools/spend.jsonl`` and ``run-atlas/per-run-accounting-*.jsonl`` -- recorded usage.
* ``trace-tools/results-v1.jsonl`` and ``run-atlas/facet-calls-v1.jsonl`` -- when each
  tool last wrote a result.

Nothing here opens a database, calls a model or the network, joins a label, or writes.
Counts are the producers'; the view never computes a recall, a false-alarm rate or a
cost per run. The browser derives those from counts, so a tool that abstained on a run
is never shown as having missed it, and a missing source is shown as missing.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evallab.production.atlas_view import json_reason, read_regular, strict_loads

TOOLS_DIR = "trace-tools"
ATLAS_DIR = "run-atlas"
TOOL_SCORECARD_FILE = "tt1-scorecard.json"
RESULTS_FILE = "results-v1.jsonl"
SPEND_FILE = "spend.jsonl"
ATLAS_SCORECARD_DIR = "scorecards"
ACCOUNTING_PREFIX = "per-run-accounting-"
FACET_CALLS_FILE = "facet-calls-v1.jsonl"
BLINDING_REFRESH_FILE = "blinding-refresh-v1.json"
REBLINDED_FILE = "reblinded-facets-v1.jsonl"
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_STREAM_BYTES = 1024 * 1024 * 1024
MAX_NOTES = 10
SCHEMA = "evallab.trace_tool_scorecard_view/v1"

# (key, title, tool-scorecard label set, run-atlas population)
POPULATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("goodfire_cm9", "Goodfire CM-9 sample", "goodfire_cm9_alarm", "goodfire_cm6"),
    ("cheatbench_dev", "CheatBench DEV cross-test", "cheatbench_dev_alarm", "cheatbench_dev"),
)
# Display order and names. A tool the scorecard adds later is listed after these.
TOOLS: tuple[tuple[str, str], ...] = (
    ("facets", "Run Atlas facets"),
    ("cascade", "Rule cascade (frozen CM-6)"),
    ("laminar", "Laminar Signals"),
    ("docent", "Docent readings"),
    ("scout", "Inspect Scout scanners"),
    ("scout_metr", "Scout METR reward-hacking scanner"),
    ("omp", "OMP subscription readings (TT-7)"),
    ("harbor_analyze", "harbor analyze"),
)
TOOL_HEADLINE = "any_flag"

_Count = Annotated[int, Field(ge=0)]
_Label = Annotated[str, Field(min_length=1, pattern=r"\S")]
_Fraction = Annotated[float, Field(ge=0, le=1)]
_Percent = Annotated[float, Field(ge=0, le=100)]


class _Strict(BaseModel):
    # Producers may add fields; only the ones used here are contracts.
    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False)


def _interval(value: list[float] | None, scale: float) -> list[float] | None:
    return None if value is None else [value[0] / scale, value[1] / scale]


def _check_interval(low_high: list[float] | None, rate: float | None, tolerance: float) -> None:
    if low_high is None:
        return
    if len(low_high) != 2 or not low_high[0] <= low_high[1]:
        raise ValueError("a confidence interval must be an ordered pair")
    if rate is not None and not low_high[0] - tolerance <= rate <= low_high[1] + tolerance:
        raise ValueError("a recorded rate lies outside its own interval")


class _Overlap(_Strict):
    both: _Count
    tool_only: _Count
    cascade_only: _Count
    neither: _Count


class _ToolRow(_Strict):
    """One tool detector on one labelled cohort; ``abstain_*`` runs were not read."""

    tool: _Label
    detector: _Label
    n_pos: _Count
    n_neg: _Count
    tp: _Count
    fp: _Count
    abstain_pos: _Count
    abstain_neg: _Count
    recall: _Fraction | None
    far: _Fraction | None
    recall_ci95: Annotated[list[_Fraction], Field(min_length=2, max_length=2)] | None
    far_ci95: Annotated[list[_Fraction], Field(min_length=2, max_length=2)] | None
    overlap: _Overlap

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        for abstained, total in ((self.abstain_pos, self.n_pos), (self.abstain_neg, self.n_neg)):
            if abstained > total:
                raise ValueError("abstentions exceed the labelled runs")
        positives = self.n_pos - self.abstain_pos
        negatives = self.n_neg - self.abstain_neg
        if self.tp > positives or self.fp > negatives:
            raise ValueError("hits exceed the runs the tool decided")
        for hits, decided, rate, interval in (
            (self.tp, positives, self.recall, self.recall_ci95),
            (self.fp, negatives, self.far, self.far_ci95),
        ):
            if (rate is None) != (not decided):
                raise ValueError("a rate must exist exactly when some runs were decided")
            if rate is not None and abs(rate - hits / decided) > 1e-9:
                raise ValueError("a recorded rate disagrees with its counts")
            _check_interval(interval, rate, 1e-9)
        if self.overlap.both + self.overlap.tool_only > self.tp:
            raise ValueError("overlap exceeds the tool's hits")
        return self


class _LabelCounts(_Strict):
    total: _Count
    positive: _Count
    negative: _Count
    unscored: _Count

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.positive + self.negative + self.unscored != self.total:
            raise ValueError("label counts do not add up to the cohort")
        return self


class _AtlasDetector(_Strict):
    """A facet rule or the cascade on a labelled cohort; rates are percentages."""

    tp: _Count
    fp: _Count
    positives: _Count
    negatives: _Count
    recall: _Percent
    far: _Percent
    recall_ci: Annotated[list[_Percent], Field(min_length=2, max_length=2)]
    far_ci: Annotated[list[_Percent], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.tp > self.positives or self.fp > self.negatives:
            raise ValueError("hits exceed the labelled runs")
        for hits, total, rate, interval in (
            (self.tp, self.positives, self.recall, self.recall_ci),
            (self.fp, self.negatives, self.far, self.far_ci),
        ):
            # Rates are rounded to one decimal place by the producer.
            if total and abs(rate - 100 * hits / total) > 0.051:
                raise ValueError("a recorded rate disagrees with its counts")
            _check_interval(interval, rate, 0.051)
        return self


class _AtlasPopulation(_Strict):
    paired_detectors: dict[_Label, _AtlasDetector]
    strata: dict[str, _Count]


class _AtlasScorecard(_Strict):
    schema_: Literal["run-atlas-scorecard/v1"] = Field(alias="schema")
    headline: _Label
    populations: dict[str, dict[str, Any]]


class _ToolScorecard(_Strict):
    schema_: Literal["evallab.trace_tool_scorecard/v1"] = Field(alias="schema")
    label_sets: dict[str, dict[str, Any]]


def _moment(text: str) -> datetime:
    """An ISO-8601 time that names its zone; anything else is not a recorded time."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return moment


class _Spend(_Strict):
    at: str
    tool: _Label
    route: str
    runs: _Count
    input_tokens: _Count
    output_tokens: _Count
    usd: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def validate_timestamp(self) -> Self:
        _moment(self.at)
        return self


class _Accounting(_Strict):
    facet_model: _Label
    reported_input_tokens_all_attempts: _Count
    reported_output_tokens_all_attempts: _Count
    recorded_subscription_cost_usd: Annotated[float, Field(ge=0)]


class _Refreshed(_Strict):
    run_id: _Label


@dataclass(frozen=True)
class Source:
    """One retained file the page read, with a digest or the reason it was not used."""

    name: str
    path: Path | None
    digest: str | None = None
    error: str | None = None
    rejected_n: int = 0
    notes: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "file": self.path.name if self.path else self.name,
            "sha256": self.digest,
            "error": self.error,
            "rejected_n": self.rejected_n,
            "notes": list(self.notes),
        }


def _newest(directory: Path, accepts: Callable[[str], bool]) -> list[Path]:
    """Regular, non-link files in a directory, newest first; none if it is absent."""
    found: list[tuple[int, str, Path]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not accepts(entry.name):
                    continue
                seen = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(seen.st_mode):
                    found.append((seen.st_mtime_ns, entry.name, Path(entry.path)))
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [path for _, _, path in sorted(found, reverse=True)]


def _stat_key(path: Path | None) -> tuple[int, int, int] | None:
    if path is None:
        return None
    try:
        seen = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (seen.st_mtime_ns, seen.st_size, seen.st_ino)


def _read_document(path: Path | None, name: str) -> tuple[Any, Source]:
    """A bounded JSON object, or an explanation of why there is none."""
    if path is None:
        return None, Source(name, None, error="not retained")
    try:
        raw = read_regular(path.parent, path.name, MAX_DOCUMENT_BYTES)
        if raw is None:
            return None, Source(name, path, error="not retained")
        document = strict_loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        return None, Source(name, path, error=json_reason(error))
    return document, Source(name, path, digest=hashlib.sha256(raw).hexdigest())


def _lines(path: Path) -> Iterator[bytes]:
    """Stream a regular file without following links; a bound stops a runaway file."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        if size > MAX_STREAM_BYTES:
            raise ValueError(f"{path.name} exceeds {MAX_STREAM_BYTES} bytes")
        yield from stream


def _read_records(path: Path | None, name: str, model: type[_Strict]) -> tuple[list[Any], Source]:
    """Validated JSONL records; a bad line is counted and skipped, never guessed."""
    if path is None:
        return [], Source(name, None, error="not retained")
    records: list[Any] = []
    rejected = 0
    notes: list[str] = []
    digest = hashlib.sha256()
    try:
        for number, line in enumerate(_lines(path), 1):
            digest.update(line)
            if not line.strip():
                continue
            try:
                records.append(model.model_validate(strict_loads(line.decode("utf-8"))))
            except (ValueError, ValidationError) as error:
                rejected += 1
                if len(notes) < MAX_NOTES:
                    notes.append(f"line {number}: {json_reason(error)}")
    except FileNotFoundError:
        return [], Source(name, path, error="not retained")
    except (OSError, ValueError) as error:
        return [], Source(name, path, error=json_reason(error))
    return records, Source(name, path, digest.hexdigest(), None, rejected, tuple(notes))


def _latest_time(
    path: Path | None, name: str, tool_of: Callable[[Any], str | None]
) -> tuple[dict[str, datetime], int, Source]:
    """Latest ``at`` per tool from a result stream; unreadable lines are counted."""
    latest: dict[str, datetime] = {}
    if path is None:
        return latest, 0, Source(name, None, error="not retained")
    skipped = 0
    try:
        for line in _lines(path):
            if not line.strip():
                continue
            try:
                row = strict_loads(line.decode("utf-8"))
                tool = tool_of(row)
                if "at" not in row:
                    continue  # e.g. a call row written before it had a time
                moment = _moment(row["at"])
                if tool is None:
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                skipped += 1
                continue
            if tool not in latest or moment > latest[tool]:
                latest[tool] = moment
    except FileNotFoundError:
        return {}, 0, Source(name, path, error="not retained")
    except (OSError, ValueError) as error:
        return {}, 0, Source(name, path, error=json_reason(error))
    return latest, skipped, Source(name, path, rejected_n=skipped)


def _detector(row: _ToolRow) -> dict[str, Any]:
    return {
        "detector": row.detector,
        "positives": row.n_pos,
        "negatives": row.n_neg,
        "tp": row.tp,
        "fp": row.fp,
        "positives_decided": row.n_pos - row.abstain_pos,
        "negatives_decided": row.n_neg - row.abstain_neg,
        "recall_ci": row.recall_ci95,
        "far_ci": row.far_ci95,
        "caught_beyond_cascade": row.overlap.tool_only,
        "cascade_only_caught": row.overlap.cascade_only,
    }


def _atlas_detector(name: str, row: _AtlasDetector) -> dict[str, Any]:
    return {
        "detector": name,
        "positives": row.positives,
        "negatives": row.negatives,
        "tp": row.tp,
        "fp": row.fp,
        "positives_decided": row.positives,
        "negatives_decided": row.negatives,
        "recall_ci": _interval(row.recall_ci, 100),
        "far_ci": _interval(row.far_ci, 100),
        "caught_beyond_cascade": None,
        "cascade_only_caught": None,
    }


def _tool_scorecard(
    document: Any, notes: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    """Per population: label counts and every tool's validated detector rows."""
    labels: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    rejected = 0
    scorecard = _ToolScorecard.model_validate(document)
    for key, title, label_set, _ in POPULATIONS:
        raw_set = scorecard.label_sets.get(label_set)
        if raw_set is None:
            notes.append(f"{title}: label set {label_set} is absent")
            continue
        try:
            counts = _LabelCounts.model_validate(raw_set.get("label_counts"))
        except ValidationError as error:
            notes.append(f"{title}: {json_reason(error)}")
            continue
        labels[key] = counts.model_dump()
        rows = raw_set.get("rows")
        for raw in rows if isinstance(rows, list) else []:
            try:
                row = _ToolRow.model_validate(raw)
            except ValidationError as error:
                rejected += 1
                if len(notes) < MAX_NOTES:
                    notes.append(f"{title}: {json_reason(error)}")
                continue
            if (row.n_pos, row.n_neg) != (counts.positive, counts.negative):
                rejected += 1
                if len(notes) < MAX_NOTES:
                    notes.append(
                        f"{title}: {row.tool}/{row.detector} counts disagree with its cohort"
                    )
                continue
            tools.setdefault(row.tool, {}).setdefault(key, []).append(_detector(row))
    return labels, tools, rejected


def _atlas_scorecards(
    paths: list[Path],
) -> tuple[dict[str, dict[str, Any]], Source]:
    """Facet and cascade rows from the newest valid ``run-atlas-scorecard/v1`` file."""
    notes: list[str] = []
    for path in paths:
        document, source = _read_document(path, "atlas scorecard")
        if source.error:
            notes.append(f"{path.name}: {source.error}")
            continue
        try:
            scorecard = _AtlasScorecard.model_validate(document)
        except ValidationError as error:
            notes.append(f"{path.name}: {json_reason(error)}")
            continue
        found: dict[str, dict[str, Any]] = {}
        for key, title, _, population in POPULATIONS:
            try:
                parsed = _AtlasPopulation.model_validate(scorecard.populations[population])
            except (KeyError, ValidationError) as error:
                notes.append(f"{path.name}: {title}: {json_reason(error)}")
                continue
            cascade = parsed.paired_detectors.get("cascade")
            headline = parsed.paired_detectors.get(scorecard.headline)
            found[key] = {
                "cascade": None if cascade is None else _atlas_detector("cascade", cascade),
                "facets": None
                if headline is None
                else _atlas_detector(scorecard.headline, headline),
                "facet_details": [
                    _atlas_detector(name, row)
                    for name, row in sorted(parsed.paired_detectors.items())
                    if name != "cascade"
                ],
                "strata": parsed.strata,
            }
        return found, Source(source.name, path, source.digest, None, 0, tuple(notes[:MAX_NOTES]))
    return {}, Source("atlas scorecard", None, error="; ".join(notes) or "not retained")


def _usage(entries: list[tuple[int, int, int, float, str]]) -> dict[str, Any]:
    """Sums over entries that recorded usage; zero-usage entries are not denominators."""
    used = [entry for entry in entries if entry[1] or entry[2] or entry[3]]
    return {
        "entries": len(entries),
        "usage_entries": len(used),
        "usage_runs": sum(entry[0] for entry in used),
        "input_tokens": sum(entry[1] for entry in used),
        "output_tokens": sum(entry[2] for entry in used),
        "usd": sum(entry[3] for entry in used),
        "routes": sorted({entry[4] for entry in entries if entry[4]}),
    }


def _blinding(directory: Path) -> tuple[dict[str, int] | None, Source]:
    document, source = _read_document(directory / BLINDING_REFRESH_FILE, "blinding refresh")
    if source.error:
        return None, source
    try:
        listed = {str(item) for item in document["run_ids"]}
    except (KeyError, TypeError) as error:
        return None, Source(source.name, source.path, error=json_reason(error))
    done, reblinded = _read_records(directory / REBLINDED_FILE, "re-extracted facets", _Refreshed)
    if reblinded.error:
        return None, reblinded
    return {"listed": len(listed), "pending": len(listed - {item.run_id for item in done})}, source


@dataclass(frozen=True)
class _Paths:
    tool_scorecard: Path
    results: Path
    spend: Path
    atlas_scorecards: list[Path]
    accounting: Path | None
    facet_calls: Path
    atlas_dir: Path

    def signature(self) -> tuple[Any, ...]:
        scorecards = [(path.name, _stat_key(path)) for path in self.atlas_scorecards]
        return (
            _stat_key(self.tool_scorecard),
            _stat_key(self.results),
            _stat_key(self.spend),
            scorecards,
            self.accounting and (self.accounting.name, _stat_key(self.accounting)),
            _stat_key(self.facet_calls),
            _stat_key(self.atlas_dir / BLINDING_REFRESH_FILE),
            _stat_key(self.atlas_dir / REBLINDED_FILE),
        )


def _paths(data_root: Path) -> _Paths:
    atlas = data_root / ATLAS_DIR
    accounting = _newest(
        atlas, lambda name: name.startswith(ACCOUNTING_PREFIX) and name.endswith(".jsonl")
    )
    return _Paths(
        data_root / TOOLS_DIR / TOOL_SCORECARD_FILE,
        data_root / TOOLS_DIR / RESULTS_FILE,
        data_root / TOOLS_DIR / SPEND_FILE,
        _newest(atlas / ATLAS_SCORECARD_DIR, lambda name: name.endswith(".json")),
        accounting[0] if accounting else None,
        atlas / FACET_CALLS_FILE,
        atlas,
    )


def build_tool_scorecard(data_root: Path) -> dict[str, Any]:
    """The side-by-side tool scorecard; every source degrades independently."""
    return _build(_paths(data_root))


def _build(paths: _Paths) -> dict[str, Any]:
    sources: list[Source] = []
    notes: list[str] = []

    labels: dict[str, dict[str, Any]] = {}
    tool_rows: dict[str, dict[str, Any]] = {}
    document, source = _read_document(paths.tool_scorecard, "tool scorecard")
    if document is not None:
        try:
            labels, tool_rows, rejected = _tool_scorecard(document, notes)
            source = Source(
                source.name, source.path, source.digest, None, rejected, tuple(notes[:MAX_NOTES])
            )
        except ValidationError as error:
            source = Source(source.name, source.path, error=json_reason(error))
    sources.append(source)

    atlas, source = _atlas_scorecards(paths.atlas_scorecards)
    sources.append(source)

    spend, source = _read_records(paths.spend, "spend ledger", _Spend)
    sources.append(source)
    by_tool: dict[str, list[tuple[int, int, int, float, str]]] = {}
    last: dict[str, tuple[datetime, str]] = {}
    for entry in spend:
        by_tool.setdefault(entry.tool, []).append(
            (entry.runs, entry.input_tokens, entry.output_tokens, entry.usd, entry.route)
        )
        moment = _moment(entry.at)
        if entry.tool not in last or moment > last[entry.tool][0]:
            last[entry.tool] = (moment, "spend ledger")

    results, skipped, source = _latest_time(
        paths.results, "scanner results", lambda row: row["tool"]
    )
    sources.append(source)
    for tool, moment in results.items():
        if tool not in last or moment > last[tool][0]:
            last[tool] = (moment, "scan results")

    calls, _, source = _latest_time(paths.facet_calls, "facet calls", lambda _row: "facets")
    sources.append(source)
    if "facets" in calls:
        last["facets"] = (calls["facets"], "facet calls")

    accounting, source = _read_records(paths.accounting, "facet accounting", _Accounting)
    sources.append(source)
    if accounting:
        by_tool["facets"] = [
            (
                1,
                item.reported_input_tokens_all_attempts,
                item.reported_output_tokens_all_attempts,
                item.recorded_subscription_cost_usd,
                item.facet_model,
            )
            for item in accounting
        ]

    blinding, source = _blinding(paths.atlas_dir)
    sources.append(source)

    keys = [key for key, _ in TOOLS]
    extra = sorted(set(tool_rows) - set(keys))
    names = dict(TOOLS)
    tools: list[dict[str, Any]] = []
    for key in [*keys, *extra]:
        populations: dict[str, Any] = {}
        for population, _, _, _ in POPULATIONS:
            if key in ("facets", "cascade"):
                found = (atlas.get(population) or {}).get(key)
                details = (
                    (atlas.get(population) or {}).get("facet_details", [])
                    if key == "facets"
                    else []
                )
                populations[population] = None if found is None else {**found, "details": details}
            else:
                rows = tool_rows.get(key, {}).get(population)
                headline = next(
                    (row for row in rows or [] if row["detector"] == TOOL_HEADLINE), None
                )
                populations[population] = (
                    None if headline is None else {**headline, "details": rows}
                )
        moment = last.get(key)
        tools.append(
            {
                "tool": key,
                "name": names.get(key, key),
                "populations": populations,
                "usage": _usage(by_tool[key]) if key in by_tool else None,
                "last_run_at": moment[0].isoformat() if moment else None,
                "last_run_source": moment[1] if moment else None,
                "pending_blinding_refresh": blinding if key == "facets" else None,
            }
        )
    return {
        "schema": SCHEMA,
        "populations": [
            {"key": key, "title": title, "labels": labels.get(key)}
            for key, title, _, _ in POPULATIONS
        ],
        "tools": tools,
        "sources": [item.describe() for item in sources],
        "result_lines_skipped": skipped,
        "notice": (
            "Counts come from retained scorecards. A tool that did not read a run abstained: "
            "that run is neither a miss nor a clean pass. Recorded cost is marginal spend only; "
            "$0 means a subscription or free allowance, not unlimited capacity. Cost per 1k runs "
            "divides recorded usage by the run count on the same ledger line, so an interrupted "
            "line is not adjusted. DEV and cross-test cohorts only; nothing is ranked."
        ),
    }


class ToolScorecardCache:
    """Rebuilds the view only when one of its retained files changes on disk."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        self._lock = threading.Lock()
        self._cached: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    def get(self) -> Mapping[str, Any]:
        with self._lock:
            paths = _paths(self._data_root)
            signature = paths.signature()
            if self._cached is None or self._cached[0] != signature:
                self._cached = (signature, _build(paths))
            return self._cached[1]


__all__ = ["SCHEMA", "ToolScorecardCache", "build_tool_scorecard"]
