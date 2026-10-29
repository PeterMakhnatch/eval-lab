"""Pure logic for TQ-UNVERIFIED: eligible findings, dedupe, units, evidence checks, labels and statistics."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

SEED = "TQ-UNVERIFIED-20261029"
GATE_KEY = "new_verified_0_5"
ROUTES = {"sol": "openai-codex/gpt-6.1-sol", "opus": "anthropic/claude-opus-5-5"}
MATCHER_ROUTE = "openai-codex/gpt-6.1-terra"
CALL_CAP = 150
KNOWN_REAL_TARGET = 20
OFF_TARGET_ONLY_TARGET = 6
KNOWN_REAL_FLOOR = 10
VALIDITY_RECALL = 0.70
REAL_LOW, REAL_HIGH = 0.25, 0.60


def order_key(tag: str, ident: str) -> str:
    return hashlib.sha256(f"{SEED}:{tag}:{ident}".encode()).hexdigest()


def unit_id(case_id: str) -> str:
    return "U" + order_key("unit", case_id)[:10]


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def eligible_findings(cell: Mapping[str, Any], gate: str) -> list[dict[str, Any]]:
    """Findings named in the production gate's membership list (member ``C<n>`` is candidate ``#n``)."""
    if cell.get("status") != "ok" or not cell.get("flags", {}).get(GATE_KEY):
        return []
    prediction = cell["prediction"]
    members = set(prediction["memberships"][GATE_KEY])
    by_rank = {f"C{p['candidate_id'].rsplit('#', 1)[1]}": p for p in prediction["parsed_payloads"]}
    if not members <= set(by_rank):
        raise ValueError("membership names a finding that was not parsed")
    out = []
    for member in sorted(members, key=lambda m: int(m[1:])):
        payload = by_rank[member]
        out.append(
            {
                "gate": gate,
                "rank": int(member[1:]),
                "direction": payload["direction"],
                "mechanism": payload["mechanism"],
                "citations": [
                    {
                        "path": c["path"],
                        "line": c["line"],
                        "end_line": c["end_line"],
                        "quote": c["quote"],
                    }
                    for c in payload["citations"]
                ],
            }
        )
    return out


def _citation_set(finding: Mapping[str, Any]) -> frozenset[tuple[str, int, int]]:
    return frozenset((c["path"], c["line"], c["end_line"]) for c in finding["citations"])


def dedupe(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical findings across gates; each candidate keeps every (gate, rank) attribution."""
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        for candidate in candidates:
            same_text = normalise(candidate["mechanism"]) == normalise(finding["mechanism"])
            same_cites = (
                candidate["direction"] == finding["direction"]
                and _citation_set(candidate) == _citation_set(finding)
                and bool(_citation_set(finding))
            )
            if same_text or same_cites:
                candidate["sources"].append({"gate": finding["gate"], "rank": finding["rank"]})
                break
        else:
            candidate = {k: v for k, v in finding.items() if k not in ("gate", "rank")}
            candidate["sources"] = [{"gate": finding["gate"], "rank": finding["rank"]}]
            candidates.append(candidate)
    return candidates


def shuffle_and_label(case_id: str, candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Seeded order and opaque ``F<n>`` ids; the order carries no gate information."""
    ordered = sorted(
        candidates,
        key=lambda c: order_key("finding", case_id + "|" + normalise(c["mechanism"])),
    )
    return [{**c, "finding_id": f"F{i + 1}"} for i, c in enumerate(ordered)]


_LINE = re.compile(r"^(\d+): ?(.*)$")


def parse_source(text: str) -> dict[str, dict[int, str]]:
    """``FILE path`` headed sections of ``N: text`` lines, as the gate displayed them."""
    files: dict[str, dict[int, str]] = {}
    current: dict[int, str] | None = None
    for raw in text.split("\n"):
        if raw.startswith("FILE "):
            current = files.setdefault(raw[5:].strip(), {})
            continue
        match = _LINE.match(raw)
        if match and current is not None:
            current[int(match.group(1))] = match.group(2)
    return files


def verified_evidence(
    evidence: Iterable[Mapping[str, Any]], files: Mapping[str, Mapping[int, str]]
) -> int:
    """Count evidence rows whose path exists, line exists, and whose quote occurs in the cited file."""
    count = 0
    for row in evidence:
        lines = files.get(row["path"])
        quote = normalise(row["quote"])
        if not lines or row["line"] not in lines or not quote:
            continue
        if quote in normalise(" ".join(lines[n] for n in sorted(lines))):
            count += 1
    return count


def effective_verdict(
    verdict: Mapping[str, Any], files: Mapping[str, Mapping[int, str]]
) -> tuple[str, bool]:
    """A ``real_defect`` with no verifiable evidence is downgraded to ``uncertain`` (second item: downgraded)."""
    if verdict["verdict"] == "real_defect" and verified_evidence(verdict["evidence"], files) == 0:
        return "uncertain", True
    return verdict["verdict"], False


def final_label(first: str | None, second: str | None) -> str:
    return first if first is not None and first == second else "uncertain"


def alarm_label(finding_labels: Sequence[str]) -> str:
    if any(label == "real_defect" for label in finding_labels):
        return "real"
    if finding_labels and all(label == "not_a_defect" for label in finding_labels):
        return "not_a_defect"
    return "uncertain"


def wilson(k: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def share(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[c] * right[c] for c in left) / (n * n)
    if expected >= 1.0:
        return None
    return round((observed - expected) / (1 - expected), 4)


def headline_reading(real: int, total: int, validated: bool) -> str:
    if not validated:
        return "not validated"
    if total == 0:
        return "no alarms"
    p = real / total
    if p <= REAL_LOW:
        return "alarms mostly not real: chance reading stands"
    if p >= REAL_HIGH:
        return "alarms mostly real: chance reading wrong"
    return "mixed"


def corrected_balanced_accuracy(
    *,
    defective: int,
    defective_flagged: int,
    unchanged: int,
    unchanged_flagged: int,
    real_alarms: int,
    uncertain_alarms: int,
) -> dict[str, float]:
    """Lower/upper bound with confirmed-real unchanged alarms relabelled defective (rest of unflagged assumed clean).

    ``uncertain_alarms`` unchanged alarms are counted as false alarms for the lower bound and as real for the upper
    bound.
    """
    bounds = {}
    for name, extra in (("lower", 0), ("upper", uncertain_alarms)):
        relabel = real_alarms + extra
        positives = defective + relabel
        negatives = unchanged - relabel
        tp = defective_flagged + relabel
        tn = unchanged - unchanged_flagged
        bounds[name] = round(0.5 * (tp / positives + tn / negatives), 4)
    return bounds
