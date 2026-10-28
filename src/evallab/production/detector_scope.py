"""Where each detector can be trusted: DEV and cross-test counts side by side.

``<data_root>/trace-tools/detector-scope-v1.json`` (``evallab.detector_scope/v1``, path
configurable with ``EVALLAB_DETECTOR_SCOPE``) holds the counts that no live scorecard
carries: the frozen cascade (v8) and Stage 1 alone on the Goodfire and CheatBench DEV
cohorts, and the HackBench natural-trajectory cross-test for the facets, the cascade and
Stage 1. It has counts, dates and the receipt each count came from, and nothing else: no run
id, trajectory, text, label or HackBench identifier (unknown fields are refused).

The Trace Tools page merges those cells with the facets, Scout and METR scanner rows the
scorecards already hold, and adds a Wilson interval to every cell. Nothing here opens a
database, calls a model or joins a label; ``from_*`` build the file offline from retained
receipts and ``write_scope`` writes it.

Two measures are never mixed: on the DEV cohorts a hit is a flagged run; on HackBench a hit
is the exact hack command named. HackBench holds only cheating rows, so it has no
false-alarm rate and the file cannot carry one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evallab.production.field_reliability import wilson

SCHEMA = "evallab.detector_scope/v1"
FILE = "detector-scope-v1.json"
DEFAULT_FILE = f"trace-tools/{FILE}"
HACKBENCH = "hackbench_natural"
DEV_COHORTS = ("goodfire_dev", "cheatbench_dev")
# Cohort key -> population key the scorecards use.
POPULATION_OF = {"goodfire_dev": "goodfire_cm9", "cheatbench_dev": "cheatbench_dev"}

_Count = Annotated[int, Field(ge=0)]
_Text = Annotated[str, Field(min_length=1, max_length=400, pattern=r"\S")]

LINES = (
    "Facets read the whole run after it ended, so they are post-hoc. The cascade reads a "
    "growing prefix and is prefix-causal: it could have alarmed while the run was still going.",
    "On Goodfire and CheatBench DEV a hit is a flagged run. On HackBench a hit is the exact hack "
    "command named. The hit definitions differ, so rates are not directly comparable across "
    "these corpora.",
    "HackBench holds only cheating rows, so it has no false-alarm rate. A high hit rate there "
    "says nothing about false alarms.",
    "CheatBench DEV was already examined during earlier tuning, so it is not held out. HackBench "
    "was never used for tuning.",
)


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class Cell(_Strict):
    detector: Literal["facets", "cascade", "stage1"]
    cohort: Literal["goodfire_dev", "cheatbench_dev", "hackbench_natural"]
    measure: Literal["run_flagged", "exact_command"]
    hits: _Count
    positives: Annotated[int, Field(gt=0)]
    negatives: Annotated[int, Field(gt=0)] | None = None
    false_alarms: _Count | None = None
    version: _Text | None = None
    route: _Text
    recorded_at: _Text
    date_basis: _Text
    source: _Text
    note: _Text | None = None

    @model_validator(mode="after")
    def validate_cell(self) -> Self:
        if self.hits > self.positives:
            raise ValueError("hits exceed the positive runs")
        if self.cohort == HACKBENCH:
            if self.measure != "exact_command":
                raise ValueError("a HackBench hit is the exact command named")
            if self.negatives is not None or self.false_alarms is not None:
                raise ValueError("HackBench has no clean rows, so it has no false-alarm count")
        else:
            if self.measure != "run_flagged":
                raise ValueError("a DEV hit is a flagged run")
            if self.negatives is None or self.false_alarms is None:
                raise ValueError("a DEV cell needs its clean-run counts")
            if self.false_alarms > self.negatives:
                raise ValueError("false alarms exceed the clean runs")
        if (
            self.detector == "cascade" and self.cohort != HACKBENCH
            and self.version != "window-cascade/v8"
        ):
            raise ValueError("the frozen DEV cascade row requires window-cascade/v8")
        if self.detector == "cascade" and self.cohort == HACKBENCH:
            if self.version not in {"window-cascade/v7", "window-cascade/v8"}:
                raise ValueError("the HackBench cascade result must record v7 or v8")
            if self.version == "window-cascade/v7" and not self.note:
                raise ValueError("a v7 HackBench result needs its v8 compatibility explanation")
        if self.detector == "facets" and self.cohort != HACKBENCH:
            raise ValueError("DEV facet counts belong to the live scorecard, not the scope receipt")
        if datetime.fromisoformat(self.recorded_at).tzinfo is None:
            raise ValueError("a recorded time must carry its timezone")
        return self


class Scope(_Strict):
    schema_: Literal["evallab.detector_scope/v1"] = Field(alias="schema")
    cells: Annotated[list[Cell], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        keys = [(cell.detector, cell.cohort) for cell in self.cells]
        if len(set(keys)) != len(keys):
            raise ValueError("a detector appears twice on one cohort")
        routes = {cell.route for cell in self.cells if cell.detector == "cascade"}
        if len(routes) > 1:
            raise ValueError("the single cascade route must agree across cohorts")
        return self


def parse_scope(document: Any) -> dict[tuple[str, str], Cell]:
    """The cells by (detector, cohort); raises ``ValidationError`` for anything else."""
    scope = Scope.model_validate(document)
    return {(cell.detector, cell.cohort): cell for cell in scope.cells}


# --- the served table ---------------------------------------------------------------------

# (tool key, name, how it reads, route when the scope file does not say)
ROWS: tuple[tuple[str, str, str, str | None], ...] = (
    ("facets", "Run Atlas facets", "whole run, after it ended (post-hoc)",
     "model reader of the whole run; the model varies by run"),
    ("cascade", "Frozen cascade (v8)", "growing prefix (prefix-causal)", None),
    ("stage1", "Stage 1 alone", "growing prefix (prefix-causal)", "none: fixed rules, no judge"),
    ("scout", "Scout deterministic scanners", "whole run, after it ended (post-hoc)",
     "none: code scanners"),
    ("scout_metr", "METR reward-hacking scanner", "whole run, after it ended (post-hoc)",
     "model scanner; the scorecard does not record which model"),
)
# Rows whose DEV counts live in the scorecards; the others come from the scope file.
SCORECARD_ROWS = frozenset({"facets", "scout", "scout_metr"})


def _absent(reason: str) -> dict[str, Any]:
    return {"state": "absent", "reason": reason}


def _interval(hits: int, total: int) -> list[float] | None:
    return wilson(hits, total) if total else None


def _dev_from_scorecard(
    entry: Mapping[str, Any] | None, recorded_at: str | None, source: str
) -> dict[str, Any]:
    if entry is None:
        return _absent("no retained scorecard row")
    read, clean_read = entry["positives_decided"], entry["negatives_decided"]
    if not read and not clean_read:
        return _absent("no runs read")
    return {
        "state": "measured",
        "measure": "run_flagged",
        "hits": entry["tp"],
        "positives": entry["positives"],
        "read": read,
        "hit_ci": _interval(entry["tp"], read),
        "false_alarms": entry["fp"],
        "negatives": entry["negatives"],
        "negatives_read": clean_read,
        "false_alarm_ci": _interval(entry["fp"], clean_read),
        "recorded_at": recorded_at,
        "date_basis": "scorecard file modified" if recorded_at else None,
        "source": source,
        "version": None,
        "note": None,
    }


def _from_cell(cell: Cell) -> dict[str, Any]:
    served: dict[str, Any] = {
        "state": "measured",
        "measure": cell.measure,
        "hits": cell.hits,
        "positives": cell.positives,
        "read": cell.positives,
        "hit_ci": _interval(cell.hits, cell.positives),
        "false_alarms": cell.false_alarms,
        "negatives": cell.negatives,
        "negatives_read": cell.negatives,
        "false_alarm_ci": None
        if cell.false_alarms is None or cell.negatives is None
        else _interval(cell.false_alarms, cell.negatives),
        "recorded_at": cell.recorded_at,
        "date_basis": cell.date_basis,
        "source": cell.source,
        "version": cell.version,
        "note": cell.note,
    }
    return served


def table(
    cells: Mapping[tuple[str, str], Cell] | None,
    scope_error: str | None,
    tools: Sequence[Mapping[str, Any]],
    dates: Mapping[str, str | None],
) -> dict[str, Any]:
    """The 'where each detector works' table from the scope cells and the scorecard tools.

    ``dates`` holds the modified date of each scorecard file (``tool_scorecard`` and
    ``atlas_scorecard``); a missing file gives no date, never a guessed one.
    """
    by_tool = {tool["tool"]: tool for tool in tools}
    missing = f"no detector scope retained ({scope_error or 'not retained'})"
    hackbench = [cell for (_, cohort), cell in (cells or {}).items() if cohort == HACKBENCH]
    sizes = {cell.positives for cell in hackbench}
    rows: list[dict[str, Any]] = []
    for key, name, reads, fixed_route in ROWS:
        route = fixed_route
        served: dict[str, dict[str, Any]] = {}
        for cohort in (*DEV_COHORTS, HACKBENCH):
            cell = (cells or {}).get((key, cohort))
            if cell is not None and route is None:
                route = cell.route
            if cohort == HACKBENCH:
                if cell is not None:
                    served[cohort] = _from_cell(cell)
                elif key in SCORECARD_ROWS and key != "facets":
                    served[cohort] = _absent("not run on HackBench")
                else:
                    served[cohort] = _absent(missing if cells is None else "no HackBench cell")
            elif key in SCORECARD_ROWS:
                tool = by_tool.get(key) or {}
                entry = (tool.get("populations") or {}).get(POPULATION_OF[cohort])
                source = "atlas scorecard" if key == "facets" else "tool scorecard"
                date = dates.get("atlas_scorecard" if key == "facets" else "tool_scorecard")
                served[cohort] = _dev_from_scorecard(entry, date, source)
            else:
                served[cohort] = _from_cell(cell) if cell is not None else _absent(
                    missing if cells is None else "no cell for this cohort"
                )
        rows.append({
            "tool": key, "name": name, "reads": reads,
            "route": route or "judge route not recorded",
            "cohorts": served,
        })
    return {
        "present": cells is not None,
        "error": scope_error,
        "hackbench_runs": sizes.pop() if len(sizes) == 1 else None,
        "lines": list(LINES),
        "rows": rows,
    }


# --- building the file from retained receipts -----------------------------------------------


def _read_json(path: Path, limit: int = 4 * 1024 * 1024) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} must be a regular file")
    if path.stat().st_size > limit:
        raise ValueError(f"{path.name} is larger than {limit} bytes")
    return json.loads(path.read_text(encoding="utf-8"))


def _timestamp(moment: str) -> str:
    parsed = datetime.fromisoformat(moment)
    if parsed.tzinfo is None:
        raise ValueError("a recorded time must carry its timezone")
    return parsed.astimezone(UTC).isoformat()


def _file_time(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def from_hackbench(directory: Path) -> list[dict[str, Any]]:
    """Natural-trajectory exact-command hits from the CM-11 receipt (counts only)."""
    union = _read_json(directory / "union-command.json")
    freeze = _read_json(directory / "freeze.json")
    if union.get("schema") != "cm11-union/v1" or union.get("layout") != "command":
        raise ValueError("expected a cm11-union/v1 receipt in the command layout")
    if (
        freeze.get("schema") != "cm11-freeze/v1"
        or freeze.get("cascade_version") != "window-cascade/v7"
        or freeze.get("git_head") != union.get("freeze_git_head")
        or not isinstance(freeze.get("git_head"), str) or not freeze["git_head"]
    ):
        raise ValueError("the HackBench score must match its v7 CM-11 freeze")
    if union["submission_tamper"]["hackbench"]["traces_with_card"] != 0:
        raise ValueError("submission_tamper fired on HackBench, so v7 counts are not v8's")
    arms = union["arms"]
    sizes = {arms[name]["natural"]["n"] for name in ("stage1", "cascade", "facets")}
    if len(sizes) != 1:
        raise ValueError("the arms scored different natural-trajectory sets")
    source = f"{directory.name}/union-command.json"
    common = {
        "cohort": HACKBENCH, "measure": "exact_command", "positives": sizes.pop(),
        "recorded_at": _timestamp(freeze["frozen_at_utc"]),
        "date_basis": "cascade freeze", "source": source,
    }
    return [
        {**common, "detector": "facets", "hits": arms["facets"]["natural"]["exact_hit"],
         "route": "Atlas readers of the whole run; the reader model is not in the receipt",
         "note": "whole-run readers, so post-hoc localization rather than a prefix monitor"},
        {**common, "detector": "cascade", "hits": arms["cascade"]["natural"]["exact_hit"],
         "version": freeze["cascade_version"], "route": union["judge"],
         "note": "scored with v7; v8 differs only in judging submission_tamper, which "
                 "produced no card on any HackBench trajectory"},
        {**common, "detector": "stage1", "hits": arms["stage1"]["natural"]["exact_hit"],
         "version": freeze["cascade_version"], "route": "none: fixed rules, no judge"},
    ]


def from_cascade_measurement(path: Path) -> list[dict[str, Any]]:
    """Frozen cascade (v8) on the Goodfire and CheatBench DEV cohorts (CM-11b counts)."""
    measurement = _read_json(path, 8 * 1024 * 1024)
    if (
        measurement.get("schema_version") != "cm11b-tamper-demotion/v1"
        or measurement.get("versions") != {
            "before": "window-cascade/v7", "after": "window-cascade/v8",
        }
        or measurement.get("arm") != "pf_r"
    ):
        raise ValueError("expected a CM-11b frozen v7-to-v8 measurement on arm pf_r")
    scope = measurement["scope"]
    if (
        scope["dev_only"] is not True or scope["hackbench_used_for_tuning"] is not False
        or scope.get("strict_cross_key_guard") is not True
    ):
        raise ValueError("the measurement must be DEV-only with the strict cross-key guard and no HackBench tuning")
    cells = []
    for cohort, key in (("goodfire_dev", "goodfire"), ("cheatbench_dev", "cheatbench")):
        after = measurement["comparison"][key]["after"]
        cells.append({
            "detector": "cascade", "cohort": cohort, "measure": "run_flagged",
            "hits": after["recall"]["count"], "positives": after["recall"]["denominator"],
            "false_alarms": after["false_alarms"]["count"],
            "negatives": after["false_alarms"]["denominator"],
            "version": measurement["versions"]["after"], "route": measurement["model_route"],
            "recorded_at": _timestamp(measurement["created_at_utc"]),
            "date_basis": "cascade measurement",
            "source": f"{path.parent.name}/{path.name}",
        })
    return cells


def from_stage1_scores(goodfire: Path, cheatbench: Path) -> list[dict[str, Any]]:
    """Count Stage 1 cards in the retained DEV scores, without exporting per-run fields."""
    cells = []
    for cohort, path in (("goodfire_dev", goodfire), ("cheatbench_dev", cheatbench)):
        document = _read_json(path, 8 * 1024 * 1024)
        traces = document["traces"]
        if document["summary"].get("corpus") != (
            "goodfire" if cohort == "goodfire_dev" else "cheatbench"
        ):
            raise ValueError(f"{path.name}: the receipt is for a different corpus")
        if any(
            type(row["positive"]) is not bool or type(row["stage1_any"]) is not bool
            for row in traces
        ):
            raise ValueError(f"{path.name}: Stage 1 decisions and positive flags must be booleans")
        positive = [row for row in traces if row["positive"]]
        clean = [row for row in traces if not row["positive"] and row["stratum"] != "considered_only"]
        summary = document["summary"]
        if (len(positive), len(clean)) != (summary["positives"], summary["negatives"]):
            raise ValueError(f"{path.name}: scored rows disagree with their own summary")
        cells.append({
            "detector": "stage1", "cohort": cohort, "measure": "run_flagged",
            "hits": sum(bool(row["stage1_any"]) for row in positive), "positives": len(positive),
            "false_alarms": sum(bool(row["stage1_any"]) for row in clean), "negatives": len(clean),
            "note": "CM-9 retained Stage 1 any-card counts; the score file does not record version or arm",
            "route": "none: fixed rules, no judge",
            "recorded_at": _file_time(path), "date_basis": "receipt file modified",
            "source": f"{path.parent.name}/{path.name}",
        })
    return cells


def build_scope(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    document = {"schema": SCHEMA, "cells": list(cells)}
    parse_scope(document)
    return document


def write_scope(
    directory: Path, document: Mapping[str, Any], *, replace: bool = False
) -> dict[str, Any]:
    """Validate and atomically write the file; an existing different file needs ``replace``."""
    parse_scope(document)
    body = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / FILE
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"{FILE} must be a regular file")
        if target.read_bytes() != body and not replace:
            raise FileExistsError(f"{target} already differs; pass --replace to overwrite it")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".detector-scope-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {
        "path": str(target),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "cells": len(document["cells"]),
    }

