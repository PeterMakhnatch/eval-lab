"""Inspect what a compiled judge instruction learned: general rules vs family shortcuts.

    PYTHONPATH=research/experiments/dspy uv run python -m judge.inspect_program \
        judge/artifacts/gepa-checkout/program.json [more program.json ...]

For each saved program this prints the instruction length, how many of each
family's reference-fact tokens and criterion names the instruction mentions, and
any literal fixture file names it names. A high count for exactly one family is
the signature of a memorised shortcut; a program that cites neither family's
facts but many criterion names is stating rubric-level rules.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from evallab.calibrate import RUBRICS

FIXTURE_FILES = re.compile(
    r"`?\b[\w-]+\.(?:csv|yaml|yml|log|json|txt|toml|md)\b`?|/app/evidence[\w/.-]*", re.I
)
STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "at",
    "was",
    "were",
    "with",
    "for",
    "from",
    "by",
    "on",
    "is",
    "as",
    "no",
    "not",
    "after",
    "later",
    "about",
    "every",
    "any",
    "that",
    "this",
    "its",
    "than",
    "only",
}


def _tokens(text: str) -> set[str]:
    raw = [t.rstrip(".:") for t in re.findall(r"[a-z0-9][a-z0-9.:=_-]*", text.lower())]
    # keep dotted forms and their tail so `evidence_fidelity.invents_evidence` counts as the criterion
    expanded = set(raw) | {t.rsplit(".", 1)[-1] for t in raw if "." in t}
    return {t for t in expanded if t not in STOP and len(t) > 2}


def fact_tokens(family: str) -> set[str]:
    return set().union(*(_tokens(f) for f in RUBRICS[family]["reference_facts"]))


def criterion_names(family: str) -> set[str]:
    return {name for block in RUBRICS[family]["criteria"].values() for name in block}


def inspect(path: Path) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    predictor = state.get("judge.predict") or next(iter(state.values()))
    instruction = predictor["signature"]["instructions"]
    tokens = _tokens(instruction)
    report = {
        "program": path.as_posix(),
        "instruction_chars": len(instruction),
        "instruction_lines": instruction.count("\n") + 1,
        "demos": len(predictor.get("demos", [])),
        "fixture_files_named": sorted({m.strip("`") for m in FIXTURE_FILES.findall(instruction)}),
    }
    shared = set.intersection(*(fact_tokens(f) for f in RUBRICS))
    for family in RUBRICS:
        own = fact_tokens(family) - shared
        report[family] = {
            "family_specific_fact_tokens_mentioned": sorted(tokens & own),
            "criterion_names_mentioned": len(tokens & criterion_names(family)),
            "criterion_names_total": len(criterion_names(family)),
        }
    return report


def main(argv: list[str]) -> int:
    for arg in argv:
        report = inspect(Path(arg))
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
