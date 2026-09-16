"""Exact-match scoring with format normalisation for the synthetic suite."""

from __future__ import annotations

import re

from evallab.rlm.bench.generators import BenchTask

_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$", re.MULTILINE)
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)$")


def normalize_answer(text: str) -> str:
    value = _FENCE.sub("", str(text)).strip()
    value = value.strip("\"'`").strip()
    value = value.rstrip(".;:!").strip()
    value = re.sub(r"\s+", " ", value)
    if value.startswith(("$", "USD ", "usd ")):
        value = value.lstrip("$").removeprefix("USD ").removeprefix("usd ").strip()
    if value.lower().endswith(" usd"):
        value = value[:-4].strip()
    if value.lower().endswith(" office"):
        value = value[:-7].strip()
    if value.lower().startswith("the "):
        value = value[4:].strip()
    candidate = value.replace(",", "")
    if _NUMBER.match(candidate):
        value = candidate
    return value.casefold()


def _as_number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def score(task: BenchTask, prediction: str) -> float:
    expected = normalize_answer(task.answer)
    got = normalize_answer(prediction)
    if got == expected:
        return 1.0
    expected_number = _as_number(expected)
    got_number = _as_number(got)
    if expected_number is not None and got_number is not None:
        tolerance = max(0.01, abs(expected_number) * 0.005)
        return 1.0 if abs(expected_number - got_number) <= tolerance else 0.0
    return 0.0
