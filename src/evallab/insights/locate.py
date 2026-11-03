"""Find where a claim's numbers live in an experiment's receipt files.

This is a search aid, not evidence: it proposes JSON pointers and recipe templates for
values equal to a claim's ``k``, ``n`` or printed ``value``. The caller still reads the
source, chooses the recipe that matches the claim's definition, and tests it with
``./q recipe``. The search is bounded in files, nodes, rows, hits and wall-clock time.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.insights.models import Recipe
from evallab.insights.packs import DATA_SUFFIXES, ReceiptFile, is_aggregate_name, scan_receipts
from evallab.insights.recompute import RecipeError, load_recipe_file
from evallab.insights.units import canonical_value, parse_quantity, printed_tolerance

MAX_FILES = 300
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_NODES = 300_000
MAX_ROWS = 50_000
MAX_FIELDS = 40
MAX_HITS = 20
MAX_FOUND = 400
_PER_FILE = 8
_BIG_ARRAY = 50
_MAX_CATEGORIES = 12
_DEPTH = 12
DEADLINE_SECONDS = 4.0


@dataclass(frozen=True)
class Targets:
    k: int | None = None
    n: int | None = None
    value: str | None = None
    unit: str | None = None
    readings: tuple[tuple[float, float], ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        if self.k is None and self.n is None and self.value is None:
            raise ValueError("give at least one of --k, --n or --value")
        readings: list[tuple[float, float]] = []
        if self.value is not None:
            quantity = parse_quantity(self.value)
            if quantity is None:
                raise ValueError(f"--value is not a number with an optional unit: {self.value!r}")
            unit = quantity.unit or self.unit
            tolerance = printed_tolerance(self.value, unit=self.unit)
            scale = canonical_value(1.0, unit)
            canonical = quantity.value * scale
            readings.append((canonical, tolerance))
            if not unit and abs(quantity.value) > 1:
                # A bare 60.0 may be a percentage stored as a fraction in the receipt.
                readings.append((quantity.value / 100, tolerance / 100))
        object.__setattr__(self, "readings", tuple(readings))

    def value_unit(self) -> str | None:
        if self.value is None:
            return None
        quantity = parse_quantity(self.value)
        return (quantity.unit if quantity else "") or self.unit

    def value_delta(self, number: float) -> float | None:
        """Smallest tolerance-normalized distance to the printed value, or None."""
        best: float | None = None
        unit = self.value_unit()
        for reading in (number, canonical_value(number, unit)):
            for target, tolerance in self.readings:
                distance = abs(reading - target)
                if distance <= tolerance + 1e-12:
                    scaled = distance / max(tolerance, 1e-12)
                    best = scaled if best is None else min(best, scaled)
        return best

    def count_kinds(self, number: float) -> list[str]:
        kinds: list[str] = []
        if self.k is not None and number == self.k:
            kinds.append("k")
        if self.n is not None and number == self.n:
            kinds.append("n")
        return kinds


@dataclass
class _Budget:
    deadline: float
    hit_limit_reached: bool = False
    timed_out: bool = False

    def expired(self) -> bool:
        if time.monotonic() > self.deadline:
            self.timed_out = True
        return self.timed_out


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _recipe(**kwargs: Any) -> dict[str, Any]:
    return Recipe(**kwargs).model_dump(mode="json", exclude_none=True)


def _hit(
    folder: str, rel: str, kind: str, found: dict[str, float], recipes: dict[str, Any],
    score: float, **extra: Any,
) -> dict[str, Any]:
    return {"folder": folder, "file": rel, "kind": kind, "found": found, "recipes": recipes,
            "_score": score, **extra}


def _scan_scalars(
    doc: object, rel: str, folder: str, targets: Targets, budget: _Budget,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    stack: list[tuple[object, str, int]] = [(doc, "", 0)]
    nodes = 0
    while stack and len(hits) < _PER_FILE:
        node, pointer, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or (nodes % 1024 == 0 and budget.expired()):
            break
        if isinstance(node, dict):
            items = [(f"{pointer}/{_escape(str(key))}", child) for key, child in node.items()]
        elif isinstance(node, list):
            if len(node) > _BIG_ARRAY and all(isinstance(item, dict) for item in node[:5]):
                continue  # Row-oriented; analysed by field, not leaf by leaf.
            items = [(f"{pointer}/{index}", child) for index, child in enumerate(node)]
        else:
            continue
        leaves: dict[str, float] = {}
        for child_pointer, child in items:
            number = _number(child)
            if number is not None:
                leaves[child_pointer] = number
            elif isinstance(child, (dict, list)) and depth < _DEPTH:
                stack.append((child, child_pointer, depth + 1))
        matched: dict[str, list[tuple[str, float]]] = {"k": [], "n": [], "value": []}
        for child_pointer, number in leaves.items():
            for kind in targets.count_kinds(number):
                matched[kind].append((child_pointer, number))
            if targets.value_delta(number) is not None:
                matched["value"].append((child_pointer, number))
        primary = next((kind for kind in ("k", "n", "value") if matched[kind]), None)
        if primary is None:
            continue
        for child_pointer, number in matched[primary]:
            pointers = {primary: child_pointer}
            found = {primary: number}
            for kind in ("k", "n", "value"):
                if kind == primary:
                    continue
                other = next((item for item in matched[kind] if item[0] != child_pointer), None)
                if other is not None:
                    pointers[kind] = other[0]
                    found[kind] = other[1]
            recipes: dict[str, Any] = {
                kind: _recipe(op="value", file=rel, pointer=pointer, target=kind)
                for kind, pointer in pointers.items()
            }
            if "k" in pointers and "n" in pointers:
                recipes["ratio"] = _recipe(
                    op="ratio", target="value",
                    num=_recipe(op="value", file=rel, pointer=pointers["k"]),
                    den=_recipe(op="value", file=rel, pointer=pointers["n"]),
                )
            delta = targets.value_delta(found["value"]) if "value" in found else None
            hits.append(_hit(
                folder, rel, "scalar", found, recipes,
                score=len(pointers) + (2 if {"k", "n"} <= pointers.keys() else 0),
                pointers=pointers, same_object=len(pointers) > 1, delta=delta or 0.0,
            ))
    return hits


def _get(row: object, path: str) -> object:
    current = row
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _fields(rows: Sequence[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows[:200]:
        for key, value in row.items():
            candidates = [str(key)]
            if isinstance(value, dict):
                candidates = [f"{key}.{inner}" for inner, item in value.items()
                              if not isinstance(item, (dict, list))]
            elif isinstance(value, list):
                continue
            for name in candidates:
                if name not in names:
                    names.append(name)
    return names[:MAX_FIELDS]


def _numeric(value: object) -> float | None:
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return _number(value)


def _scan_rows(
    rows: list[dict[str, Any]], rel: str, folder: str, pointer: str | None,
    targets: Targets, budget: _Budget,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    base: dict[str, Any] = {"file": rel}
    if pointer is not None:
        base["rows"] = pointer
    total = len(rows)
    for kind in targets.count_kinds(float(total)):
        hits.append(_hit(
            folder, rel, "row_count", {kind: float(total)},
            {kind: _recipe(op="count", target=kind, **base)}, score=1,
            note="every row counted; add where to count a subset", rows=total,
        ))
    for name in _fields(rows):
        if budget.expired() or len(hits) >= _PER_FILE:
            break
        values = [_get(row, name) for row in rows]
        present = [value for value in values if value is not None]
        if not present:
            continue
        numbers = [_numeric(value) for value in present]
        counted: list[tuple[str, float, dict[str, Any], dict[str, Any]]] = []
        if all(number is not None for number in numbers):
            floats = [number for number in numbers if number is not None]
            total_sum = math.fsum(floats)
            for kind in targets.count_kinds(total_sum):
                counted.append((kind, total_sum, _recipe(op="sum", field=name, target=kind,
                                                         **base), {"how": "sum of field"}))
            mean = total_sum / len(floats)
            delta = targets.value_delta(mean)
            if delta is not None:
                counted.append(("value", mean, _recipe(op="mean", field=name, target="value",
                                                       **base), {"how": "mean of field",
                                                                 "delta": delta}))
        if len(set(map(repr, present))) <= _MAX_CATEGORIES:
            for category, count in Counter(map(repr, present)).items():
                original = next(value for value in present if repr(value) == category)
                if isinstance(original, (dict, list)):
                    continue
                for kind in targets.count_kinds(float(count)):
                    counted.append((kind, float(count), _recipe(
                        op="count", where={name: original}, target=kind, **base),
                        {"how": f"rows where {name} == {original!r}",
                         "of_rows": total}))
        for kind, number, recipe, extra in counted:
            recipes: dict[str, Any] = {kind: recipe}
            score = 2.0
            if kind == "k" and targets.n == total:
                recipes["ratio"] = _recipe(
                    op="ratio", target="value",
                    num={key: item for key, item in recipe.items() if key != "target"},
                    den=_recipe(op="count", **base),
                )
                score += 2
            hits.append(_hit(folder, rel, "field", {kind: number}, recipes, score,
                             field=name, delta=extra.pop("delta", 0.0), **extra))
    return hits


def _row_arrays(doc: object) -> list[tuple[str | None, list[dict[str, Any]]]]:
    """Arrays of objects at the root, or under one or two keys of a JSON object."""
    found: list[tuple[str | None, list[dict[str, Any]]]] = []

    def rows_of(value: object) -> list[dict[str, Any]] | None:
        if isinstance(value, list) and len(value) >= 2 and all(
            isinstance(item, dict) for item in value[:20]
        ):
            return [item for item in value if isinstance(item, dict)][:MAX_ROWS]
        return None

    if isinstance(doc, list):
        rows = rows_of(doc)
        return [(None, rows)] if rows is not None else []
    if isinstance(doc, dict):
        for key, value in doc.items():
            rows = rows_of(value)
            if rows is not None:
                found.append((f"/{_escape(str(key))}", rows))
            elif isinstance(value, dict):
                for inner, deeper in value.items():
                    rows = rows_of(deeper)
                    if rows is not None:
                        found.append((f"/{_escape(str(key))}/{_escape(str(inner))}", rows))
    return found


def _order(file: ReceiptFile) -> tuple[int, int, str]:
    numbered = re.search(r"\d", file.name.rsplit(".", 1)[0]) is not None
    rank = 0 if is_aggregate_name(file.name) else 2 if numbered else 1
    return (rank, file.depth, file.path)


def locate_values(
    folders: Sequence[Path], *, k: int | None = None, n: int | None = None,
    value: str | None = None, unit: str | None = None,
    deadline_seconds: float = DEADLINE_SECONDS, max_hits: int = MAX_HITS,
) -> dict[str, Any]:
    """Return ranked locations of the requested numbers across ``folders``."""
    targets = Targets(k=k, n=n, value=value, unit=unit)
    budget = _Budget(time.monotonic() + deadline_seconds)
    found: list[dict[str, Any]] = []
    searched = skipped_large = unreadable = 0
    capped = False
    for folder in folders:
        scan = scan_receipts(folder)
        candidates = sorted(
            (file for file in scan.files if file.suffix in DATA_SUFFIXES), key=_order,
        )
        if len(candidates) > MAX_FILES:
            capped = True
            candidates = candidates[:MAX_FILES]
        for file in candidates:
            if budget.expired() or len(found) >= MAX_FOUND:
                break
            if file.size > MAX_FILE_BYTES:
                skipped_large += 1
                continue
            try:
                doc = load_recipe_file(file.path, folder)
            except RecipeError:
                unreadable += 1
                continue
            searched += 1
            if file.suffix == ".json":
                found.extend(_scan_scalars(doc, file.path, str(folder), targets, budget))
                arrays = _row_arrays(doc)
            else:
                rows = doc if isinstance(doc, list) else []
                arrays = [(None, rows)] if len(rows) >= 2 else []
            for pointer, rows in arrays:
                found.extend(_scan_rows(rows, file.path, str(folder), pointer, targets, budget))
    found.sort(key=lambda hit: (-hit["_score"], hit.get("delta", 0.0), hit["folder"],
                                hit["file"], str(hit.get("field", hit.get("pointers", "")))))
    hits = [{key: item for key, item in hit.items() if key != "_score"}
            for hit in found[:max_hits]]
    notes = ["Locations only: choose the filter from the source's definition, then test the "
             "recipe with ./q recipe before using it."]
    if any(item is not None and item <= 3 for item in (k, n)):
        notes.append("Small k or n matches many incidental values; prefer same_object hits.")
    if budget.timed_out:
        notes.append(f"Search stopped at the {deadline_seconds:g} second deadline.")
    if capped:
        notes.append(f"Only the first {MAX_FILES} data files per folder were searched.")
    if len(found) > max_hits:
        notes.append(f"{len(found) - max_hits} lower-ranked hits omitted.")
    return {
        "query": {"k": k, "n": n, "value": value, "unit": unit},
        "searched": {"folders": [str(folder) for folder in folders], "files": searched,
                     "skipped_large": skipped_large, "unreadable": unreadable},
        "hits": hits, "notes": notes,
    }
