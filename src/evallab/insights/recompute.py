"""Reproduce claims from bounded, experiment-local receipt files."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from evallab.insights.models import ExtractedClaim, Recipe, RecomputeField, RecomputeResult
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
# A printed value that was rounded twice (4 decimals, then 3) can sit up to 0.55 of a unit
# in its last digit from the receipt: half a unit plus half a unit of the digit below.
_ROUNDING_BAND = 1.1


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


def _gap_text(gap: float, claim: ExtractedClaim, counted: bool) -> str:
    """A gap spelled in compared units and, where one exists, in the claim's own unit."""
    # Lazy: check.py imports run_recipe at module level for card recipes.
    from evallab.insights.check import is_proportion_claim

    if counted:
        return f"{gap:g}"
    if is_proportion_claim(claim):
        return f"{gap:.3g} ({gap * 100:.3g} percentage points)"
    factor = canonical_value(1.0, claim.unit)
    if claim.unit and factor not in (0.0, 1.0):
        return f"{gap:.3g} ({gap / factor:.3g} {claim.unit})"
    return f"{gap:.3g} {claim.unit}" if claim.unit else f"{gap:.3g}"


_FIELDS: tuple[RecomputeField, ...] = ("k", "n", "value")
# A matching 0, 1 or 100 (0% / 100% once canonical) is likely to be found in many unrelated
# receipt fields, so such a match needs a pointer that ties it to the claim.
_LOW_INFORMATION = frozenset({0.0, 1.0, 100.0})
# Words that appear in nearly every receipt path or claim label and say nothing about the
# cohort or metric, so sharing one proves nothing.
_GENERIC_TOKENS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "all", "any", "per", "not", "was",
    "are", "has", "had", "rate", "count", "counts", "total", "totals", "value", "values",
    "number", "num", "mean", "avg", "average", "sum", "result", "results", "metric",
    "metrics", "stat", "stats", "data", "report", "field", "dev", "test", "split", "key",
    "keys", "item", "items", "row", "rows",
})
_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _tokens(text: str) -> set[str]:
    """Lowercase word stems of a label or JSON pointer, minus generic words."""
    words = _WORD.findall(_CAMEL.sub(" ", text).casefold())
    stems = {word[:-1] if len(word) > 3 and word.endswith("s") else word for word in words}
    return {stem for stem in stems if len(stem) >= 2 and stem not in _GENERIC_TOKENS}


def _claim_tokens(claim: ExtractedClaim, *, condition_only: bool = False) -> set[str]:
    """Cohort and metric words the claim names (the experiment id is in every path)."""
    parts = [claim.condition] if condition_only else [
        claim.metric, claim.subject, claim.dataset, claim.condition, *claim.models,
    ]
    return set().union(*(_tokens(part) for part in parts if part))


def _leaves(recipe: Recipe) -> list[Recipe]:
    if recipe.op == "ratio" and recipe.num is not None and recipe.den is not None:
        return [*_leaves(recipe.num), *_leaves(recipe.den)]
    return [recipe]


def _pointer_path(recipe: Recipe) -> str:
    """The in-receipt location of every number a recipe reads (not the file name)."""
    return " ".join(
        part for leaf in _leaves(recipe) for part in (leaf.pointer, leaf.rows, leaf.field) if part
    )


def recipe_reads(recipe: Recipe) -> str:
    """Name the receipt file and pointer (or rows and filter) a recipe reads."""
    if recipe.op == "ratio" and recipe.num is not None and recipe.den is not None:
        return f"({recipe_reads(recipe.num)}) / ({recipe_reads(recipe.den)})"
    if recipe.op == "value":
        return f"{recipe.file}#{recipe.pointer}"
    rows = f"{recipe.file}#{recipe.rows}" if recipe.rows else str(recipe.file)
    where = "" if not recipe.where else f" where {json.dumps(recipe.where, sort_keys=True)}"
    field = f" of {recipe.field}" if recipe.field else ""
    return f"{recipe.op}{field} over rows of {rows}{where}"


def _claim_label(claim: ExtractedClaim, target: str) -> str:
    context = [f"{name} {text!r}" for name, text in (
        ("metric", claim.metric), ("condition", claim.condition)) if text]
    return f"{target} ({'; '.join(context)})" if context else target


def _low_information(*readings: float) -> bool:
    return any(reading in _LOW_INFORMATION for reading in readings)


def _stated_readings(claim: ExtractedClaim, fields: Iterable[RecomputeField]) -> list[float]:
    readings: list[float] = []
    for name in fields:
        if name == "value" and claim.value is not None:
            readings.extend((claim.value, claim.value * canonical_value(1.0, claim.unit)))
        elif name in {"k", "n"} and getattr(claim, name) is not None:
            readings.append(float(getattr(claim, name)))
    return readings


def _is_weak_provenance(claim: ExtractedClaim, recipe: Recipe, readings: list[float]) -> bool:
    """A low-information match whose pointer names nothing of the claim and has no filter."""
    if not _low_information(*readings):
        return False
    if any(leaf.where for leaf in _leaves(recipe)):
        return False
    return not (_claim_tokens(claim) & _tokens(_pointer_path(recipe)))


def _compare(claim: ExtractedClaim, recipe: Recipe, folder: Path | None) -> RecomputeResult:
    """Compare one recipe with the claim quantity it targets (reproduced/differs/not)."""
    # Lazy: check.py imports run_recipe at module level for card recipes.
    from evallab.insights.check import is_proportion_claim, value_printed_tolerance

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
    note: Literal["rounding"] | None = None
    detail = (f"Receipt {recipe_reads(recipe)} gives {value:g}; "
              f"claim {_claim_label(claim, target)} states {stated:g}")
    counted = target in {"k", "n"} or recipe.op in {"count", "distinct_count"}
    if counted:
        gap = abs(value - stated)
        matches = value == stated
    else:
        factor = canonical_value(1.0, claim.unit)
        derived = claim.value is None
        stated_canonical = stated if derived else stated * factor
        tolerance = 1e-9 if derived else value_printed_tolerance(claim)
        if abs(stated_canonical) >= _CAP_FROM:
            tolerance = min(tolerance, _RELATIVE_CAP * abs(stated_canonical))
        gap = min(abs(reading - stated_canonical) for reading in (value, value * factor))
        matches = gap <= tolerance + 1e-12
        if not matches and not derived and gap <= tolerance * _ROUNDING_BAND + 1e-12:
            matches, note = True, "rounding"
    gap_text = _gap_text(gap, claim, counted)
    if matches:
        if note is not None:
            detail += (f"; the gap of {gap_text} is within the rounding of the printed "
                       "value (rounding)")
        return RecomputeResult(
            status="reproduced", value=value, stated=stated, detail=detail + ".", note=note,
            difference=gap if note else None, difference_text=gap_text if note else "",
        )
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
    detail += f"; differs by {gap_text}."
    condition = _claim_tokens(claim, condition_only=True)
    location = _tokens(" ".join(
        f"{leaf.file or ''} {_pointer_path(leaf)} {json.dumps(leaf.where or {})}"
        for leaf in _leaves(recipe)
    ))
    absent = sorted(condition - location)
    if absent:
        detail += (f" The receipt location does not mention the claim's condition word(s) "
                   f"({', '.join(absent)}); check it reads the right arm or cohort.")
    return RecomputeResult(
        status="differs", value=value, stated=stated, detail=detail,
        difference=gap, difference_text=gap_text,
    )


def _credited(claim: ExtractedClaim, recipe: Recipe, folder: Path) -> set[RecomputeField]:
    """Stated quantities a matching recipe validated.

    A recipe validates the quantity it targets. A ratio of two recipes also validates k and
    n when its numerator and denominator equal them, since it then read the counts itself.
    """
    target = recipe_target(claim, recipe)
    if target != "value":
        return {target}
    credited: set[RecomputeField] = {"value"} if claim.value is not None else set()
    if recipe.op == "ratio" and recipe.num is not None and recipe.den is not None:
        numerator = run_recipe(recipe.num, folder)
        denominator = run_recipe(recipe.den, folder)
        if claim.k is not None and math.isclose(numerator, claim.k, abs_tol=1e-9):
            credited.add("k")
        if claim.n is not None and math.isclose(denominator, claim.n, abs_tol=1e-9):
            credited.add("n")
    return credited


def _names(fields: Iterable[str]) -> str:
    names = list(fields)
    return " and ".join(names) if len(names) < 3 else f"{', '.join(names[:-1])} and {names[-1]}"


def recompute_claim(claim: ExtractedClaim, folder: Path | None) -> RecomputeResult | None:
    """Compare a recipe's result with the claim quantity named by ``recipe.target``.

    Counts are compared as exact integers. A scalar is compared in canonical units
    (percent/100, durations in seconds) within half a unit of its last printed digit,
    capped at 0.5% of a large value; a gap of up to 0.55 of that unit (a value rounded
    twice) is still reproduced and carries the note ``rounding``. A receipt may hold a
    quantity either in canonical units or in the claim's printed unit. A count recipe
    against a stated rate is a wrong target, not a disagreement with the source, so it
    is not recomputable. Every ``differs`` records the size of the gap and names the
    receipt location it read.

    A match is ``reproduced`` only when every stated quantity (k, n, value) was validated:
    by a ratio whose numerator and denominator equal k and n, or by the recipe plus
    companion recipes (``recipe.also``) that each target another field. Otherwise the match
    is ``partial`` and names what it validated and what it never read. A match on 0, 1 or
    100 (0%, 100%) at a pointer that names no cohort or metric token of the claim, with no
    ``where`` filter, is also ``partial`` and carries the ``weak_provenance`` note.
    """
    recipe = claim.recipe
    if recipe is None:
        return None
    primary = _compare(claim, recipe, folder)
    if primary.status != "reproduced" or folder is None:
        return primary
    legs = [(recipe, _credited(claim, recipe, folder))]
    problems: list[str] = []
    for extra in recipe.also:
        result = _compare(claim, extra, folder)
        if result.status == "differs":
            return result.model_copy(
                update={"detail": f"Companion recipe for {extra.target}: {result.detail}"})
        if result.status == "reproduced":
            legs.append((extra, _credited(claim, extra, folder)))
        else:
            problems.append(f"The companion recipe for {extra.target} could not run: "
                            f"{result.detail}")
    credited = {name for _, names in legs for name in names}
    required = [name for name in _FIELDS if _stated_readings(claim, [name])]
    validated = [name for name in required if name in credited]
    unchecked = [name for name in required if name not in credited]
    weak = False
    for leg_recipe, leg_credited in legs:
        readings = _stated_readings(claim, leg_credited)
        if not readings and primary.stated is not None:
            readings = [primary.stated]
        weak = weak or _is_weak_provenance(claim, leg_recipe, readings)
    if not unchecked and not weak:
        return primary.model_copy(update={"validated": validated})
    update: dict[str, Any] = {
        "status": "partial", "validated": validated, "unchecked": unchecked,
        "provenance_note": "weak_provenance" if weak else None,
    }
    partial = primary.model_copy(update=update)
    detail = primary.detail
    summary = describe_recompute(partial)
    detail += f" Partial: {summary}."
    if weak:
        detail += (" Weak provenance: a 0, 1 or 100 matched at a pointer that names no cohort "
                   "or metric word of the claim and has no where filter, so it may be a "
                   "coincidental field.")
    detail += "".join(f" {problem}" for problem in problems)
    return partial.model_copy(update={"detail": detail})


def describe_recompute(result: RecomputeResult | None) -> str:
    """Plain-language outcome, e.g. ``k matches receipt; n not checked``."""
    if result is None:
        return "no receipt recipe"
    if result.status == "reproduced":
        if not result.validated:
            return "matches receipt"
        return f"{_names(result.validated)} {'matches' if len(result.validated) == 1 else 'match'} receipt"
    if result.status == "partial":
        parts: list[str] = []
        if result.validated:
            verb = "matches" if len(result.validated) == 1 else "match"
            parts.append(f"{_names(result.validated)} {verb} receipt")
        if result.unchecked:
            parts.append(f"{_names(result.unchecked)} not checked")
        if result.provenance_note:
            parts.append("weak provenance (pointer names no cohort or metric word of the claim)")
        return "; ".join(parts) or "partially recomputed"
    return result.detail


def recompute_counts(results: Iterable[RecomputeResult | None]) -> dict[str, int]:
    """Count outcomes with ``partial`` kept apart from ``reproduced``."""
    counts = {"reproduced": 0, "partial": 0, "differs": 0, "not_recomputable": 0, "no recipe": 0}
    for result in results:
        counts["no recipe" if result is None else result.status] += 1
    return counts
