"""Reproduce claims from bounded, experiment-local receipt files."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from evallab.insights.models import ExtractedClaim, Recipe, RecomputeResult

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


def recompute_claim(claim: ExtractedClaim, folder: Path | None) -> RecomputeResult | None:
    """Compare a recipe to the claim, allowing only the stated rounding tolerance."""
    if claim.recipe is None:
        return None
    if folder is None:
        return RecomputeResult(status="not_recomputable", detail="No experiment folder is known.")
    operation = claim.recipe.op
    if operation == "ratio":
        stated = claim.value
        if stated is None and claim.k is not None and claim.n:
            stated = claim.k / claim.n
    elif operation in {"count", "distinct_count"}:
        stated = float(claim.k) if claim.k is not None else claim.value
    else:
        stated = claim.value
    if stated is None or not math.isfinite(stated):
        return RecomputeResult(status="not_recomputable", detail="The claim has no comparable value.")
    try:
        value = run_recipe(claim.recipe, folder)
    except (RecipeError, OverflowError) as exc:
        return RecomputeResult(status="not_recomputable", stated=stated, detail=str(exc))
    if operation in {"count", "distinct_count"}:
        matches = value == stated
    elif (
        operation == "ratio"
        or (claim.unit or "").lower() in {
            "%", "percent", "percentage", "proportion", "fraction", "rate"
        }
        or (
            claim.k is not None
            and claim.n
            and abs(stated - claim.k / claim.n) <= 0.006 + 1e-12
        )
    ):
        matches = abs(value - stated) <= 0.0051 + 1e-12
    else:
        matches = math.isclose(value, stated, rel_tol=0.005, abs_tol=0.0)
    status = "reproduced" if matches else "differs"
    detail = f"Receipt gives {value:g}; claim states {stated:g}."
    return RecomputeResult(status=status, value=value, stated=stated, detail=detail)
