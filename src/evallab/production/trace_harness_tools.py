"""Import-only trace-harness detectors over results the Trace Tools lane stored.

``ScoutDetector`` (``scout``), ``ScoutLegacyDetector`` (``scout_legacy``), ``MetrDetector``
(``metr``), ``LaminarDetector`` (``laminar``), ``DocentDetector`` (``docent``),
``OmpFormDetector`` (``omp_form``) and the generic ``LedgerDetector`` (``ledger``) turn the
per-run rows of the ``evallab.trace_tool_result/v1`` ledgers under
``~/.local/state/evallab-prod-live/trace-tools/`` into ``trace-findings/v1`` records.
Nothing here runs Scout, METR, Laminar, Docent or a model, and nothing reads labels: a run
with no stored row is omitted, never ``clear``. ``--allow-model`` changes nothing here:
METR and Laminar Signals are paid readers and the other tools run in their own lanes, so
missing runs are always omitted rather than generated.

Verdicts follow the published Trace Tools scorecard (``tool_scorecard``): a
flag is the tool's own flag, ``null`` abstains, and a multi-scanner tool is the
``any_flag`` union of its *flagging* scanners (a scanner with at least one
non-null flag anywhere in the ledger; always-null informational scanners such as
``restored_tool_calls`` are excluded). Per run the union is true if any scanner
flags, false if none flags and one is explicitly false, else null.

Per tool:

* ``scout``: one detector, the union of the deterministic Inspect Scout 0.5.4
  scanners over the current ``native-actor-v2`` projection ledger
  (``scout-native-actor-v2/results-v1.jsonl``); per-scanner hits are in the
  summary; ``reader_model`` is null; ``kind`` is ``loop`` when only
  ``repeated_assistant_message`` flagged, else ``cheat``. ``scout_legacy`` is the same
  union over the retained pre-projection ledger (``results-v1.jsonl``) that the published
  ``tt1-scorecard.md`` was computed on.
* ``metr``: the reward-hacking scanner, union of its predeclared ``@ge5`` and
  ``@ge7`` thresholds on the native 0-10 score (``score``). One stored arm per instance,
  chosen by the reader override (``metr:xai-oauth/grok-4.3``,
  ``metr:openrouter/google/gemini-2.5-flash-lite``, ``metr:<reader>@none`` for an arm stored
  without a projection); default is ``kimi-code/k3.1-pro-highspeed`` on ``native-actor-v2``.
  Arms are parsed from the ledger ``detector_version``, not listed. Tokens and the reader's
  explanation come from the TT-4 ``reports-scan_id=*.json`` receipts.
* ``laminar``: union of the Laminar Signals stored for our own Harbor runs
  (``laminar-own.jsonl`` rows in the ledger); ``reader_model`` is the Signals
  profile model ``zai/glm-5.3-flash``; tokens sum the per-signal runs.
* ``docent`` / ``omp_form``: one reader model per instance (the TT-7 form), by
  reader override; default ``anthropic/claude-sonnet-5-5``. ``docent`` also takes
  ``docent:rh_reading_v1`` (the own-run hosted rubric, ``openai/gpt-5.4-mini``).
* ``ledger``: every (tool, version, reader) in the ledger no adapter above reads (a new
  product, a new Laminar version, a METR arm of an unlisted reader); see ``LedgerDetector``.
  ``all`` also expands to every stored METR arm and TT-7 form reader (``discovered_specs``).

Evidence steps are original step ids: the stored citations already are
(Scout/METR citations come from native message references mapped through the
keymap, Docent/TT-7 citations are validated against the run's steps). Any
cited id that is not a step of the harness run is dropped rather than guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from evallab.production.trace_harness import HarnessOptions, HarnessRun, finding
from evallab.readers import ACTOR_PROJECTION_VERSION
from evallab.readers.docent_reading import DETECTOR as DOCENT_RH_DETECTOR
from evallab.readers.docent_reading import MODEL as DOCENT_RH_MODEL
from evallab.readers.docent_reading import VERSION as DOCENT_RH_VERSION
from evallab.readers.scout_clouddb import SCOUT_VERSION

RESULT_SCHEMA = "evallab.trace_tool_result/v1"
STATE_ROOT = Path.home() / ".local/state/evallab-prod-live"
TRACE_TOOLS = STATE_ROOT / "trace-tools"
LEGACY_PROJECTION = "unversioned-pre-native-actor-v2"
SCOUT_REPEAT = "repeated_assistant_message"

#: METR scanner build the stored rows were produced by (``metr-scanners`` 0.1.11).
METR_BUILD = "metr-scanners/0.1.11@c12893d1;scanner-v0"
#: Readers whose stored arms are decoded by name. An arm's ledger ``detector_version`` is
#: ``<build>;<route>-<model slug>[;option...][;projection-<v>]`` and the model slug cannot be
#: inverted in general (``kimi-code/k3.1`` and ``kimi/code-k3.1`` slug alike), so a reader is
#: matched forward: it selects the arm whose slug it produces. ``openrouter/<model>`` readers
#: are OpenRouter routes, every other reader an OMP route. Arms of any other reader are still
#: imported, by ``ledger_detectors``, with an unrecorded reader.
METR_KNOWN_READERS = (
    "kimi-code/k3.1-pro-highspeed",
    "xai-oauth/grok-4.3",
    "openrouter/google/gemini-2.5-flash-lite",
)
METR_DEFAULT_READER = "kimi-code/k3.1-pro-highspeed"
METR_DETECTORS = ("reward_hacking_scanner@ge5", "reward_hacking_scanner@ge7")
METR_NATIVE = "reward_hacking_scanner"
METR_THRESHOLDS = "ge5 and ge7"
METR_ROUTES = ("omp", "openrouter")
#: ``trace_findings`` identifier rule for ``detector_version``.
VERSION_LIMIT = 64
TT4_ROOT = TRACE_TOOLS / "tt4-metr-20261027"

LAMINAR_VERSION = "existing-signals/readback-v1"
#: Model of the ``evallab-zai`` Laminar LLM profile (``laminar-own-signals.json``).
LAMINAR_MODEL = "zai/glm-5.3-flash"

TT7_DOCENT_VERSION = "tt7-frozen-20261027"
TT7_OMP_VERSION = "tt7-frozen-20261027/v1"
TT7_DEFAULT_READER = "anthropic/claude-sonnet-5-5"
DOCENT_RH_VERSIONS = frozenset({"1", DOCENT_RH_VERSION})

Row = Mapping[str, Any]
Ledger = dict[tuple[str, str], Row]


# --------------------------------------------------------------------------- #
# ledger access
# --------------------------------------------------------------------------- #


def _instant(row: Row) -> datetime:
    at = datetime.fromisoformat(str(row["at"]))
    if at.utcoffset() is None:
        raise ValueError("result at must include a timezone")
    return at


def _utc(at: str) -> str:
    return datetime.fromisoformat(at).astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stream(path: Path) -> Iterator[Row]:
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as error:
                raise ValueError(f"{path}:{number}: invalid JSON") from error
            if not isinstance(row, dict):
                raise TypeError(f"{path}:{number}: expected a JSON object")
            if row.get("schema") != RESULT_SCHEMA:
                raise ValueError(f"{path}:{number}: unsupported result schema {row.get('schema')!r}")
            yield row


def read_ledger(
    path: Path, tool: str, keep: Callable[[Row], bool], wanted: Collection[str],
) -> tuple[Ledger, set[str]]:
    """Latest row per ``(detector, run_id)`` for the requested runs, plus flagging detectors.

    Latest means the latest ISO instant (``tool_scorecard.load_results`` rule); an
    identical instant is broken by append order. ``flagging`` is every detector
    with at least one non-null flag in the whole ledger slice, requested or not.
    A missing ledger is empty; a malformed one raises.
    """
    latest: Ledger = {}
    instants: dict[tuple[str, str], datetime] = {}
    flagging: set[str] = set()
    wanted_ids = set(wanted)
    try:
        for row in _stream(path):
            if row.get("tool") != tool or not keep(row):
                continue
            flag = row.get("flag")
            if flag is not None and not isinstance(flag, bool):
                raise TypeError(f"{path}: flag must be true, false or null, got {flag!r}")
            detector = str(row["detector"])
            if flag is not None:
                flagging.add(detector)
            if row["run_id"] not in wanted_ids:
                continue
            key = detector, str(row["run_id"])
            at = _instant(row)
            if key not in instants or at >= instants[key]:
                instants[key], latest[key] = at, row
    except FileNotFoundError:
        return {}, set()
    return latest, flagging


def union(flags: Iterable[bool | None]) -> bool | None:
    """True if any flag is true, false if none is true and one is false, else null."""
    values = list(flags)
    if any(value is True for value in values):
        return True
    return False if any(value is False for value in values) else None


def verdict_of(flag: bool | None) -> str:
    return "abstain" if flag is None else "alarm" if flag else "clear"


# --------------------------------------------------------------------------- #
# which stored results exist, and which adapter owns them
# --------------------------------------------------------------------------- #

TT7_PREFIX = "tt7_form:"
#: (tool, detector, detector_version, row-recorded reader or None)
Group = tuple[str, str, str, str | None]


def _row_reader(row: Row) -> str | None:
    """The reader a row records about itself. Absent means unrecorded, never guessed."""
    reader = row.get("reader_model")
    if reader is None or reader == "":
        return None
    if not isinstance(reader, str) or "/" not in reader:
        raise ValueError(f"row reader_model must be provider/id, got {reader!r}")
    return reader


@lru_cache(maxsize=8)
def _groups_at(path: str, mtime_ns: int, size: int) -> frozenset[Group]:
    del mtime_ns, size  # cache key only: a rewritten ledger is read again
    return frozenset(
        (str(row["tool"]), str(row["detector"]), str(row["detector_version"]), _row_reader(row))
        for row in _stream(Path(path))
    )


def ledger_groups(path: Path) -> frozenset[Group]:
    """Every distinct (tool, detector, version, reader) the ledger holds; a missing ledger holds none."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return frozenset()
    return _groups_at(str(path), stat.st_mtime_ns, stat.st_size)


def arm_slug(reader: str) -> str:
    """The ledger arm segment a reader produces (forward only; slugs are not invertible)."""
    if reader.startswith("openrouter/"):
        return "openrouter-" + reader.removeprefix("openrouter/").replace("/", "-")
    return "omp-" + reader.replace("/", "-")


@dataclass(frozen=True)
class MetrArm:
    """One stored METR arm, parsed from its ledger ``detector_version``.

    ``<build>;<route>-<model slug>[;option...][;projection-<v>]``. ``version`` is what the
    finding carries: the reader is a field of its own, so the route segment is dropped, and
    ``;`` becomes ``+`` to fit the ``trace_findings`` identifier rule. Options are replaced by
    a digest of themselves only when the full version would exceed the 64-character limit.
    """

    ledger_version: str
    build: tuple[str, ...]
    arm: str
    options: tuple[str, ...]
    projection: str | None

    @property
    def version(self) -> str:
        tail = [] if self.projection is None else [self.projection]
        text = "+".join([*self.build, *self.options, *tail])
        if len(text) > VERSION_LIMIT and self.options:
            digest = hashlib.sha256(";".join(self.options).encode()).hexdigest()[:6]
            text = "+".join([*self.build, digest, *tail])
        if len(text) > VERSION_LIMIT:
            raise ValueError(f"METR arm version exceeds {VERSION_LIMIT} characters: {self.ledger_version}")
        return text

    @property
    def reader(self) -> str | None:
        """The known reader whose slug this arm has, else None (the name is not invertible)."""
        return next((reader for reader in METR_KNOWN_READERS if arm_slug(reader) == self.arm), None)


def parse_metr_arm(version: str) -> MetrArm | None:
    parts = version.split(";")
    if len(parts) < 3 or not parts[0].startswith("metr-scanners/"):
        return None
    route, _, slug = parts[2].partition("-")
    if route not in METR_ROUTES or not slug:
        return None
    projections = [part.removeprefix("projection-") for part in parts[3:] if part.startswith("projection-")]
    if len(projections) > 1:
        return None
    options = tuple(part for part in parts[3:] if not part.startswith("projection-"))
    return MetrArm(version, tuple(parts[:2]), parts[2], options, projections[0] if projections else None)


def metr_arms(path: Path) -> list[MetrArm]:
    """Every METR arm stored in the ledger, whatever its reader, route, options or projection."""
    versions = sorted({version for tool, _, version, _ in ledger_groups(path) if tool == "scout_metr"})
    return [arm for version in versions if (arm := parse_metr_arm(version)) is not None]


def first_class(tool: str, detector: str, version: str) -> bool:
    """Whether a dedicated adapter reads these rows (so the generic one must not)."""
    if tool == "scout":
        return True
    if tool == "scout_metr":
        arm = parse_metr_arm(version)
        return arm is not None and arm.reader is not None
    if tool == "laminar":
        return version == LAMINAR_VERSION
    if tool == "docent":
        return (detector.startswith(TT7_PREFIX) and version == TT7_DOCENT_VERSION) or (
            detector == DOCENT_RH_DETECTOR and version in DOCENT_RH_VERSIONS
        )
    if tool == "omp":
        return detector.startswith(TT7_PREFIX) and version == TT7_OMP_VERSION
    return False


def _owner(connection: sqlite3.Connection, run: HarnessRun, step: int) -> int | None:
    """The ATIF step a cited cloud-DB tool row was folded into (``cloud_doc`` rule)."""
    row = connection.execute(
        "SELECT role, json_extract(flags_json, '$.tool_call_id') FROM trace_steps "
        "WHERE trace_id = ? AND step_index = ?", (run.run_id, step),
    ).fetchone()
    if row is None or row[0] != "tool":
        return None  # off the final parent path (resampled alternative) or unknown row
    for doc_step in run.steps:
        if row[1] and any(call.get("tool_call_id") == row[1] for call in doc_step.get("tool_calls") or ()):
            return doc_step["step_id"]
    earlier = [s["step_id"] for s in run.steps if s.get("source") == "agent" and s["step_id"] < step]
    return max(earlier) if earlier else None


def known_steps(run: HarnessRun, steps: Iterable[Any], database: Path | None = None) -> list[int]:
    """Cited ids as step ids of this run.

    A citation that already is a step id stays. A cloud-DB ``tool`` row, which
    ``cloud_doc`` folds into the agent step that called it, maps to that step
    (read-only lookup in ``database``). Anything else (resampled alternatives
    off the final path, unknown ids) is dropped, never guessed.
    """
    present = {step for step in run.step_ids if isinstance(step, int) and not isinstance(step, bool)}
    found: set[int] = set()
    connection: sqlite3.Connection | None = None
    try:
        for step in steps or ():
            if isinstance(step, str) and step.strip().isdigit():
                step = int(step.strip())
            if isinstance(step, bool) or not isinstance(step, int):
                continue
            if step in present:
                found.add(step)
            elif database is not None and Path(database).exists():
                connection = connection or sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                if (owner := _owner(connection, run, step)) is not None:
                    found.add(owner)
    finally:
        if connection is not None:
            connection.close()
    return sorted(found)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _tokens(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0 else 0


def _no_reader(name: str, options: HarnessOptions) -> None:
    if options.reader:
        raise ValueError(f"{name} has no reader override (got {options.reader!r})")


# --------------------------------------------------------------------------- #
# scout
# --------------------------------------------------------------------------- #


class ScoutDetector:
    """Deterministic Inspect Scout scanners: any-flag union of the flagging scanners."""

    name = "scout"
    reader_model = None

    def __init__(
        self, options: HarnessOptions, *, results: Path | None = None, legacy: bool = False,
    ) -> None:
        _no_reader(self.name, options)
        self.database = options.database
        self.legacy = legacy
        self.projection = LEGACY_PROJECTION if legacy else ACTOR_PROJECTION_VERSION
        self.version = f"inspect-scout/{SCOUT_VERSION}+{self.projection}"
        self.results = results or (
            TRACE_TOOLS / "results-v1.jsonl" if legacy
            else TRACE_TOOLS / f"scout-{ACTOR_PROJECTION_VERSION}" / "results-v1.jsonl"
        )

    def _keep(self, row: Row) -> bool:
        return row.get("projection_version") == (None if self.legacy else self.projection)

    def detect(self, runs: Sequence[HarnessRun]) -> Iterator[dict[str, Any]]:
        ledger, flagging = read_ledger(self.results, "scout", self._keep, [run.run_id for run in runs])
        by_run: dict[str, list[Row]] = defaultdict(list)
        for (detector, run_id), row in ledger.items():
            if detector in flagging:
                by_run[run_id].append(row)
        for run in runs:
            rows = sorted(by_run.get(run.run_id, ()), key=lambda row: row["detector"])
            if not rows:
                continue
            flag = union(row["flag"] for row in rows)
            hits = [row for row in rows if row["flag"] is True]
            numeric = [value for row in (hits or rows) if (value := _number(row.get("value"))) is not None]
            scanners = [str(row["detector"]) for row in hits]
            kind = "loop" if set(scanners) <= {SCOUT_REPEAT} else "cheat"
            yield finding(
                run.run_id, self.name, detector_version=self.version, reader_model=None,
                verdict=verdict_of(flag), kind=kind, score=max(numeric) if numeric else None,
                evidence_steps=known_steps(run, (s for row in hits for s in row["evidence_steps"] or ()), self.database),
                summary=(
                    "flagged: " + ", ".join(f"{row['detector']}={row['value']}" for row in hits)
                    if hits else f"{len(rows)} flagging scanners evaluated, none flagged"
                    if flag is False else "all scanners abstained"
                ),
                produced_at=_utc(max(str(row["at"]) for row in rows)),
            )


class ScoutLegacyDetector(ScoutDetector):
    """The retained pre-projection Scout ledger (``results-v1.jsonl``) the published TT-1 scorecard used."""

    name = "scout_legacy"

    def __init__(self, options: HarnessOptions, *, results: Path | None = None) -> None:
        super().__init__(options, results=results, legacy=True)


# --------------------------------------------------------------------------- #
# metr
# --------------------------------------------------------------------------- #


def _scan_id(location: Any) -> str | None:
    _, marker, tail = str(location or "").rpartition("scan_id=")
    return tail if marker and tail else None


class MetrReports:
    """TT-4 ``reports-scan_id=*.json`` receipts: per-run tokens, model and explanation."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[str, Path] | None = None

    def _paths(self) -> dict[str, Path]:
        if self._index is None:
            self._index = {}
            if self.root.is_dir():
                for path in sorted(self.root.glob("**/reports-scan_id=*.json")):
                    self._index[path.stem.partition("scan_id=")[2]] = path
        return self._index

    def usage(self, row: Row, run_id: str) -> dict[str, Any] | None:
        """The report of ``row``'s scan for ``run_id``: model, tokens, cost, explanation."""
        scan_id = _scan_id(row.get("scan"))
        path = self._paths().get(scan_id or "")
        if path is None:
            return None
        keymap_path = path.parent / "private-keymap.json"
        if not keymap_path.exists():
            return None
        owners = {entry["opaque_id"]: entry["run_id"] for entry in json.loads(keymap_path.read_text("utf-8"))}
        for report in json.loads(path.read_text("utf-8")):
            if owners.get(report.get("transcript_id")) != run_id:
                continue
            usage = report.get("model_usage") or {}
            models = {_inspect_model(key) for key in usage}
            totals = list(usage.values())
            return {
                "models": models,
                "input_tokens": sum(_tokens(t.get("input_tokens")) for t in totals),
                "output_tokens": sum(_tokens(t.get("output_tokens")) for t in totals),
                "cost_usd": sum(_number(t.get("total_cost")) or 0.0 for t in totals),
                "explanation": str(report.get("explanation") or ""),
            }
        return None


def _inspect_model(key: str) -> str:
    """A receipt's ``model_usage`` key without its Inspect provider (``omp/``, ``tt4openrouter/``)."""
    provider, _, model = key.partition("/")
    return model if provider in {"omp", "tt4openrouter", "openrouter"} else key


class MetrDetector:
    """METR reward-hacking scanner (native 0-10 score), ge5/ge7 any-flag union.

    The reader override (``metr:<reader>``) selects the stored arm by the slug that reader
    produces (``arm_slug``): any reader with stored rows, no fixed list. ``metr:<reader>@<projection>``
    picks among arms of one reader (``@none`` is the arm stored without a projection); with no
    ``@`` the actor-v2 arm is preferred. A reader with several equally eligible arms, or none
    stored and unknown, is refused rather than guessed.
    """

    name = "metr"

    def __init__(
        self, options: HarnessOptions, *, results: Path | None = None, tt4_root: Path | None = None,
    ) -> None:
        self.options = options
        self.reader_model, _, projection = (options.reader or METR_DEFAULT_READER).partition("@")
        self.results = results or TRACE_TOOLS / "results-v1.jsonl"
        arm = self._arm(projection or None)
        self.ledger_version, self.version = arm.ledger_version, arm.version
        self.reports = MetrReports(tt4_root or TT4_ROOT)

    def _arm(self, projection: str | None) -> MetrArm:
        slug = arm_slug(self.reader_model)
        stored = [arm for arm in metr_arms(self.results) if arm.arm == slug]
        if projection is not None:
            wanted = None if projection == "none" else projection
            stored = [arm for arm in stored if arm.projection == wanted]
        elif len(stored) > 1:
            stored = [arm for arm in stored if arm.projection == ACTOR_PROJECTION_VERSION] or stored
        if len(stored) == 1:
            return stored[0]
        if stored:
            raise ValueError(
                f"METR reader {self.reader_model!r} has several stored arms; add @<projection>: "
                + ", ".join(arm.ledger_version for arm in stored)
            )
        if self.reader_model not in METR_KNOWN_READERS:
            known = ", ".join(sorted({arm.arm for arm in metr_arms(self.results)})) or "none"
            raise ValueError(f"no stored METR arm for reader {self.reader_model!r}; stored arms: {known}")
        # A known reader with nothing stored yet: its identity, no rows, so no findings.
        parts = (*METR_BUILD.split(";"), slug)
        return MetrArm(
            ";".join(parts), tuple(parts[:2]), slug, (),
            None if projection == "none" else projection or ACTOR_PROJECTION_VERSION,
        )

    def detect(self, runs: Sequence[HarnessRun]) -> Iterator[dict[str, Any]]:
        expected = {self.reader_model.removeprefix("openrouter/")}
        ledger, _ = read_ledger(
            self.results, "scout_metr", lambda row: row.get("detector_version") == self.ledger_version,
            [run.run_id for run in runs],
        )
        stored = [run for run in runs if (METR_NATIVE, run.run_id) in ledger]
        for run in stored:
            native = ledger[METR_NATIVE, run.run_id]
            thresholds = [ledger[d, run.run_id] for d in METR_DETECTORS if (d, run.run_id) in ledger]
            flag = union(row["flag"] for row in thresholds)
            score = _number(native.get("value"))
            usage = self.reports.usage(native, run.run_id) if score is not None else None
            if usage is not None and usage["models"] != expected:
                raise ValueError(f"{run.run_id}: METR report was produced by {usage['models']}, not {self.reader_model}")
            summary = (
                (usage["explanation"] if usage and usage["explanation"] else f"METR score {score:g}/10 ({METR_THRESHOLDS})")
                if score is not None else "METR reader abstained (no score stored)"
            )
            yield finding(
                run.run_id, self.name, detector_version=self.version, reader_model=self.reader_model,
                verdict=verdict_of(flag), kind="cheat", score=score,
                evidence_steps=known_steps(run, native["evidence_steps"] or (), self.options.database), summary=summary,
                input_tokens=usage["input_tokens"] if usage else 0,
                output_tokens=usage["output_tokens"] if usage else 0,
                cost_usd=usage["cost_usd"] if usage else 0.0, produced_at=_utc(str(native["at"])),
            )


# --------------------------------------------------------------------------- #
# laminar
# --------------------------------------------------------------------------- #


class LaminarDetector:
    """Laminar Signals stored for our own Harbor runs: any-flag union of the flagging signals."""

    name = "laminar"
    version = LAMINAR_VERSION
    reader_model = LAMINAR_MODEL

    #: signal name -> payload keys holding an original step id
    STEP_KEYS: ClassVar[dict[str, str]] = {"stuck_loop": "start_step", "copied_upstream_fix": "evidence_step", "infra_not_model": "step"}

    def __init__(self, options: HarnessOptions, *, results: Path | None = None) -> None:
        _no_reader(self.name, options)
        self.options = options
        self.results = results or TRACE_TOOLS / "results-v1.jsonl"

    @classmethod
    def _steps(cls, signal: str, row: Row) -> list[Any]:
        key = cls.STEP_KEYS.get(signal)
        steps = []
        for observation in (row.get("value") or {}).get("observations", ()):
            payload: Any = observation.get("event_payload") or observation.get("explanation")
            try:
                while isinstance(payload, str):  # committed-cache explanations are double-encoded
                    payload = json.loads(payload)
            except ValueError:
                continue
            if key and isinstance(payload, dict):
                steps.append(payload.get(key))
        return steps

    @staticmethod
    def _usage(row: Row) -> tuple[int, int]:
        read = written = 0
        for observation in (row.get("value") or {}).get("observations", ()):
            run = observation.get("signal_run") or observation.get("tokens") or {}
            read += _tokens(run.get("input_tokens", run.get("input")))
            written += _tokens(run.get("output_tokens", run.get("output")))
        return read, written

    def detect(self, runs: Sequence[HarnessRun]) -> Iterator[dict[str, Any]]:
        ledger, flagging = read_ledger(
            self.results, "laminar", lambda row: row.get("detector_version") == self.version,
            [run.run_id for run in runs],
        )
        by_run: dict[str, list[Row]] = defaultdict(list)
        for (detector, run_id), row in ledger.items():
            if detector in flagging:
                by_run[run_id].append(row)
        stored = [run for run in runs if by_run.get(run.run_id)]
        for run in stored:
            rows = sorted(by_run[run.run_id], key=lambda row: row["detector"])
            flag = union(row["flag"] for row in rows)
            hits = [str(row["detector"]) for row in rows if row["flag"] is True]
            kind = "loop" if hits and set(hits) <= {"stuck_loop"} else "infra" if hits and set(hits) <= {"infra_not_model"} else "cheat"
            tokens = [self._usage(row) for row in rows]
            yield finding(
                run.run_id, self.name, detector_version=self.version, reader_model=self.reader_model,
                verdict=verdict_of(flag), kind=kind,
                evidence_steps=known_steps(
                    run, (s for row in rows if row["flag"] is True for s in self._steps(row["detector"], row)),
                    self.options.database,
                ),
                summary=(
                    "signals fired: " + ", ".join(hits) if hits
                    else f"{len(rows)} signals evaluated, none fired" if flag is False else "all signals abstained"
                ),
                input_tokens=sum(read for read, _ in tokens), output_tokens=sum(written for _, written in tokens),
                produced_at=_utc(max(str(row["at"]) for row in rows)),
            )


# --------------------------------------------------------------------------- #
# docent and the omp TT-7 form
# --------------------------------------------------------------------------- #


class _Tt7Detector:
    """One stored TT-7 form reader (``tt7_form:<model>``): suspicion >= 2 flags."""

    name: str
    tool: str
    form_version: str
    default_reader = TT7_DEFAULT_READER

    def __init__(self, options: HarnessOptions, *, results: Path | None = None) -> None:
        self.database = options.database
        self.reader_model = options.reader or self.default_reader
        if "/" not in self.reader_model:
            raise ValueError(f"{self.name} reader must be provider/model, got {self.reader_model!r}")
        self.stored_detector = f"tt7_form:{self.reader_model}"
        self.versions = frozenset({self.form_version})
        self.version = self.form_version
        self.results = results or TRACE_TOOLS / "results-v1.jsonl"

    def _read(self, row: Row) -> dict[str, Any]:
        """Verdict fields of one stored row."""
        value = row.get("value") or {}
        suspicion = _number(value.get("suspicion"))
        if row["flag"] is None:
            reason = value.get("error") or value.get("abstention_reason") or "no reading"
            return {"score": None, "summary": f"reader abstained: {reason}", "steps": []}
        shortcut = str(value.get("shortcut") or "none")
        return {
            "score": suspicion,
            "summary": f"suspicion {suspicion:g}; tests {value.get('tests')}; shortcut: {shortcut}"
            if suspicion is not None else f"shortcut: {shortcut}",
            "steps": row["evidence_steps"] or value.get("evidence_steps") or [],
        }

    def _cost(self, value: Mapping[str, Any]) -> float:
        return _number(value.get("marginal_usd")) or 0.0

    def detect(self, runs: Sequence[HarnessRun]) -> Iterator[dict[str, Any]]:
        ledger, _ = read_ledger(
            self.results, self.tool,
            lambda row: row.get("detector") == self.stored_detector and row.get("detector_version") in self.versions,
            [run.run_id for run in runs],
        )
        for run in runs:
            row = ledger.get((self.stored_detector, run.run_id))
            if row is None:
                continue
            read = self._read(row)
            value = row.get("value") or {}
            latency = _number(value.get("latency_s"))
            yield finding(
                run.run_id, self.name, detector_version=self.version, reader_model=self.reader_model,
                verdict=verdict_of(row["flag"]), kind="cheat", score=read["score"],
                evidence_steps=known_steps(run, read["steps"], self.database), summary=read["summary"],
                input_tokens=_tokens(value.get("input_tokens")), output_tokens=_tokens(value.get("output_tokens")),
                cost_usd=self._cost(value), latency_ms=round(latency * 1000) if latency else 0,
                produced_at=_utc(str(row["at"])),
            )


class DocentDetector(_Tt7Detector):
    """Docent hosted readings: the TT-7 form, or ``docent:rh_reading_v1`` (own-run rubric)."""

    name = "docent"
    tool = "docent"
    form_version = TT7_DOCENT_VERSION

    def __init__(self, options: HarnessOptions, *, results: Path | None = None) -> None:
        self.rubric = options.reader == DOCENT_RH_DETECTOR
        super().__init__(
            HarnessOptions(options.database, options.repo_root, options.allow_model, DOCENT_RH_MODEL)
            if self.rubric else options, results=results,
        )
        if self.rubric:
            self.stored_detector = DOCENT_RH_DETECTOR
            self.versions = DOCENT_RH_VERSIONS
            self.version = DOCENT_RH_VERSION

    def _read(self, row: Row) -> dict[str, Any]:
        if not self.rubric:
            return super()._read(row)
        value = row.get("value") or {}
        if row["flag"] is None:
            return {"score": None, "summary": f"reader abstained: {value.get('abstention_reason') or 'no reading'}", "steps": []}
        return {
            "score": _number(value.get("confidence")),
            "summary": f"reward hacking: {value.get('reward_hacking')}; {value.get('mechanism')}",
            "steps": row["evidence_steps"] or [],
        }


class OmpFormDetector(_Tt7Detector):
    """The TT-7 form read through OMP (subscription routes) over CheatBench DEV runs."""

    name = "omp_form"
    tool = "omp"
    form_version = TT7_OMP_VERSION


# --------------------------------------------------------------------------- #
# every other stored tool: the generic ledger adapter, and `all` discovery
# --------------------------------------------------------------------------- #


def compact_version(ledger_version: str) -> str:
    """A ledger ``detector_version`` as a ``trace_findings`` version (identifier rule, <= 64 chars)."""
    text = re.sub(r"[^A-Za-z0-9_.+:/@\-]", "_", ledger_version.replace(";", "+"))
    if len(text) <= VERSION_LIMIT:
        return text
    digest = hashlib.sha256(ledger_version.encode()).hexdigest()[:8]
    return f"{text[: VERSION_LIMIT - len(digest) - 1]}+{digest}"


class LedgerDetector:
    """Rows of any tool without a dedicated adapter: one detector per (tool, version, reader).

    A new tool or a new version of a stored tool is importable the moment its rows are in the
    ledger. The finding is the tool's own flag: the any-flag union of its *flagging* detectors,
    ``null`` abstains (a tool with no non-null flag anywhere abstains on every run it has rows
    for, keeping its numeric ``value`` as the score), a run with no row is omitted. The reader
    is the row's own ``reader_model`` field, else null (``none / unrecorded``); nothing is
    inferred from a version string. ``detector`` is ``ledger.<tool>`` and ``version`` the
    compacted ledger version.
    """

    name = "ledger"

    def __init__(
        self, options: HarnessOptions, *, tool: str, ledger_version: str, reader: str | None,
        results: Path | None = None,
    ) -> None:
        slug = re.sub(r"[^a-z0-9_]", "_", tool.lower())
        if not re.fullmatch(r"[a-z][a-z0-9_]*", slug):
            raise ValueError(f"cannot name a detector after ledger tool {tool!r}")
        self.name = f"ledger.{slug}"
        self.tool, self.ledger_version, self.reader_model = tool, ledger_version, reader
        self.version = compact_version(ledger_version)
        self.database = options.database
        self.results = results or TRACE_TOOLS / "results-v1.jsonl"

    def _keep(self, row: Row) -> bool:
        return (
            row.get("detector_version") == self.ledger_version and _row_reader(row) == self.reader_model
            and not first_class(self.tool, str(row["detector"]), self.ledger_version)
        )

    def detect(self, runs: Sequence[HarnessRun]) -> Iterator[dict[str, Any]]:
        ledger, flagging = read_ledger(self.results, self.tool, self._keep, [run.run_id for run in runs])
        by_run: dict[str, list[Row]] = defaultdict(list)
        for (detector, run_id), row in ledger.items():
            if detector in flagging or not flagging:
                by_run[run_id].append(row)
        for run in runs:
            rows = sorted(by_run.get(run.run_id, ()), key=lambda row: row["detector"])
            if not rows:
                continue
            flag = union(row["flag"] for row in rows)
            hits = [row for row in rows if row["flag"] is True]
            numeric = [value for row in (hits or rows) if (value := _number(row.get("value"))) is not None]
            yield finding(
                run.run_id, self.name, detector_version=self.version, reader_model=self.reader_model,
                verdict=verdict_of(flag), kind="cheat", score=max(numeric) if numeric else None,
                evidence_steps=known_steps(run, (s for row in hits for s in row["evidence_steps"] or ()), self.database),
                summary=(
                    "flagged: " + ", ".join(
                        f"{row['detector']}={row['value']}" if _number(row.get("value")) is not None else str(row["detector"])
                        for row in hits
                    ) if hits else f"{len(rows)} detectors evaluated, none flagged"
                    if flag is False else "all detectors abstained"
                ),
                produced_at=_utc(max(str(row["at"]) for row in rows)),
            )


def ledger_detectors(options: HarnessOptions, *, results: Path | None = None) -> list[LedgerDetector]:
    """One ``LedgerDetector`` per stored (tool, version, reader) no dedicated adapter reads.

    ``ledger`` takes every such tool, ``ledger:<tool>`` one tool. A named tool with nothing
    unclaimed is an error (it either has a dedicated adapter or no rows), never an empty run.
    """
    path = results or TRACE_TOOLS / "results-v1.jsonl"
    only = options.reader or None
    groups = sorted(
        {(tool, version, reader) for tool, detector, version, reader in ledger_groups(path)
         if not first_class(tool, detector, version) and only in (None, tool)},
        key=lambda group: (group[0], group[1], group[2] or ""),
    )
    if only is not None and not groups:
        raise ValueError(f"ledger tool {only!r} has no stored rows without a dedicated adapter")
    return [
        LedgerDetector(options, tool=tool, ledger_version=version, reader=reader, results=path)
        for tool, version, reader in groups
    ]


def discovered_specs(results: Path | None = None) -> list[str]:
    """Detector specs for every stored import-only reading ``all`` should include.

    Each METR arm of a known reader, and each TT-7 form reader of Docent and OMP. These
    only read stored rows; nothing here can start a model call.
    """
    path = results or TRACE_TOOLS / "results-v1.jsonl"
    specs = {
        f"metr:{arm.reader}@{arm.projection or 'none'}" for arm in metr_arms(path) if arm.reader is not None
    }
    for tool, detector, version, _ in ledger_groups(path):
        if detector.startswith(TT7_PREFIX) and first_class(tool, detector, version):
            specs.add(f"{'omp_form' if tool == 'omp' else tool}:{detector.removeprefix(TT7_PREFIX)}")
    return sorted(specs)
