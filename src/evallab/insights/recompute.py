"""Reproduce claims from bounded, experiment-local receipt files."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path
from typing import Any, Literal

from evallab.insights.models import ExtractedClaim, Recipe, RecomputeResult
from evallab.insights.units import canonical_value

MAX_FILE_BYTES = 64 * 1024 * 1024
_MISSING = object()
_OPERATORS = {"$ne", "$in", "$nin", "$exists", "$gt", "$gte", "$lt", "$lte", "$contains"}


class RecipeError(ValueError):
    """A receipt cannot safely or unambiguously reproduce a recipe."""


def load_recipe_file(file: str, folder: Path) -> object:
    """Read JSON, JSONL or CSV, refusing files outside the experiment folder."""
    relative = Path(file)
    if relative.is_absolute() or ".." in relative.parts:
        raise RecipeError("receipt paths must be relative and must not contain '..'")
    try:
        root = folder.resolve(strict=True)
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root):
            raise RecipeError("receipt path resolves outside the experiment folder")
        if not path.is_file():
            raise RecipeError("receipt must be a regular file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise RecipeError("receipt exceeds the 64 MiB size limit")
        with path.open("rb") as handle:
            payload = handle.read(MAX_FILE_BYTES + 1)
        if len(payload) > MAX_FILE_BYTES:
            raise RecipeError("receipt exceeds the 64 MiB size limit")
        text = payload.decode("utf-8-sig")
        suffix = relative.suffix.lower()
        if suffix == ".json":
            return json.loads(text)
        if suffix == ".jsonl":
            rows: list[dict[str, Any]] = []
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise RecipeError(f"bad JSONL at line {line_number}: {exc}") from exc
                if not isinstance(row, dict):
                    raise RecipeError(f"bad JSONL at line {line_number}: expected an object")
                rows.append(row)
            return rows
        if suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text), strict=True)
            csv_rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise RecipeError(f"bad CSV at line {reader.line_num}: inconsistent columns")
                csv_rows.append(row)
            return csv_rows
        raise RecipeError("receipt format must be .json, .jsonl or .csv")
    except (OSError, RuntimeError, UnicodeError, ValueError, csv.Error) as exc:
        if isinstance(exc, RecipeError):
            raise
        raise RecipeError(f"cannot read receipt {file}: {exc}") from exc


def json_pointer(value: object, pointer: str) -> object:
    """Select a JSON value using RFC 6901 (including the empty root pointer)."""
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise RecipeError("JSON pointer must be empty or start with '/'")
    current = value
    for encoded in pointer[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise RecipeError("JSON pointer has an invalid '~' escape")
        part = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            if index >= len(current):
                raise RecipeError(f"JSON pointer index is out of range: {part}")
            current = current[index]
        else:
            raise RecipeError(f"JSON pointer does not resolve: {pointer}")
    return current


def _field(row: object, path: str) -> object:
    current = row
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdecimal() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RecipeError("expected a finite numeric value")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise RecipeError("expected a finite numeric value") from exc
    if not math.isfinite(number):
        raise RecipeError("expected a finite numeric value")
    return number


def _equal(actual: object, expected: object) -> bool:
    # CSV fields are strings, but a numeric filter still means numeric equality.
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return _number(actual) == _number(expected)
        except RecipeError:
            return False
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected


def values_equal(actual: object, expected: object) -> bool:
    """Receipt-value equality: numeric-aware (0 == 0.0 == "0"), bool-strict."""
    return _equal(actual, expected)


def _validate_where(where: dict[str, Any]) -> None:
    for condition in where.values():
        if not isinstance(condition, dict):
            continue
        for operator, expected in condition.items():
            if operator not in _OPERATORS:
                raise RecipeError(f"unknown where operator: {operator}")
            if operator in {"$in", "$nin"} and not isinstance(expected, list):
                raise RecipeError(f"{operator} needs a list")
            if operator == "$exists" and not isinstance(expected, bool):
                raise RecipeError("$exists needs true or false")
            if operator in {"$gt", "$gte", "$lt", "$lte"}:
                _number(expected)


def _condition(actual: object, condition: object) -> bool:
    if isinstance(condition, list):
        return any(_equal(actual, candidate) for candidate in condition)
    if not isinstance(condition, dict):
        return actual is not _MISSING and _equal(actual, condition)
    for operator, expected in condition.items():
        if operator == "$exists":
            matches = (actual is not _MISSING) == expected
        elif operator == "$ne":
            matches = not _equal(actual, expected)
        elif operator in {"$in", "$nin"}:
            found = any(_equal(actual, candidate) for candidate in expected)
            matches = found if operator == "$in" else not found
        elif operator == "$contains":
            if isinstance(actual, str) and isinstance(expected, str):
                matches = expected in actual
            elif isinstance(actual, (list, dict)):
                matches = any(_equal(item, expected) for item in actual)
            else:
                matches = False
        else:
            try:
                left, right = _number(actual), _number(expected)
            except RecipeError:
                return False
            matches = {
                "$gt": left > right,
                "$gte": left >= right,
                "$lt": left < right,
                "$lte": left <= right,
            }[operator]
        if not matches:
            return False
    return True


def row_matches(row: object, where: dict[str, Any] | None) -> bool:
    """Apply exactly the validated field-filter semantics used by recipes."""
    conditions = where or {}
    _validate_where(conditions)
    return all(_condition(_field(row, key), value) for key, value in conditions.items())


def run_recipe(recipe: Recipe, folder: Path) -> float:
    """Evaluate a declarative recipe without executing code from a receipt."""
    if recipe.op == "ratio":
        if recipe.num is None or recipe.den is None:
            raise RecipeError("ratio recipes need num and den")
        numerator = run_recipe(recipe.num, folder)
        denominator = run_recipe(recipe.den, folder)
        if denominator == 0:
            raise RecipeError("ratio denominator is zero")
        return _number(numerator / denominator)
    if recipe.file is None:
        raise RecipeError("recipe needs a file")
    data = load_recipe_file(recipe.file, folder)
    if recipe.op == "value":
        if Path(recipe.file).suffix.lower() != ".json":
            raise RecipeError("value recipes require a .json file")
        if recipe.pointer is None:
            raise RecipeError("value recipes need a pointer")
        return _number(json_pointer(data, recipe.pointer))
    if recipe.rows is not None:
        if Path(recipe.file).suffix.lower() != ".json":
            raise RecipeError("rows pointers require a .json file")
        data = json_pointer(data, recipe.rows)
    if not isinstance(data, list):
        raise RecipeError("row recipes need an array; set rows to its JSON pointer")
    where = recipe.where or {}
    _validate_where(where)
    rows = [
        row for row in data
        if all(_condition(_field(row, key), val) for key, val in where.items())
    ]
    if recipe.op == "count":
        return float(len(rows))
    if recipe.field is None:
        raise RecipeError(f"{recipe.op} recipes need a field")
    values = [_field(row, recipe.field) for row in rows]
    if any(value is _MISSING for value in values):
        raise RecipeError(f"field is missing in a selected row: {recipe.field}")
    if recipe.op == "distinct_count":
        try:
            distinct = {
                json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
                for value in values
            }
        except (TypeError, ValueError) as exc:
            raise RecipeError("distinct values must be finite JSON values") from exc
        return float(len(distinct))
    try:
        total = math.fsum(_number(value) for value in values)
    except OverflowError as exc:
        raise RecipeError("numeric aggregate exceeds the finite numeric range") from exc
    if recipe.op == "sum":
        return _number(total)
    if not values:
        raise RecipeError("mean has no selected rows")
    return _number(total / len(values))


_RELATIVE_CAP = 0.005
_CAP_FROM = 1000.0


def recipe_target(claim: ExtractedClaim, recipe: Recipe) -> Literal["k", "n", "value"]:
    """The claim quantity a recipe is compared with (explicit, else by operation)."""
    if recipe.target is not None:
        return recipe.target
    if recipe.op in {"count", "distinct_count"}:
        return "k" if claim.k is not None else "value"
    if claim.value is None and claim.k is not None and not (recipe.op == "ratio" and claim.n):
        return "k"
    return "value"


def _not_recomputable(detail: str, *, stated: float | None = None) -> RecomputeResult:
    return RecomputeResult(status="not_recomputable", stated=stated, detail=detail)


def recompute_claim(claim: ExtractedClaim, folder: Path | None) -> RecomputeResult | None:
    """Compare a recipe's result with the claim quantity named by ``recipe.target``.

    Counts are compared as exact integers. A scalar is compared in canonical units
    (percent/100, durations in seconds) within half a unit of its last printed digit,
    capped at 0.5% of a large value. A receipt may hold a quantity either in canonical
    units or in the claim's printed unit. A count recipe against a stated rate is a
    wrong target, not a disagreement with the source, so it is not recomputable.
    """
    # Lazy: check.py imports run_recipe at module level for card recipes.
    from evallab.insights.check import is_proportion_claim, value_printed_tolerance

    recipe = claim.recipe
    if recipe is None:
        return None
    if folder is None:
        return _not_recomputable("No experiment folder is known.")
    target = recipe_target(claim, recipe)
    stated: float | None
    if target == "k":
        stated = None if claim.k is None else float(claim.k)
    elif target == "n":
        stated = None if claim.n is None else float(claim.n)
    else:
        stated = claim.value
        if stated is None and recipe.op == "ratio" and claim.k is not None and claim.n:
            stated = claim.k / claim.n
    if stated is None or not math.isfinite(stated):
        return _not_recomputable(f"The claim has no {target} to compare with the recipe.")
    try:
        value = run_recipe(recipe, folder)
    except (RecipeError, OverflowError) as exc:
        return _not_recomputable(str(exc), stated=stated)
    detail = f"Receipt gives {value:g}; claim {target} states {stated:g}."
    if target in {"k", "n"} or recipe.op in {"count", "distinct_count"}:
        matches = value == stated
    else:
        factor = canonical_value(1.0, claim.unit)
        derived = claim.value is None
        stated_canonical = stated if derived else stated * factor
        tolerance = 1e-9 if derived else value_printed_tolerance(claim)
        if abs(stated_canonical) >= _CAP_FROM:
            tolerance = min(tolerance, _RELATIVE_CAP * abs(stated_canonical))
        readings = {value, value * factor}
        matches = any(abs(reading - stated_canonical) <= tolerance + 1e-12 for reading in readings)
    if matches:
        return RecomputeResult(status="reproduced", value=value, stated=stated, detail=detail)
    if target == "value" and is_proportion_claim(claim):
        selected = next(
            (name for name, count in (("k", claim.k), ("n", claim.n))
             if count is not None and value == count and value > 1),
            None,
        )
        if selected is not None or recipe.op in {"count", "distinct_count"}:
            what = f"the count {selected} = {value:g}" if selected else f"a count ({value:g})"
            return _not_recomputable(
                f"The recipe selects {what}, but the claim states a rate; "
                "set target to k or n, or use a ratio recipe.",
                stated=stated,
            )
    return RecomputeResult(status="differs", value=value, stated=stated, detail=detail)
