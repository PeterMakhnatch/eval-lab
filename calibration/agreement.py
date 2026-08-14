"""Per-criterion agreement used by brief 09 `harbor-lab calibrate`.

This is the consume contract, not the CLI. BUILDER later calls these
functions from `harbor-lab calibrate <family>` and writes a
`judge_calibrations` record. Trajectory labels are not an input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .inventory import LabeledDocument, answer_key_path, iter_family_documents
from .rubrics import VERDICTS, all_criterion_names, criteria_for

JudgeOutput = dict[str, dict[str, str]]


@dataclass(frozen=True)
class CriterionComparison:
    dimension: str
    name: str
    expected: str
    observed: str | None
    agree: bool


@dataclass(frozen=True)
class DocumentAgreement:
    doc_id: str
    variant: str
    comparisons: tuple[CriterionComparison, ...]

    @property
    def n_total(self) -> int:
        return len(self.comparisons)

    @property
    def n_agree(self) -> int:
        return sum(1 for c in self.comparisons if c.agree)

    @property
    def rate(self) -> float:
        return self.n_agree / self.n_total if self.n_total else 0.0


def extract_verdicts(judge_output: dict[str, Any], family: str) -> JudgeOutput:
    """Normalize a judge blob to dimension -> criterion -> yes|no.

    Accepts either the sealed-key `criteria` shape (`{verdict, rationale}`)
    or a flat `{dimension: {name: "yes"}}` map. Unknown names are ignored;
    missing names become None at compare time.
    """
    criteria = judge_output.get("criteria", judge_output)
    out: JudgeOutput = {}
    for dimension, names in criteria_for(family).items():
        block = criteria.get(dimension) or {}
        out[dimension] = {}
        for name in names:
            cell = block.get(name)
            verdict = cell.get("verdict") if isinstance(cell, dict) else cell
            if verdict in VERDICTS:
                out[dimension][name] = verdict
    return out


def compare_document(
    family: str,
    expected_key: dict[str, Any],
    judge_output: dict[str, Any],
) -> DocumentAgreement:
    observed = extract_verdicts(judge_output, family)
    expected_block = expected_key.get("criteria") or {}
    comparisons: list[CriterionComparison] = []
    for dimension, name in all_criterion_names(family):
        exp_cell = (expected_block.get(dimension) or {}).get(name) or {}
        expected = exp_cell.get("verdict") if isinstance(exp_cell, dict) else None
        got = (observed.get(dimension) or {}).get(name)
        comparisons.append(
            CriterionComparison(
                dimension=dimension,
                name=name,
                expected=expected or "",
                observed=got,
                agree=expected in VERDICTS and got == expected,
            )
        )
    return DocumentAgreement(
        doc_id=expected_key.get("document_id") or expected_key.get("document") or "",
        variant=expected_key.get("variant") or "",
        comparisons=tuple(comparisons),
    )


def per_criterion_rates(agreements: list[DocumentAgreement]) -> dict[str, float]:
    totals: dict[str, list[int]] = {}
    for agreement in agreements:
        for cell in agreement.comparisons:
            key = f"{cell.dimension}.{cell.name}"
            bucket = totals.setdefault(key, [0, 0])
            bucket[1] += 1
            if cell.agree:
                bucket[0] += 1
    return {name: hits / n if n else 0.0 for name, (hits, n) in sorted(totals.items())}


def corpus_digest_inputs(family: str, root=None) -> list[LabeledDocument]:
    """Documents `harbor-lab calibrate <family>` must score, in manifest order."""
    return iter_family_documents(family, root)


def load_family_gold(family: str, root=None) -> list[tuple[LabeledDocument, dict[str, Any]]]:
    import json

    pairs = []
    for doc in corpus_digest_inputs(family, root):
        key = json.loads(answer_key_path(doc, root).read_text(encoding="utf-8"))
        pairs.append((doc, key))
    return pairs
