"""Read-only Harness page data: one combined evidence card per run and one combined scorecard.

Two retained files under ``<data_root>/trace-harness/`` feed it; nothing here runs a detector,
calls a model or writes:

* ``findings-v1.jsonl``: ``trace-findings/v1`` records (CONTRACT section 3b), one per line, written
  by ``evallab trace run`` and by every detector adapter, including the ``run_health.*`` ones.
  The newest record per (run, detector, detector version, reader model) wins, the same key the
  ``trace_findings`` table uses. A line that breaks the contract is counted by reason and never
  shown; no reason repeats the line.
* ``scorecard-v1.json``: ``evallab.trace_scorecard/v1``, the combined scorecard from
  ``evallab trace score``: per corpus and detector combination, the reader model, recall hits and
  false alarms at the fixed 3% and 5% false-alarm budgets, localization, cost per 1k runs and
  latency, as counts.

A run with no actor transcript is unknown, never clean: a recorded ``clear`` for such a run is
shown as ``unknown``. A detector that recorded nothing for a run is "no result", not a pass.
The card holds no outcome, reward, model, dataset or label field.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evallab.production.atlas_view import read_regular, strict_loads
from evallab.production.tool_scorecard_view import iter_lines, parse_moment

FINDINGS_FILE = "trace-harness/findings-v1.jsonl"
SCORECARD_FILE = "trace-harness/scorecard-v1.json"
MAX_FINDINGS_BYTES = 256 * 1024 * 1024
MAX_SCORECARD_BYTES = 1024 * 1024
OVERVIEW_SCHEMA = "evallab.harness/v1"
CARD_SCHEMA = "evallab.harness_card/v1"
SCORECARD_SCHEMA = "evallab.trace_scorecard/v1"
VERDICTS = ("alarm", "clear", "abstain", "unknown")
KINDS = ("cheat", "loop", "runaway", "latency", "infra", "other")
MAX_EVIDENCE_STEPS = 20
MAX_SUMMARY_WORDS = 40
MAX_FLAGGED = 50
FAR_BUDGETS = (0.03, 0.05)

_DETECTOR = re.compile(r"^[a-z][a-z0-9_]{0,30}(\.[a-z][a-z0-9_]{0,30})?$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,79}$")
_READER = re.compile(r"^[A-Za-z0-9._-]{1,40}/[A-Za-z0-9][A-Za-z0-9._:+-]{0,79}$")
_KEY = tuple[str, str, str]


class Refused(ValueError):
    """A record the contract refuses; the message is a fixed reason and never repeats the record."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(row: Mapping[str, Any], name: str, *, integer: bool) -> int | float:
    value = row.get(name)
    if value is None:
        return 0
    if integer and not _is_int(value):
        raise Refused("a number that is not a non-negative finite value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise Refused("a number that is not a non-negative finite value")
    return value


def parse_finding(row: Any) -> dict[str, Any]:
    """One validated ``trace-findings/v1`` record reduced to the named fields."""
    if not isinstance(row, Mapping):
        raise Refused("record is not an object")
    run, detector, version = row.get("run_id"), row.get("detector"), row.get("detector_version")
    if not (isinstance(run, str) and 0 < len(run) <= 200):
        raise Refused("missing run id")
    if not (isinstance(detector, str) and _DETECTOR.fullmatch(detector)):
        raise Refused("detector name is not an identifier")
    if not (isinstance(version, str) and _VERSION.fullmatch(version)):
        raise Refused("detector version is not an identifier")
    reader = row.get("reader_model")
    if reader is not None and not (isinstance(reader, str) and _READER.fullmatch(reader)):
        raise Refused("reader model is not provider/id")
    verdict, kind = row.get("verdict"), row.get("kind")
    if verdict not in VERDICTS or kind not in KINDS:
        raise Refused("verdict or kind outside the contract")
    steps = row.get("evidence_steps", [])
    if not (
        isinstance(steps, list)
        and len(steps) <= MAX_EVIDENCE_STEPS
        and all(_is_int(step) and step >= 0 for step in steps)
    ):
        raise Refused("evidence steps are not up to 20 step indices")
    summary = row.get("summary")
    if summary is not None and not (
        isinstance(summary, str) and len(summary.split()) <= MAX_SUMMARY_WORDS and len(summary) <= 600
    ):
        raise Refused("summary is longer than 40 words")
    produced_at = row.get("produced_at")
    try:
        moment = parse_moment(produced_at) if isinstance(produced_at, str) else None
    except ValueError:
        moment = None
    if moment is None:
        raise Refused("produced_at names no time zone")
    return {
        "run_id": run,
        "detector": detector,
        "detector_version": version,
        "reader_model": reader,
        "verdict": verdict,
        "kind": kind,
        "score": _number(row, "score", integer=False),
        "evidence_steps": sorted(set(steps)),
        "summary": summary.strip() if isinstance(summary, str) and summary.strip() else None,
        "input_tokens": _number(row, "input_tokens", integer=True),
        "output_tokens": _number(row, "output_tokens", integer=True),
        "cost_usd": _number(row, "cost_usd", integer=False),
        "latency_ms": _number(row, "latency_ms", integer=True),
        "produced_at": produced_at,
        "_moment": moment,
    }


def load_findings(path: Path) -> dict[str, Any]:
    """Newest record per (run, detector, version, reader); refusals counted by reason."""
    base: dict[str, Any] = {"present": False, "error": None, "by_run": {}, "refused": {}, "lines": 0}
    by_run: dict[str, dict[_KEY, dict[str, Any]]] = defaultdict(dict)
    refused: Counter[str] = Counter()
    lines = 0
    try:
        if path.lstat().st_size > MAX_FINDINGS_BYTES:
            return base | {"error": "findings file is larger than the cloud loader accepts"}
        for raw in iter_lines(path):
            if not raw.strip():
                continue
            lines += 1
            try:
                record = parse_finding(strict_loads(raw.decode("utf-8")))
            except Refused as reason:
                refused[str(reason)] += 1
                continue
            except (ValueError, UnicodeDecodeError, RecursionError):
                refused["line is not one JSON object"] += 1
                continue
            key = (record["detector"], record["detector_version"], record["reader_model"] or "")
            kept = by_run[record["run_id"]]
            if key not in kept or record["_moment"] >= kept[key]["_moment"]:
                kept[key] = record
    except FileNotFoundError:
        return base | {"error": "not retained"}
    except (OSError, ValueError):
        return base | {"error": "unreadable findings file"}
    return base | {"present": True, "by_run": dict(by_run), "refused": dict(refused), "lines": lines}


# --- the combined scorecard --------------------------------------------------------------------

_Count = Annotated[int, Field(ge=0)]
_Corpus = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,40}$")]
_Name = Annotated[str, Field(pattern=_DETECTOR.pattern)]
_ScoreReader = Annotated[
    str, Field(pattern=r"^(none|[A-Za-z0-9._-]{1,40}/[A-Za-z0-9][A-Za-z0-9._:+-]{0,79})$")
]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class Budget(_Strict):
    far_budget: float
    hits: _Count
    false_alarms: _Count

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        if self.far_budget not in FAR_BUDGETS:
            raise ValueError("only the fixed 3% and 5% false-alarm budgets are reported")
        return self


class Localization(_Strict):
    hits: _Count
    scored: _Count

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.hits > self.scored:
            raise ValueError("localization hits cannot exceed scored runs")
        return self


class Published(_Strict):
    """One published single-detector number the scorecard re-measured, and whether it matched."""

    check: Annotated[str, Field(pattern=r"^[A-Za-z0-9 ._:%/,()-]{1,80}$")]
    reproduced: bool


class Row(_Strict):
    corpus: _Corpus
    detectors: Annotated[list[_Name], Field(min_length=1, max_length=8)]
    rule: Literal["single", "any", "majority", "all", "stacked"]
    reader_model: _ScoreReader
    positives: _Count
    negatives: _Count
    budgets: Annotated[list[Budget], Field(min_length=1, max_length=2)]
    localization: Localization | None = None
    cost_usd_per_1k_runs: Annotated[float, Field(ge=0)] | None = None
    latency_ms_median: _Count | None = None
    published: Published | None = None

    @model_validator(mode="after")
    def validate_row(self) -> Self:
        if (self.rule == "single") != (len(self.detectors) == 1):
            raise ValueError("a single rule names one detector and a combination names several")
        if len(set(self.detectors)) != len(self.detectors):
            raise ValueError("a detector appears twice in one combination")
        if len({budget.far_budget for budget in self.budgets}) != len(self.budgets):
            raise ValueError("a false-alarm budget appears twice")
        for budget in self.budgets:
            if budget.hits > self.positives or budget.false_alarms > self.negatives:
                raise ValueError("hits cannot exceed positives, nor false alarms negatives")
        if self.published is not None and self.rule != "single":
            raise ValueError("only a single detector is checked against a published number")
        return self


class Scorecard(_Strict):
    schema_: Literal["evallab.trace_scorecard/v1"] = Field(alias="schema")
    generated_at: str
    rows: Annotated[list[Row], Field(min_length=1, max_length=400)]

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, populate_by_name=True)

    @model_validator(mode="after")
    def validate_scorecard(self) -> Self:
        parse_moment(self.generated_at)
        identities = [(row.corpus, tuple(row.detectors), row.rule, row.reader_model) for row in self.rows]
        if len(set(identities)) != len(identities):
            raise ValueError("two rows share a corpus, combination and reader model")
        return self


def _scorecard_rows(document: Scorecard) -> list[dict[str, Any]]:
    failed = {
        (row.corpus, row.detectors[0])
        for row in document.rows
        if row.published is not None and not row.published.reproduced
    }
    served = []
    for row in document.rows:
        unchecked = sorted(name for name in row.detectors if (row.corpus, name) in failed) if row.rule != "single" else []
        served.append(
            {
                "corpus": row.corpus,
                "detectors": row.detectors,
                "rule": row.rule,
                "reader_model": row.reader_model,
                "positives": row.positives,
                "negatives": row.negatives,
                "budgets": [budget.model_dump() for budget in sorted(row.budgets, key=lambda b: b.far_budget)],
                "localization": None if row.localization is None else row.localization.model_dump(),
                "cost_usd_per_1k_runs": row.cost_usd_per_1k_runs,
                "latency_ms_median": row.latency_ms_median,
                "published": None if row.published is None else row.published.model_dump(),
                "unreproduced_parts": unchecked,
            }
        )
    served.sort(key=lambda item: (item["corpus"], item["rule"] != "single", item["detectors"], item["reader_model"]))
    return served


def load_scorecard(path: Path) -> dict[str, Any]:
    """The served combined scorecard, or why there is none; errors never repeat file contents."""
    base: dict[str, Any] = {"present": False, "error": None, "generated_at": None, "rows": []}
    try:
        raw = read_regular(path.parent, path.name, MAX_SCORECARD_BYTES)
        if raw is None:
            return base | {"error": "not retained"}
        document = Scorecard.model_validate(strict_loads(raw.decode("utf-8")))
    except ValidationError:
        return base | {"error": "invalid counts-only scorecard"}
    except (OSError, ValueError):
        return base | {"error": "unreadable scorecard"}
    return base | {"present": True, "generated_at": document.generated_at, "rows": _scorecard_rows(document)}


# --- the per-run card and the overview ---------------------------------------------------------


def _family(detector: str) -> str:
    return "run_health" if detector.startswith("run_health.") else "trace"


def _public(record: Mapping[str, Any], steps: Collection[int], actor_inputs: bool) -> dict[str, Any]:
    verdict = record["verdict"]
    demoted = verdict == "clear" and not actor_inputs
    cited = [step for step in record["evidence_steps"] if step in steps]
    return {
        "detector": record["detector"],
        "family": _family(record["detector"]),
        "detector_version": record["detector_version"],
        "reader_model": record["reader_model"],
        "verdict": "unknown" if demoted else verdict,
        "recorded_verdict": verdict if demoted else None,
        "kind": record["kind"],
        "score": record["score"],
        "evidence_steps": cited,
        "hidden_steps": len(record["evidence_steps"]) - len(cited),
        "summary": record["summary"],
        "input_tokens": record["input_tokens"],
        "output_tokens": record["output_tokens"],
        "cost_usd": record["cost_usd"],
        "latency_ms": record["latency_ms"],
        "produced_at": record["produced_at"],
    }


def _tally(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(result["verdict"] for result in results)
    return {name: counts.get(name, 0) for name in VERDICTS} | {"results": len(results)}


def build_card(
    run: Mapping[str, Any],
    records: Mapping[_KEY, Mapping[str, Any]],
    *,
    steps: Collection[int],
    actor_inputs: bool,
    facet: Mapping[str, Any] | None,
    known: Collection[tuple[str, str, str]],
) -> dict[str, Any]:
    """Every detector's newest result for one admitted DEV run, grouped, with nothing merged away.

    ``known`` is every (detector, version, reader) the findings file holds for any run, so the card
    can name the detectors that recorded nothing here.
    """
    results = sorted(
        (_public(record, steps, actor_inputs) for record in records.values()),
        key=lambda item: (item["family"], item["detector"], item["detector_version"], item["reader_model"] or ""),
    )
    missing = sorted(
        {f"{detector} {version}" + (f" ({reader})" if reader else "") for detector, version, reader in known}
        - {
            f"{item['detector']} {item['detector_version']}" + (f" ({item['reader_model']})" if item["reader_model"] else "")
            for item in results
        }
    )
    groups = [
        {"family": family, "results": [item for item in results if item["family"] == family]}
        for family in ("trace", "run_health")
    ]
    tally = _tally(results)
    if not results:
        headline = "No detector has recorded a result for this run. That is not a clean result."
    else:
        headline = (
            f"{tally['alarm']} of {tally['results']} detector results alarm; {tally['clear']} clear, "
            f"{tally['abstain']} abstained, {tally['unknown']} unknown."
        )
    lead = None
    if facet is not None:
        lead = {
            name: facet.get(name)
            for name in (
                "goal", "approach", "suspicion", "shortcut", "tests", "eval_awareness",
                "grader_or_harness_touched", "answer_material_accessed", "success_claim_without_evidence",
            )
        }
    return {
        "schema": CARD_SCHEMA,
        "run": {**run, "actor_inputs_available": actor_inputs},
        "headline": headline,
        "tally": tally,
        "groups": groups,
        "no_result": missing,
        "facet_lead": lead,
        "totals": {
            "input_tokens": sum(item["input_tokens"] for item in results),
            "output_tokens": sum(item["output_tokens"] for item in results),
            "cost_usd": sum(item["cost_usd"] for item in results),
        },
        "notice": (
            "One row per detector, version and reader model: the newest recorded result. Nothing is merged "
            "into a verdict. A detector that recorded nothing is not a clean pass, and a run with no actor "
            "transcript is unknown, never clean."
            + ("" if actor_inputs else " This run has no actor transcript.")
        ),
    }


def build_overview(
    findings: Mapping[str, Any],
    scorecard: Mapping[str, Any],
    admitted: Mapping[str, str],
    actor_runs: Collection[str],
) -> dict[str, Any]:
    """Counts per detector over admitted DEV runs, the runs with an alarm, and the scorecard.

    ``admitted`` maps run id to task id for the findings' runs the registry admits; findings for any
    other run are only counted.
    """
    by_run: Mapping[str, Mapping[_KEY, Mapping[str, Any]]] = findings.get("by_run", {})
    detectors: dict[_KEY, Counter[str]] = defaultdict(Counter)
    spend: dict[_KEY, dict[str, float]] = defaultdict(lambda: {"cost_usd": 0.0, "runs": 0, "latency_ms": 0})
    flagged: list[dict[str, Any]] = []
    for run, task in admitted.items():
        records = by_run.get(run, {})
        alarms = []
        for key, record in records.items():
            verdict = "unknown" if record["verdict"] == "clear" and run not in actor_runs else record["verdict"]
            detectors[key][verdict] += 1
            spend[key]["cost_usd"] += record["cost_usd"]
            spend[key]["latency_ms"] += record["latency_ms"]
            spend[key]["runs"] += 1
            if verdict == "alarm":
                alarms.append(record["detector"])
        if alarms:
            flagged.append({"run_id": run, "task_id": task, "alarms": sorted(set(alarms)), "results": len(records)})
    flagged.sort(key=lambda item: (-len(item["alarms"]), item["run_id"]))
    rows = [
        {
            "detector": key[0],
            "family": _family(key[0]),
            "detector_version": key[1],
            "reader_model": key[2] or None,
            **{name: counts.get(name, 0) for name in VERDICTS},
            "runs": spend[key]["runs"],
            "cost_usd": round(spend[key]["cost_usd"], 6),
            "latency_ms_mean": round(spend[key]["latency_ms"] / spend[key]["runs"]) if spend[key]["runs"] else None,
        }
        for key, counts in sorted(detectors.items())
    ]
    return {
        "schema": OVERVIEW_SCHEMA,
        "findings": {
            "present": bool(findings.get("present")),
            "error": findings.get("error"),
            "lines": findings.get("lines", 0),
            "refused": dict(findings.get("refused", {})),
            "runs_recorded": len(by_run),
            "runs_admitted": len(admitted),
            "runs_outside": len(by_run) - len(admitted),
            "detectors": rows,
            "alarmed_runs": len(flagged),
            "flagged": flagged[:MAX_FLAGGED],
            "flagged_limit": MAX_FLAGGED,
        },
        "scorecard": scorecard,
        "notice": (
            "Read-only. The cards and the scorecard come from retained files; nothing here runs a detector or "
            "calls a model. An alarm is a lead to read, not a verdict, and no result is not a clean pass."
        ),
    }
